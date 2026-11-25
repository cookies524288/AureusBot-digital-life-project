"""合成入站、器官和 SQLite 验证表达独立质检。"""

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.common import ManualClock
from aureus.heartbeat.language_runner import ExpressionQuality, learn_language
from aureus.memory import LanguageKind, LanguageSample, LanguageStore
from aureus.memory.language_matching import ExpressionReviewStatus
from aureus.organs import CallPurpose, OrganRequest, OrganResponse
from aureus.safety import CallRejectedError

NOW = datetime(2026, 11, 24, tzinfo=UTC)
SAMPLES = [LanguageSample("合成来源", 1, "合成发言者", "整段私聊原文不应复制 GG")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_status", "expected_reason"),
    [
        ("off", None, None),
        ("passed", "passed", "matched"),
        ("rejected", "rejected", "too_specific"),
        ("unbound", "error", "no_route"),
        ("missing", "error", "no_route"),
        ("budget", "error", "blocked"),
        ("exception", "error", "failed"),
        ("malformed", "error", "invalid"),
    ],
)
async def test_independent_expression_quality_consumption_and_reply(
    tmp_path: Path, mode: str, expected_status: str | None, expected_reason: str | None
) -> None:
    database = tmp_path / "language.sqlite"
    store = LanguageStore(database)
    store.open()
    requests: list[str] = []

    async def call(request: OrganRequest) -> OrganResponse:
        task = str(request.context["task"])
        requests.append(task)
        if task == "learn_style":
            candidate = {"id": "1", "style": "乐一个", "situation": "轻松庆祝"}
            return OrganResponse(
                request.organ_id,
                json.dumps(
                    [
                        candidate,
                        candidate,
                        {**candidate, "id": "999"},
                        {**candidate, "style": "!!"},
                    ]
                ),
            )
        if task == "learn_jargon":
            return OrganResponse(request.organ_id, '[{"id":1,"content":"GG"}]')
        assert request.organ_id == "judge" and request.purpose == CallPurpose.OPTIONAL
        assert tuple(json.loads(message) for _, message in request.preceding_messages) == (
            {"situation": "轻松庆祝", "style": "乐一个"},
        )
        if mode == "budget":
            raise CallRejectedError(request.organ_id, "不应持久化的异常详情")
        if mode == "missing":
            raise ValueError("未注册")
        if mode == "exception":
            raise RuntimeError("不应持久化的异常详情")
        return OrganResponse(
            request.organ_id,
            {
                "passed": '{"suitable":true,"reason":"matched"}',
                "rejected": '{"suitable":false,"reason":"too_specific"}',
                "malformed": '{"suitable":"true","reason":"整段私聊原文不应复制 GG"}',
            }[mode],
        )

    quality = (
        None
        if mode == "off"
        else ExpressionQuality(None if mode == "unbound" else "judge", ManualClock(NOW))
    )
    for _ in range(2):
        await learn_language(
            store,
            "summarizer",
            call,
            SAMPLES * 2,
            conversation_key="甲群",
            expression_quality=quality,
        )
    expected_requests = ["learn_style", "learn_jargon"]
    if mode not in {"off", "unbound"}:
        expected_requests.insert(1, "expression_quality")
    assert requests == expected_requests
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT kind FROM language_extractions WHERE kind != 'frequency' ORDER BY kind"
        ).fetchall() == [
            ("expression",),
            ("jargon",),
        ]
        rows = connection.execute(
            "SELECT source_key, status, reason, recorded_at FROM language_expression_quality"
        ).fetchall()
        assert rows == (
            [
                ("合成来源", expected_status, expected_reason, NOW.isoformat()),
            ]
            if expected_status
            else []
        )
    entries = store.entries(LanguageKind.EXPRESSION)
    assert len(entries) == (mode in {"off", "passed"})

    def reference() -> str:
        return store.expression_reference("轻松庆祝", "庆祝", (), "", 700)

    assert ("乐一个" in reference()) == (mode in {"off", "passed"})
    if mode == "passed":
        [entry] = entries
        assert entry.example == "" and entry.review_status == ExpressionReviewStatus.PENDING
        store.review_expressions([entry.entry_key], ExpressionReviewStatus.REJECTED, "旧审核", NOW)
        assert "乐一个" in reference()
        assert store.set_removed(LanguageKind.EXPRESSION, [entry.entry_key], True)
        assert "乐一个" not in reference()


@pytest.mark.asyncio
async def test_cancelled_quality_does_not_commit_expression_consumption(tmp_path: Path) -> None:
    store = LanguageStore(tmp_path / "language.sqlite")
    store.open()
    started = asyncio.Event()

    async def call(request: OrganRequest) -> OrganResponse:
        if request.context["task"] == "expression_quality":
            started.set()
            await asyncio.Future()
        return OrganResponse(request.organ_id, '[{"id":1,"style":"嘿嘿","situation":"庆祝"}]')

    task = asyncio.create_task(
        learn_language(
            store,
            "summarizer",
            call,
            SAMPLES,
            conversation_key="甲群",
            expression_quality=ExpressionQuality("judge", ManualClock(NOW)),
        )
    )
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.pending_extraction(LanguageKind.EXPRESSION, SAMPLES) == SAMPLES
    assert not store.entries(LanguageKind.EXPRESSION)
