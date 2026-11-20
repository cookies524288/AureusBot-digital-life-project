"""主动联系的发送顺序与故障恢复回归：只用假器官和临时数据库（D76）。"""

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from aureus.feedback import PROACTIVE_CONTACT
from aureus.heartbeat import EVENT_INITIATIVE_CONSIDERED, EVENT_INITIATIVE_RESULT
from tests.heartbeat.test_initiative_flow import Env, finish, submit

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    opened = Env(tmp_path)
    yield opened
    for rig in opened.rigs:
        await rig.heartbeat.shut_down()
    for store in reversed(opened.closers):
        store.close()


async def test_queued_user_message_before_send_is_not_an_answer(env: Env) -> None:
    rig = env.build()
    await rig.exchange("你好")
    env.clock.advance(timedelta(hours=20))
    rig.heartbeat.beat()
    await rig.heartbeat.wait_for_thought()
    assert not rig.records(EVENT_INITIATIVE_CONSIDERED)
    submit(rig)  # 用户来信先进入队列，此时主动消息还没有发出。
    rig.heartbeat.beat()
    assert rig.records(EVENT_INITIATIVE_CONSIDERED)[-1].metadata["sent"] is True
    assert not rig.records(EVENT_INITIATIVE_RESULT)
    assert env.feedback.confidence(PROACTIVE_CONTACT).samples == 0
    await finish(rig)
    assert not rig.records(EVENT_INITIATIVE_RESULT)

    # 时钟不动也能区分前后；只有发送后收到的新来信才能结算这次联系。
    await rig.exchange("我在")
    [result] = rig.records(EVENT_INITIATIVE_RESULT)
    assert result.metadata["answered"] is True
    assert env.feedback.confidence(PROACTIVE_CONTACT).samples == 1
