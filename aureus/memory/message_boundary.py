"""按原件来源区分平台消息和长期记忆，不用称呼或正文判断归属。"""

MESSAGE_EVENT_TYPE = "message"
MESSAGE_SOURCE_TABLES = ("mai_messages", "messages")


def is_message_source(source_key: str) -> str:
    """返回识别迁入消息的 SQL 条件；列名由调用方固定。"""
    tables = ", ".join(f"'{table}'" for table in MESSAGE_SOURCE_TABLES)
    return f"json_extract({source_key}, '$[2]') IN ({tables})"


def is_message_excerpt(candidate: str, original: str) -> bool:
    """拒绝既有提取词中照抄的正文；仅与已绑定的那一条来源比较。"""
    candidate = " ".join(candidate.casefold().split())
    original = " ".join(original.casefold().split())
    return bool(candidate and original and (candidate in original or original in candidate))
