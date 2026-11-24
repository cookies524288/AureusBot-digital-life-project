"""用合成发言测试总结协议，不依赖器官、数据库或平台连接。"""

import json

import pytest

from aureus.memory import SummaryMessage, SummaryWindow, parse_delta


def _window(*messages: SummaryMessage) -> SummaryWindow:
    return SummaryWindow(("synthetic-route", "group"), 0, messages)


def _statement(sequence: int, content: str, *, human: bool = True) -> SummaryMessage:
    return SummaryMessage(sequence, str(sequence), f"p{sequence}", f"甲{sequence}", content, human)


def _delta(
    value: str, source: int, excerpt: str, *, context: tuple[int, str] | None = None
) -> dict:
    fact = {
        "fact_id": "f1",
        "text": f"星舟项目将到{value}演练。",
        "predicate": "演练地点",
        "value": value,
        "source": {"message_id": str(source), "sender_id": f"p{source}", "excerpt": excerpt},
        "image_evidence": [],
        "shared": True,
    }
    if context is not None:
        fact["context"] = {
            "message_id": str(context[0]),
            "sender_id": f"p{context[0]}",
            "excerpt": context[1],
        }
    return {
        "summary": f"演练计划已经敲定，星舟项目的参与者届时前往{value}。",
        "entities": ["星舟项目", value],
        "relations": [{"subject": "星舟项目", "predicate": "演练地点", "object": value}],
        "facts": [fact],
    }


def test_narrative_with_partial_non_first_person_confirmation_and_named_title() -> None:
    long_tail = "这段不是引用。" * 160
    message = _statement(1, f"“星舟项目”演练定在海桐馆，安排已确认。\n{long_tail}")
    window = _window(message)
    payload = _delta("海桐馆", 1, "“星舟项目”演练定在海桐馆")
    delta = parse_delta(json.dumps(payload), window)
    assert delta.summary != delta.facts[0].text
    assert delta.relations[0].subject == "星舟项目"


def test_last_confirmation_replaces_explicitly_denied_value_across_speakers() -> None:
    window = _window(
        _statement(1, "星舟项目本来计划在晴桂馆演练。"),
        _statement(2, "星舟项目更正：不是晴桂馆而是栖松馆。"),
    )
    old = _delta("晴桂馆", 1, "星舟项目本来计划在晴桂馆演练")
    with pytest.raises(ValueError, match="否定"):
        parse_delta(json.dumps(old), window)
    new = _delta("栖松馆", 2, "星舟项目更正：不是晴桂馆而是栖松馆")
    assert parse_delta(json.dumps(new), window).facts[0].value == "栖松馆"
    new["summary"] += "旧地点是晴桂馆。"
    with pytest.raises(ValueError, match="旧值"):
        parse_delta(json.dumps(new), window)


def test_quoted_context_is_not_evidence_but_new_authored_reply_is() -> None:
    formal = "星舟项目演练改在栖松馆，通知已经发出"
    window = _window(_statement(1, f"> 星舟项目演练在晴桂馆\n{formal}。"))
    with pytest.raises(ValueError, match="正文"):
        parse_delta(json.dumps(_delta("晴桂馆", 1, "星舟项目演练在晴桂馆")), window)
    accepted = parse_delta(json.dumps(_delta("栖松馆", 1, formal)), window)
    assert accepted.facts[0].value == "栖松馆"


@pytest.mark.parametrize("acknowledgment", ["对的。", "知道了。", "没错？"])
def test_bot_needs_linked_human_confirmation(acknowledgment: str) -> None:
    proposal = "星舟项目在栖松馆演练？"
    window = _window(_statement(1, proposal, human=False), _statement(2, acknowledgment))
    with pytest.raises(ValueError, match="机器人"):
        parse_delta(json.dumps(_delta("栖松馆", 1, proposal)), window)
    payload = _delta("栖松馆", 2, acknowledgment, context=(1, proposal))
    if acknowledgment == "对的。":
        assert parse_delta(json.dumps(payload), window).facts[0].context is not None
    else:
        with pytest.raises(ValueError):
            parse_delta(json.dumps(payload), window)
