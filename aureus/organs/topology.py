"""器官页的结构图。None 端点代表核心，以免与旧配置里的器官名冲突。"""

from collections.abc import Collection, Iterable
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
    "replyer": OrganDefinition(
        "生成回复", "返回回复文本", (("fast_model", "核心将再审后的回复交给快速模型建议切点"),)
    ),
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
    "memory": OrganDefinition("提取有原文依据的事实与偏好", "返回候选记忆"),
    "utils": OrganDefinition(
        "压缩工作记忆，在专职器官未配置时整理材料", "返回摘要或整理建议"
    ),
    "fast_model": OrganDefinition("建议回复切点，也可代替判断器官", "返回切点或判断建议"),
    "emoji": OrganDefinition("按 QQ 对话和情绪挑选表情", "返回单个候选编号"),
    "vlm": OrganDefinition("理解 QQ 来信中的图片", "返回图片描述"),
    "voice": OrganDefinition("转写 QQ 来信中的语音", "返回转写文本"),
    "text_embedding": OrganDefinition("向量化记忆与检索词", "返回文本向量"),
    "image_embedding": OrganDefinition("向量化入站图片", "返回图片向量"),
}
BUILTIN_ORGANS = tuple(ORGAN_DEFINITIONS)
_FALLBACK_ORGANS = {
    "judge": "fast_model",
    "exploration_summary": "utils",
    "diary": "utils",
    "persona_review": "utils",
    "language_learning": "utils",
    "learn_style": "utils",
    "learn_jargon": "utils",
    "jargon_inference_with_context": "utils",
    "jargon_inference_content_only": "utils",
    "jargon_compare_inference": "utils",
}


def resolve_task_organ(primary: str, task: object, available: Collection[str]) -> str:
    """按模型配置选器官，保留专职绑定和旧版记忆整理的默认路线。"""
    if task == "memory_consolidation" and "memory" in available:
        return "memory"
    if task == "emoji" and "emoji" in available:
        return "emoji"
    if primary not in available and isinstance(task, str):
        fallback = _FALLBACK_ORGANS.get(task)
        if fallback is not None and fallback in available:
            return fallback
    return primary


def organ_tasks(runtime: RuntimeConfig, available: Collection[str] = ()) -> dict[str, str]:
    """从启动配置取得各调用任务实际交给哪个器官。"""
    tasks = {
        "plan": runtime.heartbeat.plan_organ,
        "initiative_plan": runtime.heartbeat.plan_organ,
        "reply": runtime.heartbeat.reply_organ,
        "initiative_reply": runtime.heartbeat.reply_organ,
        "judge": runtime.judge.organ,
        "exploration_summary": runtime.exploration.organ,
        "diary": runtime.diary.organ,
        "persona_review": runtime.persona_growth.organ,
        "language_learning": runtime.language.organ,
        "memory_consolidation": "summarizer",
        "working_memory_summary": "utils",
        "image_description": "vlm",
    }
    if runtime.heartbeat.split_reply_enabled:
        tasks["reply_segments"] = "fast_model"
    if runtime.heartbeat.qq_emoji_selection_enabled:
        if runtime.heartbeat.qq_emoji_visual_enabled:
            tasks["emoji_visual_selection"] = "vlm"
        else:
            tasks["emoji"] = runtime.heartbeat.reply_organ
    return {task: resolve_task_organ(primary, task, available) for task, primary in tasks.items()}


def organ_connections(
    names: Iterable[str], runtime: RuntimeConfig, available: Collection[str] = ()
) -> list[dict[str, str | None]]:
    """由本次运行的职责分配生成布线；流程是可能执行的路径，分支取决于当次心跳。"""
    tasks = organ_tasks(runtime, available)
    routes: dict[str, tuple[str, ...]] = {role: (role,) for role in ORGAN_DEFINITIONS}
    routes.update(
        {
            "planner": (tasks["plan"],),
            "replyer": (tasks["reply"],),
            "judge": (tasks["judge"],),
            "summarizer": tuple(
                dict.fromkeys(
                    tasks[key]
                    for key in (
                        "exploration_summary",
                        "diary",
                        "persona_review",
                        "language_learning",
                    )
                )
            ),
        }
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
            if role == "replyer" and successor == "fast_model" and "reply_segments" not in tasks:
                continue
            for source in routes[role]:
                for target in routes[successor]:
                    if source in endpoints and target in endpoints:
                        relays.setdefault((source, target), []).append(label)
    for (source, target), labels in relays.items():
        links.append(
            {"source": source, "target": target, "kind": "relay", "label": "；".join(labels)}
        )
    return links
