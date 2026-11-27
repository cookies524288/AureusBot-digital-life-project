"""索引内容与召回格式（D80）。

结果块由 MaiBot 的 _build_replyer_memory_reference 移植并适配：
https://github.com/Mai-with-u/MaiBot/blob/edf42446f123149c57fc79765dd5a8b004b8524c/src/maisaka/builtin_tool/query_memory.py
原许可证 GPL-3.0；改用类型化命中、当地时间和称呼，增加无命中说明及整条裁剪。
去掉 MaiBot 的工具调用、语义后端、查询模式和状态依赖。
"""

import logging
import re
from collections.abc import Sequence
from datetime import tzinfo
from hashlib import sha256

from aureus.agenda import FindingStore
from aureus.common import MemoryConfig
from aureus.common.prompt_text import PromptTemplate, join_prompt
from aureus.heartbeat import events
from aureus.memory import MemoryEntry, MemoryHit, MemoryIndex
from aureus.people import PeopleDirectory
from aureus.timeline import ActorType, TimelineRecord

logger = logging.getLogger(__name__)
# D80 第 4 条：MaiBot 结果块上限；超过时只丢整条。
MAX_RECALL_CHARS = 900
REPLYER_MEMORY_REFERENCE_MARKER = PromptTemplate(
    "replyer_memory_reference",
    "按需记忆召回标题",
    "【长期记忆检索结果·内部参考】只供参考，不要原样念出来。",
)
HEURISTIC_MEMORY_REFERENCE_MARKER = PromptTemplate(
    "heuristic_memory_reference",
    "启发式记忆召回标题",
    "【启发式记忆·内部参考】只供参考，不要原样念出来。",
)
_RECALL_CUES = re.compile(r"还记得|记得|我说过|你说过|之前|上次|以前|最近")
_QUERY_WORD = re.compile(r"[^\W_]{2,20}")


def heuristic_keywords(messages: Sequence[TimelineRecord]) -> tuple[str, ...]:
    """按本轮明确的回忆提示提取若干检索词，沿用 D80 的词数与长度限制。"""
    newest = messages[-1].content
    if not _RECALL_CUES.search(newest):
        return ()
    query = _RECALL_CUES.sub(" ", newest).rstrip("？?。！! ").removesuffix("吗").removesuffix("呢")
    return tuple(dict.fromkeys(_QUERY_WORD.findall(query)))[:3]


def memory_entry(
    record: TimelineRecord, *, people: PeopleDirectory | None, findings: FindingStore | None
) -> MemoryEntry | None:
    """决定 D80 要收的经历；人物和收获的读取错误由调用方处理。"""
    kind = record.event_type
    if kind == events.EVENT_MESSAGE:
        label = "某人"
        person_id = record.metadata.get("person_id")
        if record.actor_type is ActorType.ME:
            label = "我"
        elif record.actor_type is ActorType.USER:
            label = "用户"
        elif record.actor_type is ActorType.PERSON and people is not None:
            person = people.get(person_id) if type(person_id) is int else None
            if person is not None and person.name:
                label = person.name
        person_key = f"aureus:{person_id}" if type(person_id) is int else None
        return MemoryEntry(label, record.content, person_key)
    if kind == events.EVENT_EXPLORATION_RESULT:
        if record.metadata.get("outcome") != "found" or findings is None:
            return None
        finding_id = record.metadata.get("finding_id")
        finding = findings.get(finding_id) if type(finding_id) is int else None
        return MemoryEntry("我", "（网页资料）" + finding.text) if finding is not None else None
    if kind == events.EVENT_DIARY_WRITTEN:
        return MemoryEntry("日记", record.content)
    if kind in (
        events.EVENT_INITIATIVE_RESULT,
        events.EVENT_AGENDA_ADDED,
        events.EVENT_INTEREST_CHANGED,
    ):
        return MemoryEntry("我", record.content)
    return None


def format_recall(
    keywords: Sequence[str],
    hits: Sequence[MemoryHit],
    timezone: tzinfo | None = None,
    *,
    heuristic: bool = False,
) -> str:
    """格式化受预算限制的召回参考。"""
    return format_recall_hits(keywords, hits, timezone, heuristic=heuristic)[0]


def format_recall_hits(
    keywords: Sequence[str],
    hits: Sequence[MemoryHit],
    timezone: tzinfo | None = None,
    *,
    heuristic: bool = False,
) -> tuple[str, Sequence[MemoryHit]]:
    """移植 MaiBot 的查询、命中数和编号行格式；从末尾删整条至 900 字内。"""
    query = f"查询：{'、'.join(keywords)}"
    hit_lines: list[str] = []
    for index, hit in enumerate(hits, start=1):
        normalized_content = " ".join(hit.text.split())
        stamp = (
            hit.recorded_at.astimezone(timezone).strftime("%Y-%m-%d %H:%M")
            if hit.recorded_at is not None
            else "时间未记录"
        )
        if hit.time_precision:
            stamp += f"（{hit.time_precision}）"
        provenance = ""
        if hit.source is not None:
            digest = sha256(hit.source.encode().encode()).hexdigest()[:12]
            linked = sum(item.source is not None and item.status is None for item in hit.evidence)
            provenance = f" [来源 {hit.source.table}/{digest}，可追溯证据 {linked} 条]"
        actor = " ".join(hit.actor_label.split())[:48] or "说话人未记录"
        hit_lines.append(f"{index}. {stamp} {actor}：{normalized_content}{provenance}")
    while True:
        body = "\n".join(
            [query, f"命中：{len(hit_lines)} 条", *(hit_lines or ["没有找到相关的记忆。"])]
        )
        block = recall_reference(body, heuristic=heuristic)
        if len(block) <= MAX_RECALL_CHARS:
            return block, hits[: len(hit_lines)]
        hit_lines.pop()


def recall_reference(body: str, *, heuristic: bool = False) -> str:
    """在同一召回标题下放入查询结果或预览占位。"""
    marker = HEURISTIC_MEMORY_REFERENCE_MARKER if heuristic else REPLYER_MEMORY_REFERENCE_MARKER
    return join_prompt([marker.fill(), body], "\n")


class MemoryTracker:
    """每拍推进索引；首次失败后停止索引，已收内容仍可查（D78、D80）。"""

    def __init__(
        self,
        index: MemoryIndex | None,
        config: MemoryConfig,
        people: PeopleDirectory | None,
        findings: FindingStore | None,
    ) -> None:
        self.index, self.config = index, config
        self.people, self.findings = people, findings
        self.failed = False

    async def index_more(self) -> None:
        """仅开关开启且尚未失败时索引，异常只记一次。"""
        if self.index is None or not self.config.enabled or self.failed:
            return
        try:
            await self.index.async_index_more(
                self.config.index_batch,
                # 消息已由索引层跳过；不把人物档案的长连接带到工作线程。
                lambda record: memory_entry(record, people=None, findings=self.findings),
            )
        except Exception:
            self.failed = True
            logger.exception("记忆索引失败，本次运行停止索引")
