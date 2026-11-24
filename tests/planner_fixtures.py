"""把已有合成场景意图编成真实规划器工具响应。"""

import json
from typing import Any

from aureus.organs import OrganRequest, OrganResponse, ToolCall


def plan_response(request: OrganRequest, fixture: str | dict[str, Any]) -> OrganResponse:
    """无效的旧夹具继续模拟普通文本，绝不产生发言工具。"""
    if isinstance(fixture, str):
        try:
            data = json.loads(fixture)
        except ValueError:
            return OrganResponse(request.organ_id, fixture)
    else:
        data = fixture.copy()
    if not isinstance(data, dict):
        return OrganResponse(request.organ_id, "不是规划对象")
    reply = data.pop("reply", None)
    keywords = data.pop("recall", None)
    if reply is True and keywords is not None and not request.tool_history:
        call = ToolCall("query_memory", "synthetic_query", {"keywords": keywords})
    elif reply is True:
        data.setdefault("addressed_to_me", "yes")
        data.setdefault("reply_reference", data.get("intent"))
        call = ToolCall("reply", "synthetic_reply", data)
    elif reply is False:
        attention = {
            key: data[key]
            for key in ("read_inbox", "special_contact", "stay_in_conversation")
            if key in data
        }
        call = ToolCall("inbox_attention" if attention else "stop", "synthetic_stop", attention)
    else:
        return OrganResponse(request.organ_id, json.dumps(data))
    return OrganResponse(request.organ_id, "", tool_calls=(call,))
