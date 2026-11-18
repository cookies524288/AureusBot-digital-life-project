"""给大模型看的身体状态摘要（04 号文档第 6 节，D58）。

给的是事实，不是表演指令：数字和原因都来自身体读数，行为影响由代码执行，
大模型只在被问起时如实说。这里只拼文字，不读写任何状态。
关掉的信号不写进去：不能把「没在测」说成「一切正常」。
"""

from aureus.body import BodyReading, EnergyReading
from aureus.common import BodyConfig

_HEADER = "【身体状态】"
_HONESTY_NOTE = (
    "以上是你真实的身体状态，都来自账本和时钟的实际记录，不是你想象出来的。"
    "被问起、或者聊到相关的事时，可以用自然的口吻说出来，但只能说上面有的数字和原因："
    "不要夸大，也不要编造这里没有的感受。没人问起就不必主动提。"
    "你的行为（回复快慢、用什么模型）已经由程序按这些状态调整好了，不需要你再演出来。"
)


def describe_body_state(reading: BodyReading, config: BodyConfig, reply_approach: str = "") -> str:
    """把一份身体读数写成放进系统上下文的一段话，末尾带上如实说的要求。

    Args:
        reading: 此刻的身体读数。
        config: 身体配置（说明里要用窗口长度、夜间时段）。
        reply_approach: 这次回复的做法（ReplyStrategy.describe 的结果）；空字符串表示不写。
            它是精力档位的直接结果，和身体读数放在一起，被问起时才能说得对得上。

    Returns:
        「【身体状态】」开头的几行字；所有信号都关着时仍有一行说明，免得它以为没有这回事。
    """
    lines = [_HEADER]
    lines.append(_describe_energy(reading.energy))
    fatigue, stress, sleep = reading.fatigue, reading.stress, reading.sleep
    if fatigue.enabled:
        level = "偏高" if fatigue.high else "不高"
        lines.append(
            f"疲劳 {fatigue.level:.0%}（{level}）：最近 {config.fatigue_window_minutes} 分钟"
            f"调用了 {fatigue.calls_in_window} 次，满额按 {config.fatigue_full_calls} 次算"
        )
    if stress.enabled:
        level = "偏高" if stress.high else "不高"
        lines.append(
            f"压力 {stress.level:.0%}（{level}）：最近 {stress.sampled_calls} 次调用失败了 "
            f"{stress.failed_calls} 次，排队的消息 {stress.backlog} 条"
        )
        if stress.recent_failure_reasons:
            lines.append("最近的失败原因：" + "；".join(stress.recent_failure_reasons))
    if sleep.enabled:
        lines.append(
            f"现在{'是' if sleep.night else '不是'}夜间时段"
            f"（当地 {config.night_start_hour} 点到 {config.night_end_hour} 点）"
        )
    if reply_approach:
        lines.append(f"这次回复的做法：{reply_approach}")
    lines.append(_HONESTY_NOTE)
    return "\n".join(lines)


def _describe_energy(energy: EnergyReading) -> str:
    if not energy.limited:
        return "精力 不限：没有按每日额度计算（没设月度 token 上限，或精力信号已关闭）"
    assert energy.fraction is not None  # limited 为真时一定有数字
    return (
        f"精力 {energy.fraction:.0%}（{energy.tier.value}档）："
        f"今日额度 {energy.daily_quota:,} token，已用 {energy.used_today:,}，"
        f"还剩 {energy.remaining_today:,}；"
        f"本月还剩 {energy.month_remaining:,} token、{energy.days_left_in_month} 天"
    )
