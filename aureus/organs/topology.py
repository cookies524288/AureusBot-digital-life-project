"""器官页的结构图。None 端点代表核心，以免与旧配置里的器官名冲突。"""

from collections.abc import Iterable
from dataclasses import dataclass

from aureus.common.config import RuntimeConfig


@dataclass(frozen=True)
class OrganDefinition:
    """核心与该器官交换的内容，以及经核心衔接的后续步骤。"""

    request: str
    response: str
    next_steps: tuple[tuple[str, str], ...] = ()


# 登记新器官时在同一处说明职责和后续步骤。所有器官都遵循请求 / 建议协议。
ORGAN_DEFINITIONS = {
    "replyer": OrganDefinition("生成回复", "返回回复文本"),
    "planner": OrganDefinition(
        "请求行动规划", "返回行动建议", (("replyer", "核心采纳发言计划后传递回复意图"),)
    ),
    "judge": OrganDefinition(
        "按需判断来信",
        "返回判断建议",
        (
            ("planner", "判断触发情绪变化时，核心调整规划上下文"),
            ("replyer", "核心将判断触发的情绪变化带入回复上下文"),
        ),
    ),
    "summarizer": OrganDefinition("提交待整理的材料", "返回整理结果"),
}
BUILTIN_ORGANS = tuple(ORGAN_DEFINITIONS)


def organ_connections(names: Iterable[str], runtime: RuntimeConfig) -> list[dict[str, str | None]]:
    """由本次运行的职责分配生成布线；流程是可能执行的路径，分支取决于当次心跳。"""
    routes: dict[str, tuple[str, ...]] = {name: (name,) for name in ORGAN_DEFINITIONS}
    routes.update(
        planner=(runtime.heartbeat.plan_organ,),
        replyer=(runtime.heartbeat.reply_organ,),
        judge=(runtime.judge.organ,),
        summarizer=tuple(
            dict.fromkeys(
                (runtime.exploration.organ, runtime.diary.organ, runtime.persona_growth.organ)
            )
        ),
    )
    endpoints = dict.fromkeys((*ORGAN_DEFINITIONS, *names))
    links: list[dict[str, str | None]] = []
    for name in endpoints:
        jobs = [
            definition for role, definition in ORGAN_DEFINITIONS.items() if name in routes[role]
        ]
        request = "、".join(job.request for job in jobs) or "通过器官总线派发请求"
        response = "、".join(job.response for job in jobs) or "返回器官建议"
        links.append({"source": None, "target": name, "kind": "call", "label": request})
        links.append({"source": name, "target": None, "kind": "return", "label": response})
    # 同一个器官可承担多个职责，把同向且同端点的工作流说明合在一根线上。
    relays: dict[tuple[str, str], list[str]] = {}
    for role, definition in ORGAN_DEFINITIONS.items():
        for successor, label in definition.next_steps:
            for source in routes[role]:
                for target in routes[successor]:
                    if source in endpoints and target in endpoints:
                        relays.setdefault((source, target), []).append(label)
    for (source, target), labels in relays.items():
        links.append(
            {"source": source, "target": target, "kind": "relay", "label": "；".join(labels)}
        )
    return links
