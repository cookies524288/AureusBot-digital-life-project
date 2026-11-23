"""用手动时钟和假器官检查 QQ 表情的文字与语义选择。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.common import HeartbeatConfig, ManualClock
from aureus.heartbeat.emoji_selection import EmojiSelector
from aureus.heartbeat.planning import EmojiIntent, parse_plan
from aureus.heartbeat.reply_outbox import EmojiReplyPart
from aureus.memory import EmojiStore
from aureus.organs import CallPurpose, ModelPreference, OrganRequest, OrganResponse
from aureus.safety import CallRejectedError

PNG = b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_semantic_selection_refreshes_incrementally_and_falls_back(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    store = EmojiStore(tmp_path / "test.db", tmp_path, clock)
    store.open()
    happy = store.collect(PNG + b"happy", description="笑脸", tags=("高兴",), register=True)
    sad = store.collect(PNG + b"sad", description="哭脸", tags=("难过",), register=True)
    assert happy.digest is not None and sad.digest is not None
    requests: list[tuple[str | bytes, ...]] = []
    vectors = {
        "笑脸": (1.0, 0.0, 0.0),
        "哭脸": (0.0, 1.0, 0.0),
        "问号": (0.0, 0.0, 1.0),
        "高兴": (1.0, 0.0, 0.0),
        "困惑": (0.0, 0.0, 1.0),
    }

    async def call(request: OrganRequest) -> OrganResponse:
        assert request.purpose is CallPurpose.OPTIONAL
        requests.append(request.embedding_inputs)
        if request.context.get("task") == "emoji":
            return OrganResponse(request.organ_id, '{"selected": 1}')
        if request.embedding_inputs == ("拒绝",):
            raise CallRejectedError(request.organ_id, "测试拒绝")
        return OrganResponse(
            request.organ_id,
            "",
            metadata={"embedding_space": "test"},
            embeddings=tuple(vectors[str(text)] for text in request.embedding_inputs),
        )

    selector = EmojiSelector(
        store, call, clock, HeartbeatConfig(qq_emoji_semantic_enabled=True), UTC
    )

    async def select(emotion: str) -> EmojiReplyPart | None:
        return await selector.select(
            EmojiIntent(emotion, True), (), "用户", ModelPreference.PREFERRED, has_embedding=True
        )

    assert await select("高兴") == EmojiReplyPart(happy.digest, True)
    assert set(requests[0]) == {"笑脸", "哭脸"}
    assert requests[1:] == [("高兴",)]
    await select("困惑")
    assert requests[-2:] == [("困惑",), ()]

    added = store.collect(PNG + b"confused", description="问号", tags=("困惑",), register=True)
    assert added.digest is not None
    await select("困惑")
    assert requests[-2:] == [("困惑",), ()]
    clock.advance(timedelta(seconds=300))
    assert await select("困惑") == EmojiReplyPart(added.digest, True)
    assert requests[-2:] == [("问号",), ("困惑",)]
    assert await select("拒绝") is not None
    assert requests[-2:] == [("拒绝",), ()]

    store.set_status(added.digest, is_registered=True, is_banned=True)
    result = await select("困惑")
    assert result is not None and result.digest != added.digest
    assert requests[-2:] == [("困惑",), ()]


@pytest.mark.asyncio
async def test_default_uses_text_and_skips_missing_or_invalid_selection(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    store = EmojiStore(tmp_path / "test.db", tmp_path, clock)
    store.open()
    answers = iter((' {"selected": 2}', '{"selected": true}', '{"selected": 1}'))
    calls: list[OrganRequest] = []

    async def call(request: OrganRequest) -> OrganResponse:
        calls.append(request)
        assert not request.embedding_inputs
        assert request.context["task"] == "emoji"
        assert request.prompt.count("表示喜欢") == 1
        return OrganResponse(request.organ_id, next(answers))

    selector = EmojiSelector(store, call, clock, HeartbeatConfig(), UTC)
    plan = parse_plan('{"reply": true, "intent": "打招呼", "select_emoji": {"emotion": "喜欢"}}')
    assert plan is not None and plan.emoji is not None
    intent = plan.emoji

    async def select() -> EmojiReplyPart | None:
        return await selector.select(
            intent, (), "用户", ModelPreference.PREFERRED, has_embedding=True
        )

    assert await select() is None
    assert calls == []
    for data in (b"first", b"second"):
        store.collect(PNG + data, description="表示喜欢", register=True)
    assert await select() is None
    assert await select() is None
    selection = await select()
    assert selection is not None and selection.before_text is False
