"""学习工作台宿主服务，适配 MaiBot 062d45c 的列表与维护契约。

来源 https://github.com/Mai-with-u/MaiBot/tree/062d45c/src/webui/routers，GPL-3.0。
资料和修改由 Aureus 权威存储负责；稳定编号与当前来源范围由宿主核定。
"""

from collections import Counter
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

from aureus.common import Clock
from aureus.memory import EmojiStore, LanguageContent, LanguageKind, LanguageStore
from aureus.memory.emoji import EmojiRecord
from aureus.memory.language_matching import JargonStatus, LanguageEntry


class LearningWorkbench:
    """每次操作重读权威资料与当前授权，不缓存另一份活跃资料。"""

    def __init__(
        self,
        language: LanguageStore | None,
        emojis: EmojiStore | None,
        clock: Clock,
        scopes: Callable[[], dict[str, str]],
        chat_targets: Callable[[], dict[str, dict[str, Any]]] | None = None,
    ) -> None:
        self.language, self.emojis, self.clock, self.scopes = language, emojis, clock, scopes
        self.chat_targets = chat_targets

    def language_rows(
        self, kind: LanguageKind, *, removed: bool = False, scopes: dict[str, str] | None = None
    ) -> list[LanguageEntry]:
        """返回当前来源允许管理的条目，保留待映射包的管理语义。"""
        if self.language is None:
            raise ValueError("学习功能当前关闭。")
        current = self.scopes() if scopes is None else scopes
        return [
            entry
            for entry in self.language.entries(kind, include_disabled=True)
            if entry.is_removed == removed and self._entry_in_scopes(entry, current)
        ]

    def language_payloads(
        self, kind: LanguageKind, *, removed: bool = False
    ) -> list[dict[str, Any]]:
        """投影原页面响应；审核字段只展示存量记录。"""
        scopes = self.scopes()
        entries = self.language_rows(kind, removed=removed, scopes=scopes)
        assert self.language is not None
        identifiers = self.language.workbench_ids(kind, (entry.entry_key for entry in entries))
        metadata = self.language.workbench_metadata(kind)
        result = []
        for entry in entries:
            stamp = metadata.get(entry.entry_key, {}).get("created_at")
            scope = (
                entry.learning_source
                or entry.restricted_scope
                or next((key for key in entry.scope_keys if key in scopes), "")
            )
            common = {
                "id": identifiers[entry.entry_key],
                "entry_key": entry.entry_key,
                "is_enabled": entry.is_enabled,
                "is_removed": entry.is_removed,
                "chat_name": scopes.get(scope, "共享资料" if not scope else ""),
            }
            if kind == LanguageKind.EXPRESSION:
                result.append(
                    {
                        **common,
                        "situation": entry.detail,
                        "style": entry.text,
                        "chat_id": scope,
                        "last_active_time": stamp or 0,
                        "create_date": stamp,
                        "checked": entry.review_status == "approved",
                        "modified_by": None,
                    }
                )
            else:
                sessions = list(
                    dict.fromkeys(key for key in (scope, *entry.scope_keys) if key in scopes)
                )
                result.append(
                    {
                        **common,
                        "content": entry.text,
                        "meaning": entry.detail,
                        "session_id": scope,
                        "session_ids": sessions,
                        "chat_names": [scopes[key] for key in sessions if key in scopes],
                        "count": entry.count,
                        "is_jargon": entry.jargon_status == JargonStatus.CONFIRMED,
                        "jargon_status": entry.jargon_status,
                        "is_complete": bool(entry.detail),
                        "is_legacy_empty_meaning": not bool(entry.detail),
                        "is_global": not sessions and not entry.pending_scope,
                        "created_by": "MANUAL"
                        if entry.entry_key.startswith('["manual",')
                        else "AI",
                        "created_timestamp": stamp,
                        "updated_timestamp": None,
                    }
                )
        if scopes != self.scopes():
            raise PermissionError("来源授权已变化，请刷新后重试。")
        return result

    def language_target(self, kind: LanguageKind, identifier: int) -> LanguageEntry:
        """编号只有在同一当前可管理集合中才有效。"""
        if self.language is None:
            raise ValueError("学习功能当前关闭。")
        keys = self.language.resolve_workbench_ids(kind, (identifier,))
        key = keys.get(identifier)
        if key is None:
            raise LookupError("找不到这条学习记录。")
        scopes = self.scopes()
        entry = next(
            (
                entry
                for entry in self.language.entries(kind, include_disabled=True)
                if entry.entry_key == key
            ),
            None,
        )
        if entry is None:
            raise LookupError("找不到这条学习记录。")
        if not self._entry_in_scopes(entry, scopes):
            raise PermissionError("来源授权已撤权。")
        if entry.is_removed:
            raise LookupError("这条学习记录已移除。")
        return entry

    def language_statistics(self, kind: LanguageKind) -> dict[str, Any]:
        """按当前可管理资料统计，不把未知时间当成最近发生。"""
        rows = self.language_payloads(kind)
        conversations: Counter[str] = Counter()
        for row in rows:
            targets = [row["chat_id"]] if kind == LanguageKind.EXPRESSION else row["session_ids"]
            conversations.update(key for key in targets if key)
        common = {
            "total": len(rows),
            "chat_count": len(conversations),
            "top_chats": dict(
                sorted(conversations.items(), key=lambda item: (-item[1], item[0]))[:5]
            ),
        }
        if kind == LanguageKind.EXPRESSION:
            now = self.clock.now()
            earliest = (now - timedelta(days=7)).timestamp()
            latest = now.timestamp()
            return {
                **common,
                "recent_7days": sum(
                    row["create_date"] is not None and earliest <= row["create_date"] <= latest
                    for row in rows
                ),
            }
        return {
            **common,
            "confirmed_jargon": sum(row["jargon_status"] == JargonStatus.CONFIRMED for row in rows),
            "confirmed_not_jargon": sum(
                row["jargon_status"] == JargonStatus.NON_JARGON for row in rows
            ),
            "pending": sum(row["jargon_status"] == JargonStatus.CANDIDATE for row in rows),
            "manual_jargon": sum(row["created_by"] == "MANUAL" for row in rows),
            "global_count": sum(row["is_global"] for row in rows),
            "complete_count": sum(row["is_complete"] for row in rows),
        }

    def language_chats(self, kind: LanguageKind) -> list[dict[str, Any]]:
        """仅列获准目标和角色实际采用的学习、召回状态。"""
        targets = (
            self.chat_targets()
            if self.chat_targets is not None
            else {key: {"chat_name": name} for key, name in self.scopes().items()}
        )
        rows = self.language_payloads(kind)
        if not targets.keys() <= self.scopes().keys():
            raise PermissionError("来源授权已变化，请刷新后重试。")
        return [
            {
                "chat_id": key,
                "session_id": key,
                "platform": None,
                "is_group": False,
                "enable_learning": False,
                "use_expression": False,
                **target,
                "expression_count": sum(row.get("chat_id") == key for row in rows),
                "jargon_count": sum(key in row.get("session_ids", []) for row in rows),
            }
            for key, target in targets.items()
        ]

    def language_targets_for_maintenance(
        self, kind: LanguageKind, identifiers: Sequence[int]
    ) -> list[LanguageEntry]:
        """映射旧编号并优先核验来源授权，再构造批量维护目标。"""
        if self.language is None:
            raise ValueError("学习功能当前关闭。")
        mapping = self.language.resolve_workbench_ids(kind, identifiers)
        if any(mapping.get(identifier) is None for identifier in identifiers):
            raise LookupError("所选记录不存在。")
        keys = {mapping[identifier] for identifier in identifiers}
        scopes = self.scopes()
        entries = {
            entry.entry_key: entry for entry in self.language.entries(kind, include_disabled=True)
        }
        if not keys <= entries.keys():
            raise LookupError("所选记录已经移除或不存在。")
        if any(not self._entry_in_scopes(entries[key], scopes) for key in keys):
            raise PermissionError("来源授权已撤权；没有修改。")
        if any(entries[key].is_removed for key in keys):
            raise LookupError("所选记录已经移除。")
        return [entries[key] for key in keys if key is not None]

    @staticmethod
    def _entry_in_scopes(entry: LanguageEntry, scopes: dict[str, str]) -> bool:
        """区分来源撤权、包授权和全局共享，不凭显示名称核准范围。"""
        return (
            (entry.learning_source is None or entry.learning_source in scopes)
            and (entry.restricted_scope is None or entry.restricted_scope in scopes)
            and (not entry.scope_keys or bool(set(entry.scope_keys) & scopes.keys()))
        )

    def language_keys_allowed(self, kind: LanguageKind, keys: Sequence[str]) -> bool:
        """在存储写事务中重验整批条目的原件范围。"""
        if self.language is None:
            return False
        scopes = self.scopes()
        entries = {
            entry.entry_key: entry for entry in self.language.entries(kind, include_disabled=True)
        }
        if not set(keys) <= entries.keys():
            return False
        return all(
            (entry.learning_source is None or entry.learning_source in scopes)
            and (entry.restricted_scope is None or entry.restricted_scope in scopes)
            and (not entry.scope_keys or bool(set(entry.scope_keys) & scopes.keys()))
            for entry in (entries[key] for key in keys)
        )

    def create_language(
        self, kind: LanguageKind, text: str, detail: str, scope: str | None, example: str = ""
    ) -> dict[str, Any]:
        """维护者明确创建的资料保存到同一语言库。"""
        if self.language is None:
            raise ValueError("学习功能当前关闭。")
        if scope and scope not in self.scopes():
            raise ValueError("所选目标不是当前获准会话。")
        key = self.language.add_entry(
            kind,
            LanguageContent(text, detail, example),
            origin_scope=scope,
            recorded_at=self.clock.now(),
            allowed=lambda: not scope or scope in self.scopes(),
        )
        return next(item for item in self.language_payloads(kind) if item["entry_key"] == key)

    def import_language(
        self,
        kind: LanguageKind,
        rows: Sequence[Any],
        scopes: Sequence[str],
        *,
        overwrite: bool = False,
        active: Callable[[], bool] = lambda: True,
    ) -> int:
        """将已由版本化解析器校验的记录写入当前资料库。"""
        if self.language is None:
            raise ValueError("学习功能当前关闭。")
        targets = tuple(dict.fromkeys(scopes))
        if not set(targets) <= self.scopes().keys():
            raise ValueError("导入目标已撤权或不存在。")
        result = self.language.import_workbench_entries(
            kind,
            rows,
            targets,
            self.clock.now(),
            allowed=lambda: active() and set(targets) <= self.scopes().keys(),
            overwrite=overwrite,
        )
        if result is None:
            raise ValueError("导入来源已撤权；没有修改。")
        return result

    def export_language(
        self, kind: LanguageKind, identifiers: Sequence[int]
    ) -> list[dict[str, Any]]:
        """已撤权、已移除或不属于当前可管理集合的目标不会导出。"""
        entries = [self.language_target(kind, identifier) for identifier in identifiers]
        assert self.language is not None
        return self.language.workbench_exports(
            kind,
            [entry.entry_key for entry in entries],
            allowed=lambda: all(
                entry.learning_source is None or entry.learning_source in self.scopes()
                for entry in entries
            ),
        )

    def update_language(
        self, kind: LanguageKind, identifier: int, text: str | None, detail: str | None
    ) -> dict[str, Any]:
        """正文维护保留原件和来源；不由客户端改写真实来源。"""
        entry = self.language_target(kind, identifier)
        assert self.language is not None
        content = LanguageContent(
            entry.text if text is None else text,
            entry.detail if detail is None else detail,
            entry.example,
        )
        if not self.language.update_entry(kind, entry.entry_key, content, None):
            raise LookupError("条目已变化。")
        return next(item for item in self.language_payloads(kind) if item["id"] == identifier)

    def classify_jargons(
        self, identifiers: Sequence[int], verdict: bool, session_active: Callable[[], bool]
    ) -> int:
        """整批复验后写入黑话定性；义项仍来自已保存正文。"""
        keys = [
            entry.entry_key
            for entry in self.language_targets_for_maintenance(LanguageKind.JARGON, identifiers)
        ]
        assert self.language is not None
        status = JargonStatus.CONFIRMED if verdict else JargonStatus.NON_JARGON
        changed = self.language.classify_jargons(
            keys,
            status,
            self.clock.now(),
            allowed=lambda: (
                session_active() and self.language_keys_allowed(LanguageKind.JARGON, keys)
            ),
        )
        if changed is None:
            raise LookupError("所选黑话已变化。")
        return changed

    def emoji_rows(self, *, removed: bool = False) -> list[EmojiRecord]:
        """管理目录复用 EmojiStore 的原件检查和移除状态。"""
        if self.emojis is None:
            raise ValueError("表情库当前关闭。")
        return self.emojis.browse_page("", 0, 2**31 - 1, removed=removed)[0]

    def emoji_payloads(self, *, removed: bool = False) -> list[dict[str, Any]]:
        """路径字段只给同源媒体 URL，避免公开本机实际路径。"""
        entries = self.emoji_rows(removed=removed)
        assert self.emojis is not None
        identifiers = self.emojis.workbench_ids(entry.entry_key for entry in entries)
        return [
            {
                "id": identifiers[entry.entry_key],
                "entry_key": entry.entry_key,
                "full_path": (
                    f"/api/webui/emoji/{identifiers[entry.entry_key]}/thumbnail?original=true"
                ),
                "format": (entry.media_type or "").removeprefix("image/"),
                "emoji_hash": entry.digest or "",
                "description": entry.description,
                "query_count": entry.collection_count,
                "usage_count": entry.use_count,
                "is_registered": entry.is_registered,
                "is_banned": entry.is_banned,
                "review": entry.review,
                "is_removed": entry.is_removed,
                "status": "discarded"
                if entry.is_banned
                else "adopted"
                if entry.is_registered
                else "known"
                if entry.description
                else "unknown",
                "emotion": ",".join(entry.tags) or None,
                "record_time": entry.collected_at or 0,
                "register_time": entry.collected_at if entry.is_registered else None,
                "last_used_time": entry.last_used_at,
            }
            for entry in entries
        ]

    def emoji_target(self, identifier: int) -> EmojiRecord:
        """反查并复验表情；已移除和不存在的目标不进入维护路径。"""
        if self.emojis is None:
            raise ValueError("表情库当前关闭。")
        keys = self.emojis.resolve_workbench_ids((identifier,))
        entry_key = keys.get(identifier)
        entry = self.emojis.catalog_entry(entry_key) if entry_key is not None else None
        if entry is None:
            removed = self.emojis.catalog_entry(entry_key, removed=True) if entry_key else None
            if removed is not None:
                raise LookupError("这张表情已移除。")
            raise LookupError("找不到这张表情。")
        return entry

    def find_emoji(self, identifier: int) -> EmojiRecord:
        """解析旧式整数编号，仅用于上传可能产生的待审批记录。"""
        if self.emojis is None:
            raise ValueError("表情库当前关闭。")
        keys = self.emojis.resolve_workbench_ids((identifier,))
        key = keys.get(identifier)
        entry = self.emojis.catalog_entry(key) if key else None
        if entry is None:
            raise LookupError("找不到这张表情。")
        return entry

    def set_emoji_removed(self, identifiers: Sequence[int], removed: bool) -> bool:
        """使用同一 EmojiStore 的可逆移除标记。"""
        if self.emojis is None:
            raise ValueError("表情库当前关闭。")
        keys = self.emojis.resolve_workbench_ids(identifiers)
        if any(identifier not in keys for identifier in identifiers):
            return False
        entry_keys = [keys[identifier] for identifier in identifiers]
        return self.emojis.set_removed(entry_keys, removed)

    def emoji_counts(self) -> dict[str, int]:
        """使用目录存储返回真实计数。"""
        if self.emojis is None:
            raise ValueError("表情库当前关闭。")
        return self.emojis.catalog_counts()
