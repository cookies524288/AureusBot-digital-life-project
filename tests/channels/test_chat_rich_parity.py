"""从合成 QQ 入站到聊天上下文：平台表情、转发和卡片的可见证据。"""

import json
from pathlib import Path
from typing import Any

import pytest

from aureus.common import ManualClock
from tests.channels.test_qq_behavior_replay import START, open_replay
from tests.channels.test_qq_runtime import GROUP, event
from tests.prompt_assertions import dialogue_text

pytestmark = pytest.mark.asyncio


def node(speaker: str, body: str | list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "node", "data": {"nickname": speaker, "content": body}}


@pytest.mark.parametrize("nested", [False, True])
async def test_forward_attributes_the_actual_speakers_without_a_second_outgoing_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested: bool
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        content = [node("纸船", "改成星期四"), node("风筝", "我只带一盏灯")]
        if nested:
            content = [node("转述人", [{"type": "forward", "data": {"content": content}}])]
        replay.planner.plan = {"reply": True, "intent": "辨别转发里的发言人"}
        await replay.receive(
            event(
                8101,
                2811,
                "",
                group=GROUP,
                message=[
                    {"type": "forward", "data": {"content": content}},
                    {"type": "text", "data": {"text": "转发里是谁说改期？"}},
                ],
            )
        )
        await replay.step()
        await replay.assert_sent()
        for organ in (replay.planner, replay.replyer):
            prompt = dialogue_text(organ.calls[-1][1])
            assert "纸船：改成星期四" in prompt and "风筝：我只带一盏灯" in prompt
            assert "2811" in prompt
        assert replay.fake.messages.empty()


async def test_title_only_card_adds_no_invented_chapter_or_tracking_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        card = {
            "meta": {
                "news": {
                    "title": "矿石里的微光",
                    "jumpUrl": "https://unused.invalid/private-tracking",
                    "unrelated": "这不是字幕",
                }
            }
        }
        await replay.receive(
            event(
                8102,
                2812,
                "",
                group=GROUP,
                message=[
                    {"type": "json", "data": {"data": json.dumps(card, ensure_ascii=False)}},
                ],
            )
        )
        await replay.step()
        prompt = dialogue_text(replay.planner.calls[-1][1])
        assert "矿石里的微光" in prompt
        assert "private-tracking" not in prompt and "这不是字幕" not in prompt
        assert not replay.replyer.calls and replay.fake.messages.empty()


async def test_native_face_preserves_its_position_between_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        await replay.receive(
            event(
                8103,
                2813,
                "",
                group=GROUP,
                message=[
                    {"type": "text", "data": {"text": "看到星星"}},
                    {"type": "face", "data": {"id": 264}},
                    {"type": "text", "data": {"text": "忘带地图"}},
                ],
            )
        )
        await replay.step()
        assert "看到星星[捂脸]忘带地图" in dialogue_text(replay.planner.calls[-1][1])
        assert not replay.replyer.calls and replay.fake.messages.empty()


async def test_visible_card_chapters_survive_a_followup_but_an_external_url_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        card = {
            "meta": {
                "detail": {
                    "title": "陶器烧制",
                    "chapters": [{"title": "拉坯"}, {"title": "上釉"}],
                    "captions": [{"text": "等釉层晾干"}],
                    "jumpUrl": "https://unused.invalid/ad-id",
                }
            }
        }
        await replay.receive(
            event(
                8104,
                2814,
                "",
                group=GROUP,
                message=[
                    {"type": "json", "data": {"data": json.dumps(card, ensure_ascii=False)}},
                ],
            )
        )
        await replay.step()
        assert replay.fake.messages.empty()
        replay.planner.plan = {"reply": True, "intent": "只按卡片已给的备注明确第二段"}
        await replay.receive(event(8105, 2814, "刚才的视频第二段讲什么？", group=GROUP))
        await replay.step()
        await replay.assert_sent()
        for organ in (replay.planner, replay.replyer):
            prompt = dialogue_text(organ.calls[-1][1])
            assert prompt.index("拉坯") < prompt.index("上釉")
            assert "等釉层晾干" in prompt and "ad-id" not in prompt
        assert replay.fake.messages.empty()
