"""心跳：在真实回复前取表达参考，后台增量补建派生索引（D101）。

查询语句和选择器协议移植自 MaiBot 1.3.15 的 maisaka_expression_selector.py
（GPL-3.0，见 THIRD_PARTY.md）；器官接线、权限与失效使用 Aureus 的实现。
"""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from aureus.common import LanguageConfig
from aureus.heartbeat.planning import Plan, first_json_object
from aureus.memory import ExpressionVectorIndex, LanguageStore
from aureus.memory.expression_index import (
    EXPRESSION_BACKFILL_BATCH,
    MIN_INDEXED_EXPRESSIONS,
    distinct_expressions,
    embedding_text,
    fingerprint,
    format_vector_expressions,
    rank_expressions,
)
from aureus.memory.language_matching import LanguageEntry
from aureus.organs import (
    LOCAL_EXPRESSION_EMBEDDING,
    CallPurpose,
    OrganCallError,
    OrganRequest,
    OrganResponse,
)
from aureus.safety import CallRejectedError

logger = logging.getLogger(__name__)
_MAX_SELECTED_EXPRESSIONS = 5
EXPRESSION_MAINTENANCE_BATCH = 25


@dataclass
class ExpressionBackfillRun:
    """本次人工操作；只有原登录可查结果，写入数来自已提交的批次。"""

    id: str
    owner: str
    space: str
    state: Literal["running", "writing", "written", "unchanged", "failed", "cancelled"] = "running"
    written: int = 0


def _query_text(plan: Plan | None) -> str:
    if plan is None:
        return ""
    parts = []
    if plan.expression_intent_lines:
        parts.append("表达选择意图：\n" + "\n".join(plan.expression_intent_lines))
    if plan.reply_reference:
        parts.append(f"回复信息参考：\n{plan.reply_reference}")
    elif plan.reason:
        parts.append(f"Planner 推理：\n{plan.reason}")
    return " ".join("\n\n".join(parts).split())


class ExpressionRecall:
    """每次回复重读实时候选；后台缺项补建不阻塞第一次普通回复。"""

    def __init__(
        self,
        language: LanguageStore,
        index: ExpressionVectorIndex,
        config: LanguageConfig,
        call: Callable[[OrganRequest], Awaitable[OrganResponse]],
    ) -> None:
        self._language, self._index, self._config, self._call = language, index, config, call
        self.space: str | None = None
        self._backfill: asyncio.Task[None] | None = None
        self._maintenance: asyncio.Task[None] | None = None
        self._auto_building = False
        self._scope_provider: Callable[[], frozenset[str]] | None = None
        self.maintenance_run: ExpressionBackfillRun | None = None

    async def close(self) -> None:
        """关闭运行时，取消尚未提交的补建；已进入事务则等待实际结果。"""
        if self._backfill is not None:
            self._backfill.cancel()
            with suppress(asyncio.CancelledError):
                await self._backfill
        if self._maintenance is not None:
            if self.maintenance_run is not None and self.maintenance_run.state == "running":
                self.cancel_maintenance(self.maintenance_run)
            with suppress(asyncio.CancelledError):
                await self._maintenance

    def _candidates(self, scope: str | None, avoid: tuple[str, ...]) -> list[LanguageEntry]:
        return distinct_expressions(self._language.expression_candidates(scope), avoid)

    async def reference(
        self,
        plan: Plan | None,
        scope: str | None,
        *,
        recheck: Callable[[], Awaitable[None]] | None = None,
    ) -> str | None:
        """无可用向量结果时返回 None，调用方继续使用原有普通表达。"""
        space = self.space
        if space is None:
            return None
        avoid = plan.expression_avoid if plan is not None else ()
        query = _query_text(plan)
        try:
            entries = await asyncio.to_thread(self._candidates, scope, avoid)
            indexed = await asyncio.to_thread(self._index.indexed, space, entries) if query else []
            if len(indexed) < MIN_INDEXED_EXPRESSIONS or self.space != space:
                return None
            if recheck is not None:
                await recheck()
            (vector,) = await self._embed(space, (query,))
            if not vector or self.space != space:
                return None
            if any(len(item.vector) != len(vector) for item in indexed):
                await asyncio.to_thread(self._index.discard_incompatible, space, len(vector))
                return None
            ordered = await asyncio.to_thread(
                rank_expressions, indexed, vector, self._config.expression_vector_candidates
            )
            current = await self._current_fingerprints(scope, avoid)
            if (
                self.space != space
                or sum(
                    current.get(item.entry.entry_key) == fingerprint(item.entry) for item in indexed
                )
                < MIN_INDEXED_EXPRESSIONS
            ):
                return None
            eligible = [
                entry for entry in ordered if current.get(entry.entry_key) == fingerprint(entry)
            ]
            if recheck is not None:
                await recheck()
            chosen = await self._select(query, eligible)
            current = await self._current_fingerprints(scope, avoid)
            if self.space != space:
                return None
            valid = [
                entry for entry in chosen if current.get(entry.entry_key) == fingerprint(entry)
            ]
            # 选择为空时返回空字符串，本轮不注入表达块；None 只表示向量路线不可用。
            return format_vector_expressions(valid)
        except (CallRejectedError, OrganCallError, ValueError, sqlite3.Error) as error:
            logger.warning("表达向量查询未采用（%s），使用普通候选", type(error).__name__)
            return None

    def backfill(self, scope: str | None) -> None:
        """只启动一个补建任务，在对应回复调用之后处理当前合规材料。"""
        if self.space is not None and not self.build_busy:
            self._backfill = asyncio.create_task(
                self._build(self.space, scope), name="aureus-expression-index"
            )

    @property
    def build_busy(self) -> bool:
        """当前运行时仍有向量构建任务在途，或后台自动构建正在写这一批。"""
        return self._auto_building or any(
            task is not None and not task.done() for task in (self._backfill, self._maintenance)
        )

    def start_maintenance(
        self,
        owner: str,
        space: str,
        scopes: frozenset[str],
        allows: Callable[[], Awaitable[bool]],
    ) -> ExpressionBackfillRun | None:
        """复用当前本地路线和写入器，在原授权内补建一小批缺项。"""
        if self.space != space or self.build_busy:
            return None
        run = ExpressionBackfillRun(uuid4().hex, owner, space)
        self.maintenance_run = run
        self._maintenance = asyncio.create_task(
            self._maintain(run, scopes, allows), name="aureus-expression-maintenance"
        )
        return run

    def cancel_maintenance(self, run: ExpressionBackfillRun) -> bool:
        """提交前可取消；已交给 SQLite 的事务不报告成已取消。"""
        if run is not self.maintenance_run or run.state != "running" or self._maintenance is None:
            return False
        run.state = "cancelled"
        self._maintenance.cancel()
        return True

    async def _maintain(
        self,
        run: ExpressionBackfillRun,
        scopes: frozenset[str],
        allows: Callable[[], Awaitable[bool]],
    ) -> None:
        try:
            entries = await asyncio.to_thread(self._index.candidates, self._language, scopes)
            missing = await asyncio.to_thread(self._index.missing, run.space, entries)
            batch = missing[:EXPRESSION_MAINTENANCE_BATCH]
            if not await allows() or run.state != "running" or self.space != run.space:
                run.state = "cancelled"
                return
            if not batch:
                run.state = "unchanged"
                return
            vectors = await self._embed(
                run.space, tuple(map(embedding_text, batch)), purpose=CallPurpose.OPTIONAL
            )
            current = {
                entry.entry_key: fingerprint(entry)
                for entry in await asyncio.to_thread(self._index.candidates, self._language, scopes)
            }
            if not await allows() or run.state != "running" or self.space != run.space:
                run.state = "cancelled"
                return
            valid = [
                (entry, vector)
                for entry, vector in zip(batch, vectors, strict=True)
                if current.get(entry.entry_key) == fingerprint(entry)
            ]
            if not valid:
                run.state = "unchanged"
                return
            retained, retained_vectors = zip(*valid, strict=True)
            run.state = "writing"
            await asyncio.to_thread(self._index.publish, run.space, retained, retained_vectors)
            run.written = len(retained)
            run.state = "written"
        except asyncio.CancelledError:
            run.state = "cancelled"
            raise
        except Exception as error:
            if run.state != "cancelled":
                run.state = "failed"
                logger.warning("人工表达向量补建失败（%s）", type(error).__name__)

    async def build_authorized_batch(self, limit: int) -> tuple[bool, bool]:
        """心跳自动构建：为所有已授权范围补一批缺项（D125）。

        Returns:
            （是否还有缺项，本批是否写入过）。按需补建或人工维护在途时让出这一轮。
        """
        space = self.space
        if space is None:
            return False, False
        if self.build_busy:
            return True, False
        self._auto_building = True
        try:
            scopes = await asyncio.to_thread(self.authorized_scopes)
            entries = await asyncio.to_thread(self._index.candidates, self._language, scopes)
            return await self._publish_missing(
                space,
                entries,
                limit,
                purpose=CallPurpose.OPTIONAL,
                current=self._authorized_fingerprints,
            )
        finally:
            self._auto_building = False

    def set_scope_provider(self, provider: Callable[[], frozenset[str]]) -> None:
        """由心跳提供当前可见、已授权的来源范围（D125）。"""
        self._scope_provider = provider

    def authorized_scopes(self) -> frozenset[str]:
        """当前可处理的来源范围；全局候选由索引自己并入。

        心跳提供范围时以它为准；独立使用时退回语言库里的来源过滤器。
        """
        if self._scope_provider is not None:
            return self._scope_provider()
        pool = self._language.expression_pool()
        scopes = {scope for entry in pool for scope in entry.scope_keys}
        scopes.update(entry.restricted_scope for entry in pool if entry.restricted_scope)
        return frozenset(scope for scope in scopes if self._language.learning_source_allowed(scope))

    async def _authorized_fingerprints(self) -> dict[str, str]:
        scopes = await asyncio.to_thread(self.authorized_scopes)
        entries = await asyncio.to_thread(self._index.candidates, self._language, scopes)
        return {entry.entry_key: fingerprint(entry) for entry in entries}

    async def _publish_missing(
        self,
        space: str,
        entries: Sequence[LanguageEntry],
        limit: int,
        *,
        purpose: CallPurpose,
        current: Callable[[], Awaitable[dict[str, str]]],
    ) -> tuple[bool, bool]:
        """嵌入并发布一批指纹仍有效的缺项；返回（是否还有缺项，本批是否写入）。"""
        missing = await asyncio.to_thread(self._index.missing, space, entries)
        batch = missing[:limit]
        if not batch:
            return False, False
        valid = {entry.entry_key: fingerprint(entry) for entry in entries}
        batch = [entry for entry in batch if valid.get(entry.entry_key) == fingerprint(entry)]
        if not batch:
            return True, False
        vectors = await self._embed(space, tuple(map(embedding_text, batch)), purpose=purpose)
        current_marks = await current()
        retained = [
            (entry, vector)
            for entry, vector in zip(batch, vectors, strict=True)
            if current_marks.get(entry.entry_key) == fingerprint(entry)
        ]
        if not retained or self.space != space:
            return True, False
        retained_entries, retained_vectors = zip(*retained, strict=True)
        await asyncio.to_thread(self._index.publish, space, retained_entries, retained_vectors)
        return len(retained) < len(missing), True

    async def _build(self, space: str, scope: str | None) -> None:
        try:
            entries = await asyncio.to_thread(self._candidates, scope, ())
            await self._publish_missing(
                space,
                entries,
                EXPRESSION_BACKFILL_BATCH,
                purpose=CallPurpose.USER_REPLY,
                current=lambda: self._current_fingerprints(scope, ()),
            )
        except (CallRejectedError, OrganCallError, ValueError, sqlite3.Error) as error:
            logger.warning("表达向量补建未完成（%s），普通回复不受影响", type(error).__name__)

    async def _current_fingerprints(
        self, scope: str | None, avoid: tuple[str, ...]
    ) -> dict[str, str]:
        return {
            entry.entry_key: fingerprint(entry)
            for entry in await asyncio.to_thread(self._candidates, scope, avoid)
        }

    async def _embed(
        self, space: str, inputs: tuple[str, ...], *, purpose: CallPurpose = CallPurpose.USER_REPLY
    ) -> tuple[tuple[float, ...], ...]:
        response = await self._call(
            OrganRequest(
                LOCAL_EXPRESSION_EMBEDDING,
                "",
                purpose=purpose,
                embedding_inputs=inputs,
                embedding_space=space,
            )
        )
        if response.metadata.get("embedding_space") != space or len(response.embeddings) != len(
            inputs
        ):
            raise ValueError("表达嵌入返回的空间或数量不符。")
        return response.embeddings

    async def _select(self, query: str, ordered: Sequence[LanguageEntry]) -> list[LanguageEntry]:
        """按 MaiBot 协议让快速模型挑 0–5 条。

        没有配置 fast_model、安全网关拒绝或调用失败时退回向量前 5 条；
        与 MaiBot 的整池注入有意不同，避免把 40–50 条候选塞进提示词（D119）。
        """
        organ = self._config.expression_selector_organ
        candidates = "\n".join(
            f"{number}: 情景={entry.detail} | 风格={entry.text}"
            for number, entry in enumerate(ordered, 1)
        )
        prompt = (
            "你只负责根据下方参考，为这一次可见回复挑选最合适的表达方式。\n"
            f"请只从下面候选中选择 0 到 {_MAX_SELECTED_EXPRESSIONS} 条最适合当前语境的表达方式。\n"
            "优先考虑自然、贴合上下文、不生硬、不模板化。\n"
            "如果没有明显合适的，就返回空数组。\n"
            '严格只输出 JSON，对象格式为 {"selected_ids":[1,2]}。\n\n'
            f"匹配依据：\n{query}\n\n候选表达方式：\n{candidates}"
        )
        try:
            response = await self._call(
                OrganRequest(
                    organ,
                    prompt,
                    context={"task": "expression_selection"},
                    purpose=CallPurpose.USER_REPLY,
                )
            )
        except (CallRejectedError, OrganCallError, ValueError):
            return list(ordered[:_MAX_SELECTED_EXPRESSIONS])
        values = (first_json_object(response.suggestion) or {}).get("selected_ids")
        if not isinstance(values, list):
            return []
        ids = dict.fromkeys(
            value for value in values if type(value) is int and 1 <= value <= len(ordered)
        )
        return [ordered[value - 1] for value in list(ids)[:_MAX_SELECTED_EXPRESSIONS]]
