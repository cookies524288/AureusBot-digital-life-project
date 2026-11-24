"""用手动时钟和假器官检查 QQ 表情的文字与语义选择。"""

from datetime import UTC, datetime, timedelta
from io import BytesIO
from itertools import pairwise
from pathlib import Path

import pytest
from PIL import Image, ImageChops

from aureus.common import HeartbeatConfig, ManualClock
from aureus.heartbeat.emoji_selection import EmojiSelector
from aureus.heartbeat.planning import EmojiIntent, parse_plan
from aureus.heartbeat.reply_outbox import EmojiReplyPart
from aureus.memory import EmojiStore
from aureus.organs import CallPurpose, ModelPreference, OrganRequest, OrganResponse
from aureus.safety import CallRejectedError

PNG = b"\x89PNG\r\n\x1a\n"


def synthetic_emoji(color: str) -> bytes:
    with Image.new("RGB", (100, 100), color) as image, BytesIO() as buffer:
        image.save(buffer, format="PNG")
        return buffer.getvalue()


@pytest.mark.asyncio
async def test_visual_grid_filters_candidates_and_maps_labels_to_the_same_order(
    tmp_path: Path,
) -> None:
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    store = EmojiStore(tmp_path / "test.db", tmp_path, clock)
    store.open()
    colors = {"red": (255, 0, 0), "blue": (0, 0, 255), "green": (0, 128, 0)}
    entries = {
        color: store.collect(synthetic_emoji(color), description=color, register=True)
        for color in colors
    }
    red_digest = entries["red"].digest
    assert red_digest is not None
    store.record_use(red_digest)
    banned = store.collect(synthetic_emoji("yellow"), description="禁用", register=True)
    assert banned.digest is not None
    store.set_status(banned.digest, is_registered=True, is_banned=True)
    removed = store.collect(synthetic_emoji("purple"), description="移除", register=True)
    assert store.set_removed([removed.entry_key], True)
    store.collect(synthetic_emoji("cyan"), description="未登记", register=False)
    store.collect(PNG + b"invalid", description="坏图", register=True)
    ordered = [entry for entry in store.candidates() if entry.description in colors]
    assert len(ordered) == 3 and ordered[-1].digest == red_digest
    calls = []

    async def call(request: OrganRequest) -> OrganResponse:
        calls.append(request)
        assert request.organ_id == "vlm" and request.purpose is CallPurpose.OPTIONAL
        assert request.model_preference is ModelPreference.CHEAPEST
        assert request.context == {"task": "emoji_visual_selection"}
        assert "2x2" in request.system_context and "范围是 1 到 3" in request.system_context
        assert "情绪意图：庆祝" in request.prompt and "候选总数: 3" in request.prompt
        assert request.media_type == "image/png" and request.media_bytes is not None
        with Image.open(BytesIO(request.media_bytes)) as sheet:
            assert sheet.size == (524, 524)
            badges = []
            for (left, top), entry in zip(((0, 0), (268, 0), (0, 268)), ordered, strict=True):
                assert sheet.getpixel((left + 128, top + 128)) == colors[entry.description]
                badge = sheet.crop((left + 14, top + 14, left + 78, top + 78)).convert("L")
                assert badge.getextrema() == (0, 255)
                badges.append(badge)
            assert all(
                ImageChops.difference(previous, current).getbbox() is not None
                for previous, current in pairwise(badges)
            )
            assert sheet.getpixel((396, 396)) == (255, 255, 255)
        return OrganResponse(request.organ_id, '{"emoji_index":3,"reason":"合成"}')

    selector = EmojiSelector(store, call, clock, HeartbeatConfig(qq_emoji_visual_enabled=True), UTC)
    selected = await selector.select(
        EmojiIntent("庆祝", True), (), "用户", ModelPreference.CHEAPEST, has_embedding=True
    )
    assert selected == EmojiReplyPart(red_digest, True)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        '{"emoji_index":0}',
        '{"emoji_index":-1}',
        '{"emoji_index":2}',
        "{}",
        '{"emoji_index":true}',
        '{"emoji_index":"1"}',
        '{"emoji_index":1.0}',
        "not JSON",
        '{"emoji_index":1}',
    ],
)
async def test_visual_no_selection_and_late_moderation_do_not_try_text(
    tmp_path: Path, answer: str
) -> None:
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    store = EmojiStore(tmp_path / "test.db", tmp_path, clock)
    store.open()
    entry = store.collect(synthetic_emoji("red"), description="庆祝", register=True)
    assert entry.digest is not None
    digest = entry.digest
    calls = []

    async def call(request: OrganRequest) -> OrganResponse:
        calls.append(request.organ_id)
        if answer == '{"emoji_index":1}':
            store.set_status(digest, is_registered=True, is_banned=True)
        return OrganResponse(request.organ_id, answer)

    config = HeartbeatConfig(qq_emoji_visual_enabled=True)
    selector = EmojiSelector(store, call, clock, config, UTC)
    result = await selector.select(
        EmojiIntent(), (), "用户", ModelPreference.PREFERRED, has_embedding=True
    )
    assert result is None
    assert calls == ["vlm"]
    store.set_status(digest, is_registered=False, is_banned=False)
    result = await selector.select(
        EmojiIntent(), (), "用户", ModelPreference.PREFERRED, has_embedding=True
    )
    assert result is None
    assert calls == ["vlm"]


@pytest.mark.asyncio
async def test_removed_emoji_is_not_selected_and_recovery_keeps_moderation(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 23, tzinfo=UTC))
    store = EmojiStore(tmp_path / "emoji.db", tmp_path, clock)
    store.open()
    active = store.collect(PNG + b"active", description="庆祝", register=True)
    banned = store.collect(PNG + b"banned", description="停用", register=True)
    assert active.digest and banned.digest
    store.set_status(banned.digest, is_registered=True, is_banned=True)
    calls = []

    async def call(request: OrganRequest) -> OrganResponse:
        calls.append(request)
        assert "庆祝" in request.prompt and "停用" not in request.prompt
        return OrganResponse(request.organ_id, '{"selected": 1}')

    selector = EmojiSelector(store, call, clock, HeartbeatConfig(), UTC)

    async def select() -> EmojiReplyPart | None:
        return await selector.select(
            EmojiIntent(), (), "测试者", ModelPreference.PREFERRED, has_embedding=False
        )

    assert await select() is not None
    keys = [active.entry_key, banned.entry_key]
    assert store.set_removed(keys, True)
    assert await select() is None and len(calls) == 1
    assert store.read_sendable(active.digest) is None
    assert store.read_catalog_media(active.digest) == PNG + b"active"
    removed, total = store.browse_page("", 0, 20, removed=True)
    assert total == 2 and all(entry.is_removed for entry in removed)
    assert store.set_removed(keys, False)
    assert await select() is not None and len(calls) == 2
    assert store.read_sendable(banned.digest) is None
    assert store.read_catalog_media(banned.digest) == PNG + b"banned"


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

    selector = EmojiSelector(store, call, clock, HeartbeatConfig(), UTC)

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
async def test_no_embedding_uses_text_and_skips_missing_or_invalid_selection(
    tmp_path: Path,
) -> None:
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
            intent, (), "用户", ModelPreference.PREFERRED, has_embedding=False
        )

    assert await select() is None
    assert calls == []
    for data in (b"first", b"second"):
        store.collect(PNG + data, description="表示喜欢", register=True)
    assert await select() is None
    assert await select() is None
    selection = await select()
    assert selection is not None and selection.before_text is False
