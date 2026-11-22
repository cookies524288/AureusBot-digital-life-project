"""将 MaiBot 1.3.15 和 A_Memorix 2.0.0 的业务行发布到统一记忆。"""

import json
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, tzinfo
from hashlib import md5
from pathlib import Path

import msgpack

from aureus.memory import (
    HistoricalMemory,
    MemoryEvidence,
    MemoryIndex,
    MemorySnapshotConflictError,
    MemorySource,
    MemoryStatus,
)
from aureus.tools.migrate.maibot import open_readonly_database
from aureus.tools.migrate.maibot_catalog import MigrationError
from aureus.tools.migrate.maibot_memory_media import (
    AttachmentArchive,
    components,
    message_components,
)
from aureus.tools.migrate.maibot_memory_rows import MemoryRows, StagedRow, StagedValue

_FACT_STATES: dict[str, MemoryStatus] = {
    "active": "active",
    "conflicted": "conflicted",
    "superseded": "superseded",
    "retracted": "retracted",
}
_LINK_TARGETS = {
    "paragraph": "paragraphs",
    "relation": "relations",
    "entity": "entities",
    "episode": "episodes",
    "fact": "fact_claims",
    "image_occurrence": "image_occurrences",
}


def _text(values: dict[str, StagedValue], field: str) -> str:
    value = values.get(field)
    return str(value) if isinstance(value, (str, int, float)) else ""


def _time(value: StagedValue, zone: tzinfo) -> tuple[datetime | None, str | None]:
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, UTC), "亚秒" if value % 1 else "秒"
        if isinstance(value, str) and value:
            stamp = datetime.fromisoformat(value)
            precision = "日" if len(value) == 10 else "亚秒" if "." in value else "秒"
            return stamp.replace(tzinfo=zone) if stamp.tzinfo is None else stamp, precision
    except (OverflowError, OSError, ValueError):
        return None, None
    return None, None


def _list(value: StagedValue) -> list[str]:
    if not isinstance(value, str):
        return []
    try:
        decoded = json.loads(value)
    except ValueError:
        return []
    return (
        [item for item in decoded if isinstance(item, str) and item]
        if isinstance(decoded, list)
        else []
    )


def _person(instance: str, upstream_id: str) -> str | None:
    return f"maibot:{instance}:{upstream_id}" if upstream_id else None


def _message_person(row: StagedRow) -> str | None:
    platform, user = _text(row.values, "platform"), _text(row.values, "user_id")
    if not platform or not user:
        return None
    # MaiBot 1.3.15 person_info.py::get_person_id，沿用记忆库事实已经保存的人物键。
    platform = platform.split("-")[1] if "-" in platform else platform
    return _person(
        row.source.instance, md5(f"{platform}_{user}".encode(), usedforsecurity=False).hexdigest()
    )


class _Projection:
    def __init__(self, staged: MemoryRows, zone: tzinfo, archive: AttachmentArchive) -> None:
        self.staged, self.zone, self.archive = staged, zone, archive
        self.messages: dict[tuple[str, str], MemorySource | None] = {}
        self.chat_messages: dict[tuple[str, str], MemorySource | None] = {}
        self.memory: dict[str, dict[str, StagedRow]] = {}
        self.links: dict[tuple[str, str], list[MemoryEvidence]] = defaultdict(list)
        self.link_rows: dict[str, tuple[MemoryEvidence, ...]] = {}
        self.counts: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
        self.media: dict[tuple[str, str], MemorySource] = {}

    def _reference(self, kind: str, identifier: str) -> MemoryEvidence:
        row = self.memory.get(_LINK_TARGETS.get(kind, kind), {}).get(identifier)
        if row is None:
            return MemoryEvidence(kind, identifier)
        status = _source_status(row)
        return MemoryEvidence(kind, identifier, row.source, status if status != "active" else None)

    def _message_reference(self, platform: str, identifier: str) -> MemoryEvidence:
        return MemoryEvidence("message", identifier, self.messages.get((platform, identifier)))

    def _load_links(self) -> None:
        for row in self.staged.rows("main", "mai_messages"):
            identity = (_text(row.values, "platform"), _text(row.values, "message_id"))
            if all(identity):
                self.messages[identity] = None if identity in self.messages else row.source
            chat = (_text(row.values, "session_id"), identity[1])
            if all(chat):
                self.chat_messages[chat] = None if chat in self.chat_messages else row.source
        for table, field in (("images", "image_hash"), ("binary_data", "data_hash")):
            for row in self.staged.rows("main", table):
                if identifier := _text(row.values, field):
                    self.media[table, identifier] = row.source
        for table in (*dict.fromkeys(_LINK_TARGETS.values()), "image_assets"):
            self.memory[table] = {
                str(row.source.key[0]): row for row in self.staged.rows("memory", table)
            }
        for name, owner, owner_field, kind, target_field in (
            ("episode_paragraphs", "episodes", "episode_id", "paragraph", "paragraph_hash"),
            ("paragraph_relations", "paragraphs", "paragraph_hash", "relation", "relation_hash"),
            ("paragraph_entities", "paragraphs", "paragraph_hash", "entity", "entity_hash"),
            ("fact_evidence", "fact_claims", "claim_id", "", "evidence_id"),
            ("image_memory_links", "image_occurrences", "occurrence_id", "", "target_id"),
        ):
            for row in self.staged.rows("memory", name):
                raw = row.values
                identifier = _text(raw, target_field)
                link_kind = kind or _text(raw, "evidence_type") or _text(raw, "target_type")
                related = self._reference(link_kind, identifier)
                owner_id = _text(raw, owner_field)
                self.link_rows[row.source.encode()] = (self._reference(owner, owner_id), related)
                if name != "image_memory_links" or _text(raw, "status") == "active":
                    self.links[owner, owner_id].append(related)

    def _message_content(self, row: StagedRow, evidence: list[MemoryEvidence]) -> str:
        fragments: list[str] = []
        try:
            parsed = message_components(row)
        except (KeyError, ValueError, msgpack.UnpackException):
            evidence.append(MemoryEvidence("attachment_invalid_message_components", "raw_content"))
            return ""
        for component in components(parsed):
            kind, data = component.get("type"), component.get("data")
            identifier = component.get("hash")
            if kind == "text" and isinstance(data, str):
                fragments.append(data)
            elif kind in ("image", "emoji", "voice") and isinstance(identifier, str):
                table = "binary_data" if kind == "voice" else "images"
                source = self.media.get((table, identifier))
                if source is None and table == "images":
                    source = self.media.get(("binary_data", identifier))
                evidence.append(MemoryEvidence("attachment", identifier, source))
            elif kind == "file" and isinstance(data, dict):
                encoded = data.get("base64")
                reference = data.get("url") or data.get("file_id") or data.get("name")
                if isinstance(encoded, str) and encoded:
                    evidence.append(self.archive.inline(encoded))
                elif isinstance(reference, str) and reference:
                    evidence.append(MemoryEvidence("attachment_external", reference))
                else:
                    evidence.append(MemoryEvidence("attachment_reference_missing", "file"))
        return "".join(fragments)

    def _record_attachment(self, row: StagedRow) -> MemoryEvidence | None:
        reference_field, digest_field = {
            "images": ("full_path", "image_hash"),
            "binary_data": ("full_path", "data_hash"),
            "image_assets": ("storage_key", "content_hash"),
        }.get(row.source.table, ("", ""))
        if not reference_field:
            return None
        if row.source.table == "images" and _text(row.values, "no_file_flag") in {"1", "True"}:
            reference = _text(row.values, reference_field) or _text(row.values, digest_field)
            return MemoryEvidence("attachment_marked_missing", reference)
        return self.archive.file(
            row, _text(row.values, reference_field), _text(row.values, digest_field)
        )

    def _convert(self, row: StagedRow) -> HistoricalMemory:
        raw, table = row.values, row.source.table
        status: MemoryStatus = "active"
        label, person = "", None
        evidence = list(self.links.get((table, str(row.source.key[0])), ()))
        evidence.extend(self.link_rows.get(row.source.encode(), ()))
        text_field, time_field = {
            "mai_messages": ("processed_plain_text", "timestamp"),
            "paragraphs": ("content", "event_time"),
            "episodes": ("summary", "event_time_start"),
            "fact_claims": ("value_text", "first_observed_at"),
            "image_observations": ("text", "created_at"),
            "image_occurrences": ("", "occurred_at"),
            "images": ("description", "record_time"),
        }.get(table, ("", ""))
        text = _text(raw, text_field)
        stamp, precision = _time(raw.get(time_field), self.zone)
        if table == "mai_messages":
            raw_text = self._message_content(row, evidence)
            text = raw_text or text
            label = _text(raw, "user_nickname") or _text(raw, "user_cardname")
            person = _message_person(row)
            if reply := _text(raw, "reply_to"):
                evidence.append(self._message_reference(_text(raw, "platform"), reply))
        elif table == "paragraphs":
            if stamp is None:
                stamp, precision = _time(raw.get("event_time_start"), self.zone)
            evidence.extend(
                self._reference("relation", key) for key in _list(raw.get("relation_ids"))
            )
        elif table == "episodes":
            text = " ".join(dict.fromkeys(filter(None, (_text(raw, "title"), text))))
            evidence.extend(
                self._reference("paragraph", key) for key in _list(raw.get("evidence_ids_json"))
            )
        elif table == "fact_claims":
            status = _FACT_STATES.get(_text(raw, "status"), "inactive")
            if _text(raw, "scope_type") == "person":
                person = _person(row.source.instance, _text(raw, "scope_id"))
            if fact := _text(raw, "fact_key"):
                polarity = "否定：" if _text(raw, "polarity") == "negative" else ""
                text = f"{polarity}{fact}：{text}" if text else ""
        elif table == "relations":
            text = " ".join(_text(raw, part) for part in ("subject", "predicate", "object")).strip()
            if target := _text(raw, "source_paragraph"):
                evidence.append(self._reference("paragraph", target))
        elif table == "image_observations":
            occurrence_id = _text(raw, "occurrence_id")
            occurrence = self.memory["image_occurrences"].get(occurrence_id)
            evidence.append(self._reference("image_occurrence", occurrence_id))
            # D92 保留有用正文，只有查得到源状态时才按证据行状态归档。
            if occurrence is not None:
                if (occurred := _time(occurrence.values.get("occurred_at"), self.zone))[0]:
                    stamp, precision = occurred
                asset_id = _text(occurrence.values, "asset_id")
                asset = self._reference("image_assets", asset_id)
                evidence.append(asset)
                status = _source_status(occurrence)
                if status == "active" and asset.status is not None:
                    status = asset.status
            if _text(raw, "superseded_by"):
                status = "superseded"
        elif table == "image_occurrences":
            evidence.append(self._reference("image_assets", _text(raw, "asset_id")))
            if _text(raw, "source_kind") == "chat" and (message := _text(raw, "message_id")):
                source = self.chat_messages.get((_text(raw, "chat_id"), message))
                evidence.append(MemoryEvidence("message", message, source))
        if attachment := self._record_attachment(row):
            evidence.append(attachment)
        if (source_status := _source_status(row)) != "active":
            status = source_status
        elif not text.strip() and status == "active":
            status = "missing"
        valid_from, _ = _time(raw.get("valid_from"), self.zone)
        valid_to_field = "valid_to" if "valid_to" in raw else "expires_at"
        valid_to, _ = _time(raw.get(valid_to_field), self.zone)
        received_at, _ = (
            _time(raw.get("received_at"), self.zone) if table == "mai_messages" else (None, None)
        )
        unique = {
            (item.kind, item.reference, item.source.encode() if item.source else None): item
            for item in evidence
        }
        return HistoricalMemory(
            row.source,
            text,
            label,
            person,
            stamp,
            _text(raw, "time_granularity") or precision,
            status,
            valid_from,
            valid_to,
            tuple(unique.values()),
            received_at,
        )

    def records(self) -> Iterator[HistoricalMemory]:
        self._load_links()
        for role, table in sorted(self.staged.tables):
            counts = self.counts[role, table]
            if role == "main" and table not in {"mai_messages", "images", "binary_data"}:
                counts["other_subsystem"] = self.staged.tables[role, table]
                continue
            if table.startswith("person_profile_"):
                counts["other_subsystem"] = self.staged.tables[role, table]
                continue
            rows = (
                self.memory[table].values()
                if table in self.memory
                else self.staged.rows(role, table)
            )
            for row in rows:
                record = self._convert(row)
                counts["published"] += 1
                if record.status != "active":
                    counts["archive"] += 1
                for link in record.evidence:
                    if link.kind.startswith("attachment_"):
                        counts[link.kind] += 1
                    elif link.source is None:
                        counts["unresolved_links"] += 1
                yield record


def _source_status(row: StagedRow) -> MemoryStatus:
    raw, table = row.values, row.source.table
    if _text(raw, "is_deleted") in {"1", "True"} or table == "deleted_relations":
        return "deleted"
    if _text(raw, "is_inactive") in {"1", "True"} or _text(raw, "is_banned") in {"1", "True"}:
        return "inactive"
    if table in {"image_assets", "image_occurrences", "image_memory_links"}:
        state = _text(raw, "status")
        if state == "deleted":
            return "deleted"
        if state != "active":
            return "inactive"
    if table == "fact_claims":
        return _FACT_STATES.get(_text(raw, "status"), "inactive")
    return "active"


def publish_maibot_memory(
    stage_path: Path,
    instance: str,
    zone: tzinfo,
    index: MemoryIndex,
    roots: dict[str, Path | None],
    destination: Path,
) -> dict[str, object]:
    """从 3.2 已校验的暂存库发布记忆，返回逐表计数而不泄漏私人内容。"""
    with closing(open_readonly_database(stage_path)) as connection:
        connection.execute("BEGIN")
        projection = _Projection(
            MemoryRows(connection, instance), zone, AttachmentArchive(roots, destination)
        )
        index.open()
        try:
            snapshot = instance, projection.staged.fingerprint(str(zone))
            index.publish(projection.records(), snapshot=snapshot)
        except MemorySnapshotConflictError as error:
            raise MigrationError("memory_snapshot_conflict") from error
        finally:
            index.close()
    return {
        "operation": "publish-memory",
        "tables": [
            {
                "role": role,
                "table": table,
                "source": projection.staged.tables[role, table],
                **counts,
            }
            for (role, table), counts in sorted(projection.counts.items())
        ],
    }
