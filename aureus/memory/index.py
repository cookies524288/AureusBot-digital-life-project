"""记忆全文索引：只管增量保存和检索，内容由调用方决定（D80）。"""

import sqlite3
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from aureus.common import Clock, MemoryConfig, SystemClock
from aureus.common.sqlite import open_connection
from aureus.memory import bundles
from aureus.memory.bundle_export import export_knowledge
from aureus.memory.bundle_format import BundleMode
from aureus.memory.bundles import BundlePeople
from aureus.memory.consolidation import (
    CandidateKind,
    ConsolidationBatch,
    ConsolidationItem,
    DerivedCandidate,
    open_consolidation,
    read_batch,
    read_candidates,
    retention_weight,
    save_batch,
)
from aureus.memory.edits import (
    MemoryKind,
    effective_keyword_conditions,
    effective_ranked_matches,
    open_edits,
    write_edit,
)
from aureus.memory.flow import MemoryEvent, MemoryEventType, browse_flow, open_flow, publish_events
from aureus.memory.graph import (
    MemoryGraph,
    MemoryGraphSource,
    collect_graph_batch_members,
    put_graph_sources,
    read_graph,
)
from aureus.memory.graph_access import (
    GraphAccess,
    MemoryGraphDisplay,
    open_graph_access,
    save_graph_batch,
)
from aureus.memory.graph_query import (
    MAX_GRAPH_PAGE_SIZE,
    MemoryGraphItem,
    MemoryGraphPage,
    graph_detail,
    query_graph,
)
from aureus.memory.graph_recall import search_graph_relations
from aureus.memory.history import (
    HistoricalMemory,
    MemoryEvidence,
    MemorySource,
    browse_history,
    count_history_sources,
    open_history,
    publish_history,
    read_history,
    search_history,
)
from aureus.memory.inspection import LiveMemoryInspection, inspect_live_memory
from aureus.memory.message_boundary import (
    MESSAGE_EVENT_TYPE,
    MESSAGE_EVENT_TYPES,
    is_message_event,
    is_message_excerpt,
)
from aureus.memory.removal_operations import (
    MemoryRemovalOperation,
    browse_removal_operations,
    open_removal_operations,
)
from aureus.memory.resources import browse_text_resources, set_resources_removed
from aureus.memory.sparse import keyword_conditions, relevance_expression
from aureus.memory.transfer import export_archive, import_archive
from aureus.timeline import TimelineDatabase, TimelineQuery, TimelineRecord, VisibilityScope

# D80 第 4 条：单条记忆交给回复器时的正文上限。
MAX_HIT_CHARS = 180
_MAX_GRAPH_REPLY_HITS = 2
_LIVE_INDEX_SCHEMA = """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_index USING fts5(
        content, actor_label UNINDEXED, record_id UNINDEXED,
        recorded_at UNINDEXED, visibility UNINDEXED, person_key UNINDEXED,
        tokenize = 'trigram'
    )
"""


def _open_live_index(conn: sqlite3.Connection) -> None:
    """让全文条目的行号等于时间线序号，整理游标可跨重建使用。"""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_index)")}
        state_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(memory_index_state)")
        }
        if "person_key" not in columns or not state_columns:
            # 派生表从时间线重建，已提交的整理游标随原件保留。
            conn.execute("DROP TABLE IF EXISTS memory_index")
            conn.execute("DROP TABLE IF EXISTS memory_index_state")
            conn.execute(_LIVE_INDEX_SCHEMA)
            conn.execute("""
                CREATE TABLE memory_index_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1), indexed_through INTEGER NOT NULL,
                    stable_rowid INTEGER NOT NULL DEFAULT 1
                )
            """)
            conn.execute("INSERT INTO memory_index_state(id, indexed_through) VALUES (1, 0)")
        elif "stable_rowid" not in state_columns:
            conn.execute("ALTER TABLE memory_index RENAME TO memory_index_previous")
            conn.execute(_LIVE_INDEX_SCHEMA)
            conn.execute("""
                INSERT INTO memory_index(rowid, content, actor_label, record_id,
                                         recorded_at, visibility, person_key)
                SELECT record_id, content, actor_label, record_id,
                       recorded_at, visibility, person_key FROM memory_index_previous
            """)
            conn.execute("DROP TABLE memory_index_previous")
            conn.execute(
                "ALTER TABLE memory_index_state ADD COLUMN stable_rowid INTEGER NOT NULL DEFAULT 1"
            )


@dataclass(frozen=True)
class MemoryEntry:
    """心跳决定要收进索引的正文和称呼。"""

    actor_label: str
    text: str
    person_key: str | None = None


@dataclass(frozen=True)
class MemoryHit:
    """带来源和时刻的检索结果；正文已按 D80 截短。"""

    record_id: int | None
    recorded_at: datetime | None
    actor_label: str
    text: str
    source: MemorySource | None = None
    time_precision: str | None = None
    evidence: tuple[MemoryEvidence, ...] = ()
    relevance: float = 0
    sparse_score: float = 0.0
    is_enabled: bool = True
    is_recallable: bool = True
    original_text: str | None = None


def _search_extracted(
    conn: sqlite3.Connection,
    keywords: Sequence[str],
    filters: Sequence[str],
    filter_parameters: Sequence[str | int | float],
    now: datetime,
    half_life_days: float,
    limit: int,
) -> list[MemoryHit]:
    """查询旧版已提取的事实正文；时间线只判定来源资格并提供定位。"""
    aliases, terms = keyword_conditions(keywords, "memory_extracted_fts", "rowid")
    conn.create_function("retention_weight", 3, retention_weight, deterministic=True)
    conn.create_function("is_message_excerpt", 2, is_message_excerpt, deterministic=True)
    rows = conn.execute(
        "WITH matches AS (SELECT rowid, "
        + relevance_expression(aliases)
        + " AS words FROM memory_extracted_fts WHERE "
        + " OR ".join(aliases)
        + f"""), eligible AS (
            SELECT c.content, c.fingerprint, c.last_seen, matches.words,
                   t.id AS record_id, t.timestamp AS recorded_at,
                   CASE WHEN json_type(t.metadata, '$.person_id') = 'integer'
                        THEN 'aureus:' || json_extract(t.metadata, '$.person_id') END AS person_key
            FROM matches JOIN memory_candidates c ON c.rowid = matches.rowid
            JOIN memory_candidate_evidence e ON e.fingerprint = c.fingerprint
            JOIN timeline t ON t.id = CAST(substr(e.reference, 10) AS INTEGER)
            WHERE e.reference = 'timeline:' || t.id AND t.actor_type = 'user'
              AND t.visibility = 'user_only' AND t.event_type = '{MESSAGE_EVENT_TYPE}'
              AND c.kind IN ('fact', 'preference') AND NOT is_message_excerpt(c.content, t.content)
        )
        SELECT eligible.*, words * retention_weight(?, last_seen, ?) AS relevance
        FROM eligible LEFT JOIN memory_edits edit ON edit.kind = 'live'
             AND edit.entry_id = eligible.record_id
        WHERE edit.edited_content IS NULL AND """
        + " AND ".join(filters)
        + " ORDER BY relevance DESC, recorded_at DESC, record_id DESC, fingerprint",
        [*terms, *terms, now.timestamp(), half_life_days, *filter_parameters],
    )
    hits: dict[str, MemoryHit] = {}
    for row in rows:
        fingerprint = row["fingerprint"]
        if fingerprint in hits:
            continue
        hits[fingerprint] = MemoryHit(
            None,
            datetime.fromisoformat(row["recorded_at"]),
            "用户",
            row["content"][:MAX_HIT_CHARS],
            source=MemorySource("aureus", "memory", "extracted_facts", (fingerprint,)),
            evidence=(MemoryEvidence("platform_message", f"timeline:{row['record_id']}"),),
            relevance=row["relevance"],
        )
        if len(hits) == limit:
            break
    return list(hits.values())


class MemoryIndex:
    """可重建的 FTS5 trigram 索引；不解释事件、不改时间线。"""

    def __init__(
        self,
        db_path: Path,
        timeline: TimelineDatabase,
        clock: Clock | None = None,
        *,
        config: MemoryConfig | None = None,
    ) -> None:
        self._db_path, self._timeline = db_path, timeline
        self._clock = clock if clock is not None else SystemClock()
        self._config = config or MemoryConfig()
        self._conn: sqlite3.Connection | None = None
        self.graph_display = MemoryGraphDisplay(db_path, self._clock)

    def open(self) -> None:
        """创建派生表；SQLite 不支持 trigram 时抛 sqlite3.Error。"""
        if self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_connection(self._db_path)
        try:
            open_history(conn)
            _open_live_index(conn)
            open_edits(conn)
            open_graph_access(conn)
            bundles.open_bundles(conn)
            open_removal_operations(conn)
            open_consolidation(conn)
            open_flow(conn)
        except BaseException:
            conn.close()
            raise
        self._conn = conn

    def close(self) -> None:
        """关闭增量索引使用的连接。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def index_more(
        self, limit: int, describe: Callable[[TimelineRecord], MemoryEntry | None]
    ) -> int:
        """最多看 limit 条；正文与进度同批提交，异常回滚并往外抛。"""
        conn = self._conn
        if conn is None:
            raise RuntimeError("记忆索引还没有打开")
        through = conn.execute("SELECT indexed_through FROM memory_index_state WHERE id = 1")
        records = self._timeline.query(TimelineQuery(since_id=through.fetchone()[0], limit=limit))
        if not records:
            return 0
        with conn:
            for record in records:
                if record.event_type in MESSAGE_EVENT_TYPES:
                    continue
                entry = describe(record)
                if entry is not None:
                    conn.execute(
                        "INSERT INTO memory_index "
                        "(rowid, content, actor_label, record_id, recorded_at, "
                        "visibility, person_key) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            record.id,
                            entry.text,
                            entry.actor_label,
                            record.id,
                            record.timestamp.isoformat(),
                            record.visibility.value,
                            entry.person_key,
                        ),
                    )
            conn.execute(
                "UPDATE memory_index_state SET indexed_through = ? WHERE id = 1",
                (records[-1].id,),
            )
        return len(records)

    def publish(
        self,
        records: Iterable[HistoricalMemory],
        *,
        snapshot: tuple[str, str] | None = None,
        graph_sources: Iterable[MemoryGraphSource] = (),
    ) -> int:
        """写入长期记忆原件及派生索引，保留归档记录。"""
        if self._conn is None:
            raise RuntimeError("记忆索引还没有打开")
        published: set[str] = set()

        def published_records() -> Iterator[HistoricalMemory]:
            for record in records:
                if snapshot is not None and record.source.role == "memory":
                    published.add(record.source.encode())
                yield record

        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            written = publish_history(self._conn, published_records(), snapshot)
            if snapshot is None:
                put_graph_sources(self._conn, graph_sources)
            else:
                projected = tuple(graph_sources)
                put_graph_sources(self._conn, projected)
                members = collect_graph_batch_members(snapshot[0], published, projected)
                save_graph_batch(self._conn, snapshot, members)
        return written

    def read_graph(self, *, allowed_sources: Collection[MemorySource] | GraphAccess) -> MemoryGraph:
        """显式获准的原件范围内读取图来源；不初始化、扩权或推测关系。"""
        return read_graph(self._db_path, allowed_sources=allowed_sources, now=self._clock.now())

    def query_graph(
        self,
        *,
        allowed_sources: Collection[MemorySource] | GraphAccess,
        query: str = "",
        neighbor_of: MemorySource | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> MemoryGraphPage:
        """只读查询获准的实体和关系；页参数越界时抛 ValueError。"""
        if offset < 0 or not 1 <= limit <= MAX_GRAPH_PAGE_SIZE:
            raise ValueError(f"关系图分页要求 offset 非负、limit 为 1 至 {MAX_GRAPH_PAGE_SIZE}")
        graph = self.read_graph(allowed_sources=allowed_sources)
        return query_graph(graph, query=query, neighbor_of=neighbor_of, offset=offset, limit=limit)

    def graph_detail(
        self, source: MemorySource, *, allowed_sources: Collection[MemorySource] | GraphAccess
    ) -> MemoryGraphItem | None:
        """只读获准原件的图详情；未授权、失效或未知来源均返回 None。"""
        return graph_detail(self.read_graph(allowed_sources=allowed_sources), source)

    def search_graph_recall(self, keywords: Sequence[str], limit: int) -> list[MemoryHit]:
        """后台读取有限的内部关系和证据，不初始化任何图表。"""
        if not self._config.graph_recall_enabled or limit <= 0:
            return []
        with closing(open_connection(self._db_path)) as conn:
            conn.execute("BEGIN")
            records = search_graph_relations(
                conn,
                keywords,
                min(limit, _MAX_GRAPH_REPLY_HITS),
                now=self._clock.now(),
                max_chars=MAX_HIT_CHARS,
            )
        return [
            MemoryHit(None, None, item.actor_label, item.text, item.source, evidence=item.evidence)
            for item in records
        ]

    def get_history(self, source: MemorySource) -> HistoricalMemory | None:
        """读指定记忆原件及证据，包括归档记录但不读取平台消息。"""
        if self._conn is None:
            raise RuntimeError("记忆索引还没有打开")
        return read_history(self._conn, source)

    def consolidation_batch(self, size: int) -> ConsolidationBatch | None:
        """在后台连接中查是否累积满一批待整理的原件。"""
        with closing(open_connection(self._db_path)) as conn:
            return read_batch(conn, size)

    def browse_history(
        self,
        query: str,
        status: str,
        offset: int,
        limit: int,
        *,
        recall: str = "",
        source_table: str = "",
        omit_image_type: str = "",
    ) -> list[HistoricalMemory]:
        """按入库顺序浏览历史原件，保留原记录与证据。"""
        rows, _total = self.browse_history_page(
            query,
            status,
            offset,
            limit,
            recall=recall,
            source_table=source_table,
            omit_image_type=omit_image_type,
        )
        return rows

    def browse_history_page(
        self,
        query: str,
        status: str,
        offset: int,
        limit: int,
        *,
        recall: str = "",
        source_table: str = "",
        omit_image_type: str = "",
    ) -> tuple[list[HistoricalMemory], int]:
        """返回历史原件当前页和筛选后的总条数。"""
        with closing(open_connection(self._db_path)) as conn:
            return browse_history(
                conn,
                query,
                status,
                offset,
                limit,
                now=self._clock.now(),
                recall=recall,
                source_table=source_table,
                omit_image_type=omit_image_type,
            )

    def count_history_sources(self) -> dict[str, int]:
        """按原来源统计已迁入的记忆。"""
        with closing(open_connection(self._db_path)) as conn:
            return count_history_sources(conn)

    def inspect_live(self, visibility: VisibilityScope) -> LiveMemoryInspection:
        """核对指定可见范围内、原件仍在的本地文字索引。"""
        return inspect_live_memory(self._db_path, visibility)

    def browse_text_resources(
        self,
        query: str,
        category: str,
        status: str,
        recall: str,
        offset: int,
        limit: int,
        *,
        removed: bool = False,
    ) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
        """浏览同一目录下的新旧文字原件，保留各自的维护定位键。"""
        with closing(open_connection(self._db_path)) as conn:
            return browse_text_resources(
                conn,
                query,
                category,
                status,
                recall,
                offset,
                limit,
                self._clock.now(),
                removed=removed,
            )

    def set_resources_removed(
        self, targets: Sequence[tuple[MemoryKind, int]], removed: bool
    ) -> bool:
        """原子移除或恢复所选文字记忆，不修改正文覆盖或原召回状态。"""
        with closing(open_connection(self._db_path)) as conn:
            return set_resources_removed(
                conn,
                targets,
                removed,
                self._clock.now(),
                record_operation=self._config.removal_operations_enabled,
            )

    def browse_removal_operations(
        self, visibility: VisibilityScope, offset: int, limit: int
    ) -> tuple[list[MemoryRemovalOperation], int] | None:
        """查询可见的操作摘要；关闭记录时不读库。"""
        if not self._config.removal_operations_enabled:
            return None
        return browse_removal_operations(self._db_path, visibility, offset, limit)

    def publish_events(self, events: Iterable[MemoryEvent], snapshot: tuple[str, str]) -> int:
        """从相同迁入快照追加缺失的事件，保留原记忆。"""
        with closing(open_connection(self._db_path)) as conn:
            return publish_events(conn, events, snapshot)

    def browse_flow(
        self,
        offset: int,
        limit: int,
        *,
        event_type: MemoryEventType | None = None,
        conversation: str = "",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[list[MemoryEvent], list[str]]:
        """读取按原发生时间排序的长期记忆事件和有依据的会话。"""
        rows, conversations, _total = self.browse_flow_page(
            offset, limit, event_type=event_type, conversation=conversation, start=start, end=end
        )
        return rows, conversations

    def browse_flow_page(
        self,
        offset: int,
        limit: int,
        *,
        event_type: MemoryEventType | None = None,
        conversation: str = "",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[list[MemoryEvent], list[str], int]:
        """返回记忆流当前页、会话选项和筛选后的总条数。"""
        with closing(open_connection(self._db_path)) as conn:
            return browse_flow(
                conn,
                offset,
                limit,
                event_type=event_type,
                conversation=conversation,
                start=start,
                end=end,
            )

    def export_archive(self, output: BinaryIO) -> None:
        """将此运行库的长期记忆原件和维护覆盖写入文件。"""
        export_archive(self._db_path, output)

    def preview_bundle(self, file: BinaryIO, people: BundlePeople | None = None) -> dict[str, Any]:
        """校验上传包并检查当前安装条件。"""
        with closing(open_connection(self._db_path)) as connection:
            return bundles.preview_bundle(connection, file, people)

    def install_bundle(
        self, file: BinaryIO, mode: BundleMode, people: BundlePeople | None = None
    ) -> dict[str, Any]:
        """将兼容的包安装到实际记忆原件和索引。"""
        with closing(open_connection(self._db_path)) as connection:
            return bundles.install_bundle(connection, file, mode, self._clock.now(), people)

    def list_bundles(self, offset: int, limit: int) -> list[dict[str, Any]]:
        """浏览包安装记录。"""
        with closing(open_connection(self._db_path)) as connection:
            return bundles.list_bundles(connection, offset, limit)

    def set_bundle_installed(
        self, installation_id: str, installed: bool, people: BundlePeople | None = None
    ) -> bool:
        """可逆停用或恢复该包的独占资料。"""
        with closing(open_connection(self._db_path)) as connection:
            return bundles.set_bundle_installed(
                connection, installation_id, installed, self._clock.now(), people
            )

    def export_bundle(self, installation_id: str, output: BinaryIO) -> bool:
        """下载保存的原包资料。"""
        with closing(open_connection(self._db_path)) as connection:
            return bundles.export_bundle(connection, installation_id, output)

    def export_knowledge(self, output: BinaryIO) -> None:
        """把当前可用知识段落和证据关系打包。"""
        with closing(open_connection(self._db_path)) as connection:
            export_knowledge(connection, output, self._clock.now())

    def import_archive(self, file: BinaryIO) -> tuple[int, int]:
        """将文件追加到此运行库，返回新增和跳过条数。"""
        return import_archive(self._db_path, file)

    def browse_live(
        self, query: str, offset: int, limit: int, *, recall: str = ""
    ) -> list[MemoryHit]:
        """浏览已经索引的本地经历，返回时间线来源。"""
        rows, _total = self.browse_live_page(query, offset, limit, recall=recall)
        return rows

    def browse_live_page(
        self, query: str, offset: int, limit: int, *, recall: str = ""
    ) -> tuple[list[MemoryHit], int]:
        """浏览已索引的本地经历，返回时间线来源和筛选总条数。"""
        with closing(open_connection(self._db_path)) as conn:
            conn.execute("BEGIN")
            filtered = f"""FROM memory_index
                   LEFT JOIN memory_edits AS edit
                     ON edit.kind = 'live' AND edit.entry_id = memory_index.rowid
                   WHERE NOT coalesce(edit.is_removed, 0)
                     AND EXISTS (SELECT 1 FROM timeline event
                                 WHERE event.id = memory_index.record_id
                                   AND NOT ({is_message_event('event.event_type')}))
                     AND (? = '' OR coalesce(edit.is_enabled, 1) = (? = 'enabled'))
                     AND (? = '' OR instr(lower(edit.edited_content), lower(?)) > 0
                                   OR instr(lower(content), lower(?)) > 0
                                   OR instr(lower(actor_label), lower(?)) > 0)"""
            params = (recall, recall, query, query, query, query)
            total = conn.execute(f"SELECT count(*) {filtered}", params).fetchone()[0]
            rows = conn.execute(
                f"""SELECT record_id, content, actor_label, recorded_at, edit.edited_content,
                           coalesce(edit.is_enabled, 1) AS is_recallable {filtered}
                    ORDER BY record_id DESC LIMIT ? OFFSET ?""",
                (*params, limit, offset),
            ).fetchall()
        return [
            MemoryHit(
                row["record_id"],
                datetime.fromisoformat(row["recorded_at"]),
                row["actor_label"],
                row["edited_content"] or row["content"],
                is_enabled=bool(row["is_recallable"]),
                is_recallable=bool(row["is_recallable"]),
                original_text=row["content"] if row["edited_content"] is not None else None,
            )
            for row in rows
        ], total

    def update_resource(
        self,
        kind: MemoryKind,
        entry_id: int,
        text: str | None,
        is_enabled: bool | None,
    ) -> bool:
        """修订召回正文或设置召回开关，保留关联原件。"""
        with closing(open_connection(self._db_path)) as conn:
            return write_edit(conn, kind, entry_id, text, is_enabled)

    def save_consolidation(
        self,
        batch: ConsolidationBatch,
        extracted: Iterable[tuple[CandidateKind, str, ConsolidationItem]],
    ) -> bool:
        """在后台连接中原子提交整理结果。"""
        with closing(open_connection(self._db_path)) as conn:
            return save_batch(conn, batch, extracted, self._clock.now().timestamp())

    def consolidated_candidates(self, limit: int) -> list[DerivedCandidate]:
        """读取带原件指针的派生候选及当前权重。"""
        with closing(open_connection(self._db_path)) as conn:
            return read_candidates(
                conn,
                self._clock.now().timestamp(),
                self._config.consolidation_half_life_days,
                limit,
            )

    def search(
        self,
        keywords: Sequence[str],
        exclude_ids: Collection[int],
        limit: int,
        *,
        person_key: str | tuple[str, ...] | None = None,
        since: datetime | None = None,
        before: datetime | None = None,
        include_user_extractions: bool = True,
    ) -> list[MemoryHit]:
        """在同一次查询内召回历史与新经历，按稳定人物键和时间过滤。"""
        clauses, terms = effective_keyword_conditions(
            keywords, "memory_index", "memory_index.rowid", "memory_index.content"
        )
        if limit <= 0 or (
            not clauses and (keywords or (person_key is None and since is None and before is None))
        ):
            return []
        parameters: list[str | int | float] = list(terms)
        where = ["(" + " OR ".join(clauses) + ")"] if clauses else []
        where.append("coalesce(edit.is_enabled, 1) = 1 AND NOT coalesce(edit.is_removed, 0)")
        person_keys = (person_key,) if isinstance(person_key, str) else person_key
        if person_keys is not None:
            where.append("person_key IN (" + ",".join("?" for _ in person_keys) + ")")
            parameters.extend(person_keys)
        if since is not None:
            where.append("julianday(recorded_at) >= julianday(?)")
            parameters.append(since.isoformat())
        if before is not None:
            where.append("julianday(recorded_at) < julianday(?)")
            parameters.append(before.isoformat())
        if exclude_ids:
            where.append("record_id NOT IN (" + ",".join("?" for _ in exclude_ids) + ")")
            parameters.extend(exclude_ids)
        evidence_where = where[1:] if clauses else where
        evidence_parameters = parameters[len(terms) :]
        parameters.append(limit)
        rank_prefix, score, rank_parameters = effective_ranked_matches(
            keywords, "memory_index", "memory_index.rowid"
        )
        conn = open_connection(self._db_path)
        try:
            conn.execute("BEGIN")
            now = self._clock.now()
            has_timeline = conn.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'timeline' AND type = 'table'"
            ).fetchone()
            rows = (
                conn.execute(
                    rank_prefix
                    + "SELECT record_id, recorded_at, actor_label, "
                    + "coalesce(edit.edited_content, content) AS content, "
                    + relevance_expression(clauses)
                    + f" AS relevance, {score} AS sparse_score "
                    + "FROM memory_index LEFT JOIN memory_edits AS edit "
                    + "ON edit.kind = 'live' AND edit.entry_id = memory_index.rowid WHERE "
                    + "EXISTS (SELECT 1 FROM timeline event "
                    + "WHERE event.id = memory_index.record_id "
                    + f"AND NOT ({is_message_event('event.event_type')})) AND "
                    + " AND ".join(where)
                    + " ORDER BY relevance DESC, recorded_at DESC, sparse_score, "
                    + "CAST(record_id AS INTEGER) DESC LIMIT ?",
                    [*rank_parameters, *terms, *parameters],
                ).fetchall()
                if has_timeline
                else []
            )
            hits = [
                MemoryHit(
                    int(row["record_id"]),
                    datetime.fromisoformat(row["recorded_at"]),
                    row["actor_label"],
                    row["content"][:MAX_HIT_CHARS],
                    relevance=row["relevance"],
                    sparse_score=row["sparse_score"],
                )
                for row in rows
            ]
            if (
                has_timeline
                and clauses
                and include_user_extractions
                and self._config.consolidation_enabled
                and self._config.consolidation_extract_enabled
            ):
                hits.extend(
                    _search_extracted(
                        conn,
                        keywords,
                        evidence_where,
                        evidence_parameters,
                        now,
                        self._config.consolidation_half_life_days,
                        limit,
                    )
                )
            history = search_history(
                conn,
                keywords,
                limit,
                now=now,
                person_keys=person_keys,
                since=since,
                before=before,
            )
        finally:
            conn.close()
        hits.extend(
            MemoryHit(
                None,
                record.recorded_at,
                record.actor_label,
                record.text[:MAX_HIT_CHARS],
                record.source,
                record.time_precision,
                record.evidence,
                score,
                sparse_score,
            )
            for record, score, sparse_score in history
        )
        hits.sort(
            key=lambda hit: (
                hit.relevance,
                hit.recorded_at.timestamp() if hit.recorded_at else float("-inf"),
            ),
            reverse=True,
        )
        return hits[:limit]
