"""公共地基 common：时钟。

所有子系统取"现在几点"，都从时钟对象取，不直接读系统时间（见 D15、D26）。
这样实验时可以换成加速时钟，测试时可以换成手动时钟。

时间一律是带时区的 UTC：给人看时再转成本地时间。传入不带时区的时间直接报错，不去猜。
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol


def ensure_aware(moment: datetime) -> datetime:
    """确认时间带时区，并统一换算成 UTC。

    Args:
        moment: 任意带时区的时间。

    Returns:
        同一时刻、时区为 UTC 的时间。

    Raises:
        ValueError: 传入的时间不带时区。
    """
    # 为什么报错而不是默认当成 UTC 或本地时间：猜错了不会有任何提示，
    # 时间线上的记录就悄悄偏了几小时。见 D26。
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"时间必须带时区，但收到了不带时区的时间：{moment!r}")
    return moment.astimezone(UTC)


class Clock(Protocol):
    """时钟接口：只有一个方法 now()。

    语法：Protocol 是"只看形状"的接口（PEP 544）。任何类，只要有一个返回 datetime 的 now() 方法，
    就算是 Clock，不需要显式继承。
    """

    def now(self) -> datetime:
        """返回当前时间，带时区，UTC。"""
        ...


class SystemClock:
    """真实时钟：读电脑的系统时间。

    这是整个项目里唯一允许读系统时间的地方（见 D26）。
    读的是系统时间，不是匀速计时器：电脑休眠醒来后，时间会往前跳，
    睡眠信号要靠这个跳变发现休眠（见 docs/04-body-signals.md 第 5 节）。
    """

    def now(self) -> datetime:
        """返回系统当前时间，带时区，UTC。"""
        return datetime.now(UTC)


class ManualClock:
    """手动时钟：时间只在你拨动时才变化，测试用。"""

    def __init__(self, start: datetime) -> None:
        """创建手动时钟。

        Args:
            start: 起始时间，必须带时区。
        """
        self._current = ensure_aware(start)

    def now(self) -> datetime:
        """返回当前时间，带时区，UTC。"""
        return self._current

    def advance(self, delta: timedelta) -> None:
        """把时间往后拨。

        Args:
            delta: 要拨过去的时长，不能是负数。

        Raises:
            ValueError: delta 是负数。
        """
        if delta < timedelta(0):
            raise ValueError(f"手动时钟只能往后拨，但收到了负的时长：{delta!r}")
        self._current += delta


class AcceleratedClock:
    """加速时钟：按倍数加快时间流逝，实验用。

    比如速度 3600：真实过 1 秒，它的时间过 1 小时。
    它自己不读系统时间，而是向另一个时钟（通常是 SystemClock）问"真实过了多久"，
    所以测试里可以传入 ManualClock 来充当真实时间，不用真的等。
    """

    def __init__(self, start: datetime, speed: float, real_clock: Clock) -> None:
        """创建加速时钟。

        Args:
            start: 加速时间的起点，必须带时区。
            speed: 速度倍数，必须大于 0。1 表示和真实时间一样快。
            real_clock: 提供真实时间的时钟。

        Raises:
            ValueError: speed 不是正数。
        """
        if speed <= 0:
            raise ValueError(f"加速倍数必须大于 0，但收到了 {speed}")
        self._start = ensure_aware(start)
        self._speed = speed
        self._real_clock = real_clock
        # 记下创建这一刻的真实时间，之后算"真实过了多久"都以它为起点。
        self._real_start = real_clock.now()

    def now(self) -> datetime:
        """返回加速后的当前时间，带时区，UTC。"""
        real_elapsed = self._real_clock.now() - self._real_start
        # TODO（用户来写）：换算公式 = 起点时间 + 真实经过的时间 × 速度。
        # 提示：real_elapsed 是 timedelta；timedelta 可以乘以数字，datetime 可以加 timedelta。
        # 写好后，用 uv run pytest tests/common/test_clock.py -q 验证。
        return self._start + real_elapsed * self._speed
