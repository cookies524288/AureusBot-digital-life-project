"""2.3 激素的验收（D69）：被冒犯后生气并回击，过一阵平复，问得出原因，刷屏有上限，判断失败不动。"""

from datetime import timedelta

import pytest

from aureus.common import HormonesConfig
from aureus.hormones import HormoneKind
from tests.heartbeat.test_hormones_behavior import ANGRY, Env, _judgment
from tests.heartbeat.test_hormones_behavior import env as env  # fixture

pytestmark = pytest.mark.asyncio

EVENT_HORMONE_TRIGGERED = "hormone_triggered"
ANGER_HALF_LIFE_SECONDS = 600.0
HALF_LIVES_TO_CALM = 8


async def test_offense_makes_it_angry_then_it_calms_down_and_can_say_why(env: Env) -> None:
    rig, hormones = env.build([_judgment(1.0), _judgment(0.0), _judgment(0.0)], config=ANGRY)
    assert hormones is not None

    await rig.exchange("你这个破 AI")

    # D110：判断在回复之后才被采纳，所以本轮先把怒气记下，下一轮的回答才带情绪。
    assert hormones.level(HormoneKind.ANGER, env.clock.now()) >= ANGRY.retaliate_at
    replyer = [r for r in rig.organs.requests if r.organ_id == "replyer"][-1]
    assert "允许质疑、反驳" not in replyer.system_context

    await rig.exchange("还骂你")
    replyer = [r for r in rig.organs.requests if r.organ_id == "replyer"][-1]
    assert "允许质疑、反驳" in replyer.system_context
    assert "主要原因" in replyer.system_context

    # 过几个半衰期：怒气回落到几乎为零
    env.clock.advance(timedelta(seconds=ANGER_HALF_LIFE_SECONDS * HALF_LIVES_TO_CALM))
    assert hormones.level(HormoneKind.ANGER, env.clock.now()) < 0.01

    # 平复后再说话：回击说明消失
    await rig.exchange("早")
    replyer = [r for r in rig.organs.requests if r.organ_id == "replyer"][-1]
    assert "允许质疑" not in replyer.system_context


async def test_spamming_offenses_stays_under_the_cap(env: Env) -> None:
    config = HormonesConfig(enabled=True)
    rig, hormones = env.build([_judgment(1.0)] * 10, config=config)
    assert hormones is not None

    for index in range(10):
        await rig.exchange(f"破 AI {index}")

    assert hormones.level(HormoneKind.ANGER, env.clock.now()) <= 1.0
    # 同一个人反复冒犯，后面的上涨按 repeat_decay 递减，不会每次都涨满
    raises = [r.metadata["rise"] for r in rig.records(EVENT_HORMONE_TRIGGERED)]
    assert raises == sorted(raises, reverse=True)
    assert raises[-1] < raises[0]


async def test_a_failed_judgment_leaves_the_hormones_untouched(env: Env) -> None:
    rig, hormones = env.build(["完全不是 JSON"], config=ANGRY)
    assert hormones is not None

    await rig.exchange("你这个破 AI")

    assert hormones.level(HormoneKind.ANGER, env.clock.now()) == 0
    assert rig.records(EVENT_HORMONE_TRIGGERED) == []
    assert len(rig.replies()) == 1  # 回复照常
