"""用只读身份和具名占位符构造器官预览（D90）。"""

from dataclasses import dataclass
from datetime import UTC, datetime

from aureus.actions.web import WebPage
from aureus.common.language_prompts import (
    LANGUAGE_EXPRESSION_ITEM,
    LANGUAGE_JARGON_ITEM,
    LANGUAGE_TEMPLATES,
    language_reference_block,
)
from aureus.heartbeat.body_summary import ROLEPLAY_NOTE
from aureus.heartbeat.context import (
    CONTEXT_TEMPLATES,
    build_diary_prompt,
    build_exploration_prompt,
    build_initiative_plan_prompt,
    build_initiative_reply_prompt,
    build_judge_context,
    build_judge_prompt,
    build_plan_prompt,
    build_reply_prompt,
    build_system_context,
)
from aureus.heartbeat.downtime import Downtime
from aureus.heartbeat.language_runner import build_language_learning_prompt
from aureus.heartbeat.people_context import PEOPLE_CONTEXT_TEMPLATE
from aureus.heartbeat.persona_growth import REVIEW_TEMPLATES, build_prompt
from aureus.heartbeat.recall import (
    HEURISTIC_MEMORY_REFERENCE_MARKER,
    REPLYER_MEMORY_REFERENCE_MARKER,
    recall_reference,
)
from aureus.identity import Identity
from aureus.organs import EMPTY_SYSTEM_MESSAGE, OrganRequest, PromptTemplate, prompt_template_keys

_HISTORY = "〔聊天历史〕"
_INPUT = "〔当前消息〕"
_LENGTH = "〔回复长度〕"
_INTENT = "〔规划意图〕"
_CONDITIONAL = {
    "plan": "按开关和本轮输入附加黑话、表达意图、日程、兴趣及召回。",
    "reply": "有命中时附记忆、人际与习得表达。合并回复才附黑话，规划后可附意图和长度。",
    "language_learning": "启用增量学习且收到未处理的新样本时调用。",
    "initiative_plan": "有历史时附对话，没有历史时显示首次联系提示。",
    "initiative_reply": "仅在主动规划决定开口时使用；无长度设置时使用短回复。",
    "judge": "仅在判断器官启用且档位允许时调用，有历史才附对话。",
}


@dataclass(frozen=True)
class PromptScene:
    """一个可能发给指定器官的场景及其预览请求。"""

    key: str
    task: str
    label: str
    request: OrganRequest

    @property
    def note(self) -> str:
        """说明此调用场景的条件。"""
        return _CONDITIONAL.get(self.task, "")

    def template_keys(self) -> set[str]:
        """本场景用到的模板，包括空系统提示。"""
        keys = prompt_template_keys(self.request.prompt, self.request.system_context)
        if not self.request.system_context:
            keys.add(EMPTY_SYSTEM_MESSAGE.key)
        return keys


def registered_templates() -> tuple[PromptTemplate, ...]:
    """汇总内置消息构造代码登记的模板。"""
    return (
        EMPTY_SYSTEM_MESSAGE,
        *CONTEXT_TEMPLATES,
        *REVIEW_TEMPLATES,
        *LANGUAGE_TEMPLATES,
        PEOPLE_CONTEXT_TEMPLATE,
        HEURISTIC_MEMORY_REFERENCE_MARKER,
        REPLYER_MEMORY_REFERENCE_MARKER,
    )


def build_prompt_scenes(
    organ: str, tasks: set[str], identity: Identity, now: datetime, *, roleplay: bool = False
) -> list[PromptScene]:
    """按启动时的器官职责及代表性输入组合预览，不查询运行记录。"""
    user = identity.user.name
    scenes: list[PromptScene] = []

    def add(task: str, variant: str, label: str, prompt: str, system: str) -> None:
        if task in tasks:
            scenes.append(
                PromptScene(
                    variant, task, label, OrganRequest(organ, prompt, system_context=system)
                )
            )

    def system(
        *, full: bool = True, rich: bool = False, contact: bool = False, initiative: bool = False
    ) -> str:
        return build_system_context(
            identity,
            now,
            Downtime(now, None, False, None) if rich else None,
            include_behavior_style=full,
            contact_account="〔联系人账号〕" if contact else None,
            identity_changes=["〔醒来前修改的人设层〕"] if rich else [],
            identity_changes_live=["〔刚修改的人设层〕"] if rich else [],
            body_state=ROLEPLAY_NOTE if roleplay else ("〔身体状态〕" if rich else ""),
            hormone_state="〔激素状态和行为说明〕" if rich else "",
            affinity_state="〔人际状态〕" if rich else "",
            mood_state="〔当前心情〕" if rich else "",
            agenda_state="〔当前日程〕" if rich else "",
            interests_state="〔兴趣和最近收获〕" if rich and not contact else "",
            initiative_note="〔主动联系触发原因〕" if initiative else "",
            preview=True,
        )

    if tasks & {"plan", "reply", "initiative_plan", "initiative_reply"}:
        jargon = language_reference_block(
            [LANGUAGE_JARGON_ITEM.fill(text="〔习得黑话〕", detail="〔黑话释义〕")], jargon=True
        )
        expression = language_reference_block(
            [LANGUAGE_EXPRESSION_ITEM.fill(text="〔习得表达〕", detail="〔适用情景〕")],
            jargon=False,
        )
        people = PEOPLE_CONTEXT_TEMPLATE.fill(people_records="〔人物资料〕")
        automatic = recall_reference(
            "〔启发式记忆召回：查询词、命中数、时间、说话人和记录〕", heuristic=True
        )
        add("plan", "plan", "回应规划", build_plan_prompt([], _INPUT, user), system())
        add(
            "plan",
            "plan_context",
            "回应规划 · 含可选上下文",
            build_plan_prompt(
                _HISTORY,
                _INPUT,
                user,
                with_agenda=True,
                with_interests=True,
                with_recall=True,
                jargon=jargon,
                with_expressions=True,
                heuristic_recall=automatic,
            ),
            system(rich=True),
        )
        add(
            "plan",
            "plan_contact",
            "回应规划 · 陌生联系人",
            build_plan_prompt(_HISTORY, _INPUT, user, with_agenda=True, with_recall=True),
            system(rich=True, contact=True),
        )
        add("reply", "reply", "合并回复 · 无规划", build_reply_prompt([], _INPUT, user), system())
        add(
            "reply",
            "reply_context",
            "合并回复 · 含上下文",
            build_reply_prompt(
                _HISTORY,
                _INPUT,
                user,
                length=_LENGTH,
                recall=automatic,
                jargon=jargon,
                expressions=expression,
                people_context=people,
            ),
            system(rich=True),
        )
        add(
            "reply",
            "reply_planned",
            "规划后回复 · 含记忆召回",
            build_reply_prompt(
                _HISTORY,
                _INPUT,
                user,
                intent=_INTENT,
                length=_LENGTH,
                recall=recall_reference("〔记忆召回：查询词、命中数、时间、说话人和记录〕"),
                expressions=expression,
                people_context=people,
            ),
            system(full=False, rich=True),
        )
        add(
            "reply",
            "reply_contact",
            "规划后回复 · 陌生联系人",
            build_reply_prompt(_HISTORY, _INPUT, user, intent=_INTENT),
            system(full=False, rich=True, contact=True),
        )
        add(
            "plan",
            "plan_references",
            "回应规划 · 含习得语言及启发式记忆",
            build_plan_prompt(
                [],
                _INPUT,
                user,
                with_recall=True,
                jargon=jargon,
                with_expressions=True,
                heuristic_recall=automatic,
            ),
            system(),
        )
        add(
            "reply",
            "reply_references",
            "规划后回复 · 含人际、记忆及习得表达",
            build_reply_prompt(
                [],
                _INPUT,
                user,
                intent=_INTENT,
                length=_LENGTH,
                recall=recall_reference("〔记忆召回：查询词、命中数、时间、说话人和记录〕"),
                expressions=expression,
                people_context=people,
            ),
            system(full=False),
        )
        add(
            "initiative_plan",
            "initiative_plan",
            "主动规划",
            build_initiative_plan_prompt(_HISTORY, user, ["〔主动联系原因〕"]),
            system(rich=True),
        )
        add(
            "initiative_plan",
            "initiative_first",
            "主动规划 · 无历史",
            build_initiative_plan_prompt([], user, ["〔主动联系原因〕"]),
            system(),
        )
        add(
            "initiative_reply",
            "initiative_reply",
            "主动发言",
            build_initiative_reply_prompt(_HISTORY, user, intent=_INTENT, length=_LENGTH),
            system(full=False, rich=True, initiative=True),
        )
        add(
            "initiative_reply",
            "initiative_reply_first",
            "主动发言 · 无历史",
            build_initiative_reply_prompt([], user, intent=_INTENT),
            system(full=False, initiative=True),
        )
    if "judge" in tasks:
        judge = build_judge_context(identity)
        add("judge", "judge", "情绪判断", build_judge_prompt([], _INPUT, user), judge)
        add(
            "judge",
            "judge_context",
            "情绪判断 · 含历史",
            build_judge_prompt(_HISTORY, _INPUT, user),
            judge,
        )
    if "exploration_summary" in tasks:
        prompt, background = build_exploration_prompt(
            "〔探索主题〕", WebPage("〔网页地址〕", "〔网页标题〕", "〔网页正文〕", False)
        )
        add("exploration_summary", "exploration", "探索总结", prompt, background)
    if "diary" in tasks:
        add(
            "diary",
            "diary",
            "日记",
            build_diary_prompt("〔当天经历〕", "〔日记字数上限〕"),
            system(),
        )
    if "persona_review" in tasks:
        add(
            "persona_review",
            "persona_review",
            "人设回顾",
            build_prompt("〔可引用经历〕", identity),
            system(),
        )
    add(
        "language_learning",
        "language_learning",
        "增量语言学习",
        build_language_learning_prompt("〔新入站样本 JSON〕"),
        "",
    )
    return scenes or [
        PromptScene("custom", "custom", "通用请求", OrganRequest(organ, "〔调用方提示词〕"))
    ]


def catalog_template_keys(organ: str, tasks: set[str]) -> set[str]:
    """在完整模拟身份下盘点每个器官可能使用的模板。"""
    identity = Identity.model_validate(
        {
            "facts": {
                "name": "模拟身份",
                "birthday": "2000-01-01",
                "gender": "模拟",
                "alias_names": ["模拟昵称"],
            },
            "user": {"name": "模拟用户"},
            "persona": {
                "immutable": "甲",
                "mutable": "乙",
                "behavior_style": "丙",
                "expression_style": "丁",
            },
        }
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    keys = set().union(
        *(scene.template_keys() for scene in build_prompt_scenes(organ, tasks, identity, now))
    )
    if "identity_age" in keys:
        keys.add("identity_birthday")
    return keys
