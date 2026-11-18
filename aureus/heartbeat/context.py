"""给器官的上下文：身份、人设、现在几点、醒来信息、最近的对话（D41 第 8 条）。

给大模型的是事实，不是表演指令（04 号文档第 6 节）。Phase 0 一个器官兼做规划和回复，
所以四层人设全部放进去（03 号文档“身份”一节的注入规则）。这里只拼文字，不读写任何状态。
"""

from collections.abc import Sequence
from datetime import date, datetime, tzinfo

from aureus.common.time_format import format_local_time
from aureus.heartbeat.downtime import Downtime
from aureus.identity import Identity
from aureus.timeline import ActorType, TimelineRecord

# 人设后三层的标题；不变人格单独处理，因为它必填。
_OPTIONAL_PERSONA_LAYERS = (
    ("可变人格", "mutable"),
    ("行为风格", "behavior_style"),
    ("表达风格", "expression_style"),
)


def age_on(birthday: str, today: date) -> int | None:
    """按生日算出今天几岁。

    Args:
        birthday: identity.toml 里的出生日期，YYYY-MM-DD。
        today: 今天（本地日期）。

    Returns:
        周岁；生日写得不对、或者在今天之后时返回 None，不去猜。
    """
    try:
        born = date.fromisoformat(birthday.strip())
    except ValueError:
        return None
    if born > today:
        return None
    had_birthday_this_year = (today.month, today.day) >= (born.month, born.day)
    return today.year - born.year - (0 if had_birthday_this_year else 1)


def describe_aliases(name: str, alias_names: Sequence[str]) -> str:
    """把昵称写成一句话；没有昵称时返回空字符串。

    去掉首尾空白、空的、和名字相同的、重复的，保留用户填写的顺序：
    昵称是用户手填的，多一个空格或重复一遍不该让提示词里出现怪字。

    Args:
        name: 它的名字。
        alias_names: identity.toml 里的昵称列表。

    Returns:
        比如“大家也叫你“阿绿”、“绿宝”，这些都是在叫你。”
    """
    seen = {name.strip()}
    aliases: list[str] = []
    for alias in alias_names:
        cleaned = alias.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            aliases.append(cleaned)
    if not aliases:
        return ""
    quoted = "、".join(f"“{alias}”" for alias in aliases)
    return f"大家也叫你{quoted}，这些都是在叫你。"


def build_system_context(
    identity: Identity,
    now: datetime,
    downtime: Downtime | None,
    timezone: tzinfo | None = None,
    body_state: str = "",
) -> str:
    """拼出放在系统提示词最前面的背景。

    Args:
        identity: 身份事实和四层人设。
        now: 现在（从时钟取）。
        downtime: 这次醒来前停了多久；还没醒来时传 None。
        timezone: 显示用的时区；不给就用这台电脑设置的时区。
        body_state: 身体状态摘要（body_summary.describe_body_state 的结果），空字符串表示不放。

    Returns:
        几段中文，段与段之间空一行。
    """
    facts = identity.facts
    profile = [f"你是{facts.name}。"]
    aliases = describe_aliases(facts.name, facts.alias_names)
    if aliases:
        profile.append(aliases)
    if facts.gender.strip():
        profile.append(f"性别：{facts.gender.strip()}。")
    age = age_on(facts.birthday, now.astimezone(timezone).date())
    if age is None:
        profile.append(f"生日：{facts.birthday}。")
    else:
        profile.append(f"生日：{facts.birthday}，今年 {age} 岁。")
    profile.append(f"和你说话的是你的用户，你称呼对方“{identity.user.name}”。")

    sections = ["".join(profile), f"【不变人格】\n{identity.persona.immutable.strip()}"]
    for title, field_name in _OPTIONAL_PERSONA_LAYERS:
        text = str(getattr(identity.persona, field_name)).strip()
        if text:
            sections.append(f"【{title}】\n{text}")

    sections.append(f"现在是 {format_local_time(now, timezone)}。")
    if downtime is not None:
        woke = format_local_time(downtime.woke_at, timezone)
        sections.append(
            f"你这次在 {woke} 醒来。{downtime.describe(timezone)}停机期间你确实没有在运行。"
        )
    if body_state:
        sections.append(body_state)
    return "\n\n".join(sections)


def _speaker(record: TimelineRecord, user_name: str) -> str:
    if record.actor_type == ActorType.ME:
        return "你"
    if record.actor_type == ActorType.USER:
        return user_name
    return record.actor_id or str(record.actor_type)


def _dialogue_line(record: TimelineRecord, user_name: str, timezone: tzinfo | None) -> str:
    # 对话可能跨好几天（中间停过机），所以带上日期。
    moment = record.timestamp.astimezone(timezone)
    return f"[{moment:%m-%d %H:%M}] {_speaker(record, user_name)}：{_one_line(record.content)}"


def _one_line(text: str) -> str:
    """把内容压成一行：每个换行换成两个字符 反斜杠加 n，免得有人发多行消息伪造一句我说过的话。"""
    return "\\n".join(text.splitlines())


def build_reply_prompt(
    history: Sequence[TimelineRecord],
    new_messages: Sequence[TimelineRecord],
    user_name: str,
    timezone: tzinfo | None = None,
) -> str:
    """拼出回复用的提示词：之前的对话，加上刚收到、还没回复的消息。

    Args:
        history: 之前的对话，从早到晚。
        new_messages: 这次要回复的消息，从早到晚，至少一条。
        user_name: 怎么称呼用户。
        timezone: 显示用的时区；不给就用这台电脑设置的时区。

    Returns:
        提示词。

    Raises:
        ValueError: 没有要回复的消息。
    """
    if not new_messages:
        raise ValueError("没有要回复的消息")
    parts: list[str] = []
    if history:
        lines = "\n".join(_dialogue_line(record, user_name, timezone) for record in history)
        parts.append(f"之前的对话（从早到晚）：\n{lines}")
    lines = "\n".join(_dialogue_line(record, user_name, timezone) for record in new_messages)
    parts.append(f"刚收到、还没回复的消息：\n{lines}")
    parts.append("请直接写出你要说的话，不要加名字或时间前缀。")
    return "\n\n".join(parts)
