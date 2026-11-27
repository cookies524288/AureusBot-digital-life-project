"""群聊复读只认不同群友确实重复的短消息，不把任意附件或 CQ 文本作为动作。"""

from aureus.heartbeat.inbox_store import InboxMessage, InboxStore
from aureus.heartbeat.perception import Perception


def followup_emotion(text: str) -> str:
    """只给有明确轻松情绪的正文建议一次选图；普通说明不额外打断。"""
    for emotion, markers in (
        ("高兴", ("哈哈", "嘿嘿", "开心", "太好了", "太棒了", "好耶", "😂", "🎉")),
        ("感谢", ("谢谢", "多谢", "辛苦啦", "感激")),
        ("拥抱安慰", ("抱抱", "抱一下", "拍拍你")),
    ):
        if any(marker in text for marker in markers):
            return emotion
    return ""


def repeat_signature(source: Perception) -> tuple[str, str] | None:
    """只比较单张原图、原生表情或简短纯文字，不复读指令或混合消息。"""
    parts = source.components
    if source.attachments:
        if len(parts) != 1 or parts[0].kind != "image" or len(source.attachments) != 1:
            return None
        attachment = source.attachments[0]
        return ("image", attachment.file_id) if attachment.kind == "image" else None
    if len(parts) == 1 and parts[0].kind == "face" and parts[0].emoji_id:
        return "face", parts[0].emoji_id
    text = source.content.strip()
    if (
        all(item.kind == "text" for item in parts)
        and 0 < len(text) <= 64
        and "\n" not in text
        and not text.startswith(("/", "!", "["))
    ):
        return "text", text
    return None


def group_repeat(store: InboxStore, source: InboxMessage) -> tuple[str, str] | None:
    """确认相邻原件来自同一群内不同成员，且确实说了同样的话。"""
    key = source.conversation
    if key.kind != "group" or source.perception.sender_id == key.account_id:
        return None
    signature = repeat_signature(source.perception)
    previous = store.nearby_before(source, 1)
    if not previous or previous[0].perception.sender_id in (
        source.perception.sender_id,
        key.account_id,
    ):
        return None
    return signature if repeat_signature(previous[0].perception) == signature else None
