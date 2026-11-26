"""D142：消息防抖控制器的等待档位与特殊规则。"""

import pytest

from aureus.heartbeat.debounce import (
    BARE_IMAGE_MIN_WAIT,
    BATCH_LIMIT,
    MENTION_LIMIT,
    QUIET_THRESHOLD,
    DebounceMode,
    MessageDebouncer,
)

_BASE = 100.0


def _observe(
    debouncer: MessageDebouncer,
    *,
    author: str = "a",
    is_bare_image: bool = False,
    is_pure_emoji: bool = False,
    at_bot: bool = False,
    received_at: float | None = None,
    now: float = _BASE,
    mode: DebounceMode = "steady",
) -> str:
    return debouncer.observe(
        author=author,
        is_bare_image=is_bare_image,
        is_pure_emoji=is_pure_emoji,
        at_bot=at_bot,
        received_at=now if received_at is None else received_at,
        wall_time=now,
        now=now,
        mode=mode,
    )


@pytest.mark.parametrize(
    ("mode", "quiet", "active", "same_author", "different_author"),
    [
        ("fast", 1.0, 3.0, 1.0, 0.5),
        ("natural", 2.0, 5.0, 2.0, 0.5),
        ("steady", 4.0, 7.0, 4.0, 2.0),
    ],
)
def test_each_mode_uses_its_own_waits(
    mode: DebounceMode,
    quiet: float,
    active: float,
    same_author: float,
    different_author: float,
) -> None:
    debouncer = MessageDebouncer()
    assert "安静" in _observe(debouncer, now=_BASE, mode=mode)
    assert debouncer.deadline == pytest.approx(_BASE + quiet)

    debouncer.consume()
    assert "活跃" in _observe(debouncer, now=_BASE + 10, mode=mode)
    assert debouncer.deadline == pytest.approx(_BASE + 10 + active)

    assert "同人续发" in _observe(debouncer, author="a", now=_BASE + 11, mode=mode)
    assert debouncer.deadline == pytest.approx(_BASE + 11 + same_author)

    debouncer.consume()
    assert "活跃" in _observe(debouncer, author="a", now=_BASE + 20, mode=mode)
    assert "换人" in _observe(debouncer, author="b", now=_BASE + 21, mode=mode)
    assert debouncer.deadline == pytest.approx(_BASE + 21 + different_author)


def test_quiet_threshold_is_thirty_seconds() -> None:
    debouncer = MessageDebouncer()
    _observe(debouncer, now=_BASE)
    debouncer.consume()

    # 正好 30 秒仍算活跃；超过 30 秒才算安静开批。
    assert "活跃" in _observe(debouncer, now=_BASE + QUIET_THRESHOLD)
    debouncer.consume()
    assert "安静" in _observe(debouncer, now=_BASE + QUIET_THRESHOLD + 31)


def test_bare_image_opens_with_at_least_four_seconds() -> None:
    debouncer = MessageDebouncer()
    reason = _observe(debouncer, is_bare_image=True, now=_BASE, mode="fast")
    assert "裸图" in reason
    assert debouncer.deadline == pytest.approx(_BASE + BARE_IMAGE_MIN_WAIT)


def test_pure_emoji_does_not_extend_but_can_pull_the_deadline_earlier() -> None:
    debouncer = MessageDebouncer()
    _observe(debouncer, author="a", now=_BASE)
    assert debouncer.deadline == pytest.approx(_BASE + 4)

    # 同人纯表情：本来会续到 +5 秒，但纯表情包不能延长已有批次。
    reason = _observe(debouncer, author="a", is_pure_emoji=True, now=_BASE + 1)
    assert "纯表情包" in reason
    assert debouncer.deadline == pytest.approx(_BASE + 4)

    # 换人的纯表情按换人等待，可以比原截止时间更早。
    _observe(debouncer, author="b", is_pure_emoji=True, now=_BASE + 1)
    assert debouncer.deadline == pytest.approx(_BASE + 3)


def test_batch_limit_is_eight_seconds_from_the_first_arrival() -> None:
    debouncer = MessageDebouncer()
    _observe(debouncer, now=_BASE)
    for offset in (2.0, 4.0, 6.0, 8.0):
        _observe(debouncer, now=_BASE + offset)
        assert debouncer.deadline == pytest.approx(min(_BASE + offset + 4, _BASE + BATCH_LIMIT))
    # 已到期的新消息不延长批次。
    assert "已到期" in _observe(debouncer, now=_BASE + BATCH_LIMIT + 1)
    assert debouncer.deadline == pytest.approx(_BASE + BATCH_LIMIT)


def test_mention_caps_at_one_second_and_keeps_the_earliest_limit() -> None:
    debouncer = MessageDebouncer()
    _observe(debouncer, now=_BASE)
    assert debouncer.deadline == pytest.approx(_BASE + 4)

    reason = _observe(debouncer, at_bot=True, now=_BASE + 0.5)
    assert "1 秒" in reason
    assert debouncer.deadline == pytest.approx(_BASE + MENTION_LIMIT + 0.5)

    # 连续提及取最早的上限，后面的提及不能把上限往后推。
    _observe(debouncer, at_bot=True, now=_BASE + 0.6)
    assert debouncer.deadline == pytest.approx(_BASE + MENTION_LIMIT + 0.5)

    # 批次已到期后，新的提及也不延长。
    _observe(debouncer, at_bot=True, now=_BASE + 2)
    assert debouncer.deadline == pytest.approx(_BASE + MENTION_LIMIT + 0.5)


def test_expired_batch_is_not_extended_by_new_messages() -> None:
    debouncer = MessageDebouncer()
    _observe(debouncer, now=_BASE)
    reason = _observe(debouncer, now=_BASE + 5)
    assert "已到期" in reason
    assert debouncer.deadline == pytest.approx(_BASE + 4)


def test_consume_keeps_the_last_arrival_and_author() -> None:
    debouncer = MessageDebouncer()
    _observe(debouncer, author="a", now=_BASE)
    debouncer.consume()
    assert debouncer.deadline is None

    # 消费后仍记得上一条的时间和发言人：2 秒后是新批次的活跃开批。
    assert "活跃" in _observe(debouncer, author="b", now=_BASE + 2)
    assert debouncer.deadline == pytest.approx(_BASE + 2 + 7)

    debouncer.consume()
    # 距上一条 38 秒，超过安静阈值，下一批按安静开批。
    assert "安静" in _observe(debouncer, author="b", now=_BASE + 40)
