"""器官配置目录只读；实际会话规划的同源工具受启动能力限制（D103）。"""

from dataclasses import asdict
from pathlib import Path
from typing import ClassVar

import pytest

from aureus.common import HeartbeatConfig, MemoryConfig, RuntimeConfig
from aureus.heartbeat import Heartbeat
from aureus.heartbeat.inbox_store import InboxStore
from aureus.heartbeat.planner_tools import PlannerToolOptions, planner_tools
from aureus.identity import Identity
from aureus.memory import MemoryIndex
from aureus.safety import EmergencyStop, PermissionChecker
from tests.web.config_rig import ConfigRig


class _CustomPlannerConfig(HeartbeatConfig):
    plan_organ: ClassVar[str] = "custom-planner"


@pytest.mark.parametrize(
    ("memory_enabled", "recall_enabled", "has_memory", "inbox_enabled", "roleplay"),
    [
        (True, True, True, True, False),
        (True, False, True, False, False),
        (False, True, False, True, True),
        (True, True, False, True, False),
    ],
)
def test_planner_tool_catalog_uses_active_binding_and_runtime_capabilities(
    tmp_path: Path,
    memory_enabled: bool,
    recall_enabled: bool,
    has_memory: bool,
    inbox_enabled: bool,
    roleplay: bool,
) -> None:
    heartbeat_config = _CustomPlannerConfig(
        inbox_attention_enabled=inbox_enabled, roleplay_mode=roleplay
    )
    memory_config = MemoryConfig(enabled=memory_enabled, recall_enabled=recall_enabled)
    rig = ConfigRig(
        tmp_path,
        runtime=RuntimeConfig(heartbeat=heartbeat_config, memory=memory_config),
        planner_tool_definitions=lambda: heartbeat.planner_tool_definitions(),
    )
    database = rig.paths.database_file
    stop = EmergencyStop(rig.paths.halt_file, rig.clock)
    memory = MemoryIndex(database, rig.timeline, rig.clock) if has_memory else None
    if memory is not None:
        memory.open()
    try:
        heartbeat = Heartbeat(
            clock=rig.clock,
            timeline=rig.timeline,
            organs=rig.models.bus,
            identity=Identity.model_validate(
                {
                    "facts": {"name": "测试助手", "birthday": "2026-11-23"},
                    "user": {"name": "测试用户"},
                    "persona": {"immutable": "友善。"},
                }
            ),
            config=heartbeat_config,
            permissions=PermissionChecker(stop),
            emergency_stop=stop,
            memory=memory,
            memory_config=memory_config,
            inbox_store=InboxStore(database, rig.clock),
        )
        path = "/api/organs/custom-planner/planner-tools"
        assert rig.client.get(path).status_code == 401
        rig.log_in()
        assert rig.client.get("/api/models").json()["planner_organ"] == heartbeat_config.plan_organ
        assert rig.client.get("/api/organs/planner/planner-tools").status_code == 404
        assert rig.client.get("/api/organs/replyer/planner-tools").status_code == 404
        assert rig.client.get(path).json() == {"tools": []}
        heartbeat.update_available_organs(frozenset({heartbeat_config.plan_organ}))
        expected = planner_tools(PlannerToolOptions(
            recall=has_memory and memory_enabled and recall_enabled,
            inbox=inbox_enabled and not roleplay,
            stay_in_conversation=True,
        ))
        assert rig.client.get(path).json()["tools"] == [asdict(tool.definition()) for tool in expected]
        heartbeat.attach_channel("qq", lambda _message: None, can_quote=lambda _route: False)
        reply = next(tool for tool in rig.client.get(path).json()["tools"] if tool["name"] == "reply")
        reply_options = reply["parameters"]["properties"]
        assert reply_options["quote_source"]["default"] is False
        assert rig.client.put(path, json={}).status_code == 405
    finally:
        if memory is not None:
            memory.close()
        rig.close()
