"""用同一个合成网页窗口对照验证整理开关、预算与来源授权。"""

from datetime import timedelta
from pathlib import Path

import pytest

from aureus.body import Body, EnergyTier
from aureus.common import BodyConfig, MemoryConfig
from aureus.organs import CallAttempt
from aureus.safety import Ledger
from aureus.timeline import ActorType, VisibilityScope
from tests.heartbeat.test_recall_flow import MemoryEnv


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["allowed", "disabled", "no_extract", "overdraft", "no_web"])
async def test_platform_summary_respects_gates(tmp_path: Path, gate: str) -> None:
    env = MemoryEnv(tmp_path)
    ledger = Ledger(env.database, env.clock, calendar=env.calendar)
    ledger.open()
    try:
        env.clock.advance(timedelta(hours=3))  # 测试时钟从当地 20 点进入夜间。
        body = Body(BodyConfig(night_start_hour=23), 1000, ledger, env.clock, env.calendar)
        if gate == "overdraft":
            ledger.record("replyer", [CallAttempt("offline", "fixture", 1000, 0)])
        assert body.read_sleep().night
        assert (body.read_energy().tier is EnergyTier.OVERDRAFT) == (gate == "overdraft")
        heart = env.build(
            MemoryConfig(
                consolidation_enabled=gate != "disabled",
                consolidation_extract_enabled=gate != "no_extract",
                consolidation_batch=1,
            ),
            body=body,
        )
        if gate != "no_web":
            heart.attach_channel("web", env.sent.append)
        heart.update_available_organs(frozenset({"summarizer"}))
        env.timeline.append(
            "message",
            "合成的网页来信",
            ActorType.USER,
            actor_id="local",
            visibility=VisibilityScope.USER_ONLY,
            metadata={"channel": "web", "trust": "verified_user"},
        )
        await heart.beat()
        await heart.wait_for_thought()
        assert [r.context.get("task") for r in env.organs.requests] == (
            ["memory_consolidation"] if gate == "allowed" else []
        )
    finally:
        for heart in env.rigs:
            await heart.shut_down()
        for store in (env.index, env.findings, env.people, env.timeline, ledger):
            store.close()
