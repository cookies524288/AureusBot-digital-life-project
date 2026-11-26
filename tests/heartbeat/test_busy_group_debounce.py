"""D138：人多的群里消息不断时，防抖最多等到上限就开始规划，不能一直不回复。"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.common import HeartbeatConfig, ManualClock
from aureus.heartbeat import Heartbeat, Perception, TrustLevel
from aureus.heartbeat.inbox_attention import InboxResidence
from aureus.heartbeat.inbox_store import ConversationKey, InboxStore
from aureus.heartbeat.perception import MessageRoute
from aureus.identity import Identity
from aureus.safety import EmergencyStop, PermissionChecker
from aureus.timeline import TimelineDatabase, VisibilityScope
from tests.heartbeat.test_inbox_attention import Organs


@pytest.mark.asyncio
async def test_busy_group_gets_a_reply_within_the_debounce_max_wait(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 11, 26, 10, tzinfo=UTC))
    database = tmp_path / "aureus.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    store = InboxStore(database, clock)
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    organs = Organs()
    organs.plan = '{"reply":true,"intent":"回应","stay_in_conversation":true}'
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=organs,
        identity=Identity.model_validate(
            {
                "facts": {"name": "测试助手", "birthday": "2026-11-23"},
                "user": {
                    "name": "维护者",
                    "accounts": [{"platform": "qq", "id": "owner"}],
                },
                "persona": {"immutable": "温和。"},
            }
        ),
        config=HeartbeatConfig(inbox_debounce_enabled=True, split_reply_enabled=False),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    sent = []
    heart.attach_channel("qq", sent.append)
    heart.wake_up()

    def receive(text: str, *, sender: str = "visitor") -> None:
        result = store.add(
            Perception(
                "qq",
                sender,
                text,
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_USER if sender == "owner" else TrustLevel.VERIFIED_CONTACT,
                route=MessageRoute("bot", "group", "busy", text),
            )
        )
        assert result is not None
        heart.on_inbox_message(result)
        heart.beat()

    try:
        receive("暖场", sender="owner")
        clock.advance(timedelta(seconds=4))
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        assert len(sent) == 1
        sent.clear()

        # 每 2 秒一条，滑动防抖窗口永远不会结束；20 秒上限到了必须开始规划。
        for number in range(15):
            receive(f"第 {number} 条")
            clock.advance(timedelta(seconds=2))
            heart.beat()
            await asyncio.sleep(0)

        for _ in range(5):
            heart.beat()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        assert sent, "人多的群里连续来信时应当至少回复一次"
    finally:
        await heart.shut_down()
        timeline.close()


def test_interrupt_keeps_the_restart_budget(tmp_path: Path) -> None:
    """D138：中断当前会话不能把重开计数清零，否则优先来信会反复重置上限。"""
    database = tmp_path / "aureus.db"
    clock = ManualClock(datetime(2026, 11, 26, 10, tzinfo=UTC))
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    store = InboxStore(database, clock)
    stop = EmergencyStop(tmp_path / "halt.json", clock)
    heart = Heartbeat(
        clock=clock,
        timeline=timeline,
        organs=Organs(),
        identity=Identity.model_validate(
            {
                "facts": {"name": "测试助手", "birthday": "2026-11-23"},
                "user": {"name": "维护者"},
                "persona": {"immutable": "温和。"},
            }
        ),
        config=HeartbeatConfig(inbox_debounce_enabled=True, split_reply_enabled=False),
        permissions=PermissionChecker(stop),
        emergency_stop=stop,
        inbox_store=store,
    )
    try:
        inbox = heart._platform_inbox
        assert inbox is not None
        key = ConversationKey("qq", "bot", "group", "busy")
        inbox.residence = InboxResidence(key, 35)
        heart._planner_restarts[key] = 3
        heart._interrupt_inbox(inbox, "测试抢占")
        assert inbox.residence is None
        assert heart._planner_restarts[key] == 3
    finally:
        timeline.close()
