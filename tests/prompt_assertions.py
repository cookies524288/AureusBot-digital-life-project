"""为对话流测试读取模型看到的消息正文。"""

from aureus.organs import OrganRequest, assemble_messages


def dialogue_text(request: OrganRequest) -> str:
    """按发送顺序拼出所有非 system 消息，方便检查具体材料。"""
    return "\n".join(message["content"] for message in assemble_messages(request)[1:])
