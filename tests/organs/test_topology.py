"""器官关系由登记表和正在使用的职责配置生成。"""

import pytest

from aureus.common.config import RuntimeConfig
from aureus.organs.topology import ORGAN_DEFINITIONS, OrganDefinition, organ_connections


def test_registered_organ_inherits_core_ports_and_declares_its_successor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        ORGAN_DEFINITIONS, "observer", OrganDefinition("观察", "结果", (("planner", "传递观察"),))
    )
    edges = organ_connections([], RuntimeConfig())
    assert {
        (e["source"], e["target"], e["kind"])
        for e in edges
        if "observer" in (e["source"], e["target"])
    } == {
        (None, "observer", "call"),
        ("observer", None, "return"),
        ("observer", "planner", "relay"),
    }


def test_flow_shows_builtin_planner_and_replyer_despite_legacy_names() -> None:
    runtime = RuntimeConfig.model_validate(
        {"heartbeat": {"plan_organ": "legacy", "reply_organ": "legacy"}}
    )
    edges = organ_connections(["legacy"], runtime)
    relays = {(e["source"], e["target"]) for e in edges if e["kind"] == "relay"}
    assert relays == {
        ("judge", "planner"),
        ("judge", "replyer"),
        ("planner", "replyer"),
        ("replyer", "fast_model"),
    }
    assert any(
        e["source"] is None and e["target"] == "replyer" and "生成回复" in (e["label"] or "")
        for e in edges
    )
