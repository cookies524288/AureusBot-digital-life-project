"""D90 目录、权限、持久化与真实 LLM 适配器的离线消息对照。"""

import json
import tomllib
from collections.abc import Iterator
from datetime import UTC
from pathlib import Path

import pytest

from aureus.actions.web import WebPage
from aureus.common import HeartbeatConfig, LanguageConfig, RuntimeConfig
from aureus.common.time_format import format_local_time
from aureus.heartbeat import ReplyLength, build_prompt_scenes, registered_templates
from aureus.heartbeat.context import (
    build_diary_prompt,
    build_exploration_prompt,
    build_initiative_plan_prompt,
    build_initiative_reply_prompt,
    build_judge_context,
    build_judge_prompt,
    build_plan_prompt,
    build_reply_request,
    build_system_context,
)
from aureus.heartbeat.language_requests import build_expression_quality
from aureus.heartbeat.persona_growth import MaterialLine, build_prompt
from aureus.heartbeat.planner_tools import PlannerToolOptions, planner_tools
from aureus.identity import Identity
from aureus.organs import (
    BUILTIN_ORGANS,
    LLMOrgan,
    OrganRequest,
    PromptStore,
    PromptTemplate,
    assemble_messages,
    organ_tasks,
)
from aureus.timeline import ActorType, TimelineRecord, VisibilityScope
from tests.organs.test_llm_adapter import FakeCompletion, make_config, make_response
from tests.web.config_rig import ConfigRig
from tests.web.test_timeline_api import NOW

IDENTITY = Identity.model_validate(
    {
        "facts": {"name": "测试助手", "birthday": "2000-01-01"},
        "user": {"name": "测试用户"},
        "persona": {
            "immutable": "温和。",
            "behavior_style": "先听完。",
            "expression_style": "说短句。",
        },
    }
)


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[ConfigRig]:
    """真实登录和端点，假运行环境。"""
    item = ConfigRig(tmp_path, prompt_identity=IDENTITY)
    item.log_in()
    yield item
    item.close()


def test_emoji_preview_routes_follow_active_selection_mode() -> None:
    default = RuntimeConfig()
    assert organ_tasks(default)["emoji"] == default.heartbeat.reply_organ
    assert organ_tasks(default, {"emoji", default.heartbeat.reply_organ})["emoji"] == "emoji"
    visual = RuntimeConfig(heartbeat=HeartbeatConfig(qq_emoji_visual_enabled=True))
    assert "emoji" not in organ_tasks(visual, {"emoji"})
    assert organ_tasks(visual)["emoji_visual_selection"] == "vlm"
    disabled = RuntimeConfig(heartbeat=HeartbeatConfig(qq_emoji_selection_enabled=False))
    assert "emoji" not in organ_tasks(disabled, {"emoji"})
    assert "emoji_visual_selection" not in organ_tasks(disabled)


@pytest.mark.parametrize(
    ("enabled", "organ", "assigned"),
    [
        (False, "judge", None),
        (True, None, None),
        (True, "judge", "judge"),
        (True, "reviewer", "reviewer"),
    ],
)
def test_expression_quality_preview_uses_runtime_builder_and_explicit_route(
    tmp_path: Path, enabled: bool, organ: str | None, assigned: str | None
) -> None:
    runtime = RuntimeConfig(
        language=LanguageConfig(expression_quality_enabled=enabled, expression_quality_organ=organ)
    )
    rig = ConfigRig(tmp_path, prompt_identity=IDENTITY, runtime=runtime)
    try:
        rig.log_in()
        catalog = rig.client.get("/api/prompts").json()
        assert any(item["key"] == "expression_quality" for item in catalog["templates"])
        assert {
            name for name, keys in catalog["organs"].items() if "expression_quality" in keys
        } == ({assigned} if assigned else set())
        response = rig.client.get(f"/api/organs/{assigned or 'judge'}/prompt-preview")
        assert response.status_code == 200
        previews = [
            scene for scene in response.json()["scenes"] if scene["id"] == "expression_quality"
        ]
        if assigned is None:
            assert not previews
            return
        [preview] = previews
        [scene] = build_prompt_scenes(assigned, {"expression_quality"}, IDENTITY, NOW)
        assert scene.request == build_expression_quality(assigned, "〔使用情境〕", "〔候选说法〕")
        assert json.loads(preview["messages"][1]["content"]) == {
            "situation": "〔使用情境〕",
            "style": "〔候选说法〕",
        }
    finally:
        rig.close()


def test_prompt_access_and_catalog(rig: ConfigRig, tmp_path: Path) -> None:
    listing = rig.client.get("/api/prompts").json()
    assert set(listing["organs"]) == set(BUILTIN_ORGANS)
    assert set().union(*map(set, listing["organs"].values())) | {"expression_quality"} == {
        template["key"] for template in listing["templates"]
    }
    assert all("expression_quality" not in keys for keys in listing["organs"].values())
    assert rig.client.get("/api/organs/missing/prompt-preview").status_code == 404
    assert rig.client.delete("/api/prompts/missing").status_code == 404
    assert rig.client.put("/api/prompts/plan_format", json={"source": "改格式"}).status_code == 422
    rig.client.cookies.clear()
    assert rig.client.get("/api/prompts").status_code == 401
    assert rig.client.delete("/api/prompts/reply_instruction").status_code == 401
    locked = ConfigRig(tmp_path / "locked", password_set=False, prompt_identity=IDENTITY)
    try:
        locked.log_in("admin")
        assert locked.client.get("/api/organs/planner/prompt-preview").status_code == 403
        assert (
            locked.client.put("/api/prompts/reply_instruction", json={"source": "测试"}).status_code
            == 403
        )
    finally:
        locked.close()


def test_judge_preview_uses_only_synthetic_self_state(rig: ConfigRig) -> None:
    response = rig.client.get("/api/organs/judge/prompt-preview")
    assert response.status_code == 200
    scenes = {scene["id"]: scene["messages"] for scene in response.json()["scenes"]}
    assert "【判断前的自身状态】" not in scenes["judge"][0]["content"]
    with_state = scenes["judge_self_state"]
    expected = build_judge_context(
        IDENTITY, ("疲劳 10%", "情绪（0 至 1）：低落 0.12", "心情（−1 至 1）：-0.06")
    )
    actual = with_state[0]["content"].replace("〔身体读数〕", "10%")
    actual = actual.replace("〔情绪读数〕", "低落 0.12").replace("〔心情读数〕", "-0.06")
    assert actual == expected
    assert "记录序号 〔当前消息记录号〕" in with_state[1]["content"]
    catalog = rig.client.get("/api/prompts").json()
    template = next(item for item in catalog["templates"] if item["key"] == "judge_self_state")
    assert template["variables"] == ["state"] and not template["editable"]


def test_restore_each_editable_template_keeps_other_overrides(rig: ConfigRig) -> None:
    client = rig.client
    originals = {
        item["key"]: item
        for item in client.get("/api/prompts").json()["templates"]
        if item["editable"]
    }
    assert originals
    overrides = {key: "离线样本。" + item["source"] for key, item in originals.items()}
    for key, source in overrides.items():
        saved = client.put(f"/api/prompts/{key}", json={"source": source})
        assert saved.status_code == 200 and saved.json()["customized"], key
    path = rig.paths.home / "prompts.toml"
    assert tomllib.loads(path.read_text(encoding="utf-8")) == overrides
    for key, original in originals.items():
        restored = client.delete(f"/api/prompts/{key}")
        assert restored.status_code == 200 and restored.json() == original, key
        del overrides[key]
        assert (
            tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        ) == overrides
        reloaded = PromptStore(path, registered_templates()).snapshot()
        assert {name: template.template for name, template in reloaded.items()} == overrides


def _record(sequence: int, actor: str, content: str) -> TimelineRecord:
    return TimelineRecord(
        id=sequence,
        timestamp=NOW,
        actor_type=ActorType.PERSON,
        actor_id=actor,
        event_type="message",
        content=content,
        visibility=VisibilityScope.PUBLIC,
    )


def _request(name: str, prompt: str, system: str) -> OrganRequest:
    return OrganRequest(name, prompt, system_context=system)


@pytest.mark.asyncio
async def test_every_organ_and_call_role_matches_captured_messages(rig: ConfigRig) -> None:
    user = IDENTITY.user.name
    history = [_record(1, "前位成员", "历史材料")]
    incoming = [_record(2, "当前说话人", "当前材料")]
    full = build_system_context(IDENTITY, NOW, None, UTC)
    reply = build_system_context(
        IDENTITY, NOW, None, UTC, include_behavior_style=False, initiative_note="主动触发样本"
    )
    exploration, exploration_system = build_exploration_prompt(
        "探索材料", WebPage("https://example.test/", "页面标题样本", "网页正文样本", False)
    )
    # 这里传入普通运行材料，使用正式构造函数及适配器；预览来自真实网页端点。
    cases = {
        "plan": _request("planner", build_plan_prompt([], incoming, user, UTC), full),
        "initiative_first": _request(
            "planner", build_initiative_plan_prompt([], user, ["主动原因样本"], UTC), full
        ),
        "reply": build_reply_request("replyer", [], incoming, user, UTC, system_context=full),
        "initiative_reply_first": _request(
            "replyer",
            build_initiative_reply_prompt([], user, UTC, "意图样本", with_splits=True),
            reply,
        ),
        "judge_context": _request(
            "judge", build_judge_prompt(history, incoming, user, UTC), build_judge_context(IDENTITY)
        ),
        "exploration": _request("summarizer", exploration, exploration_system),
        "diary": _request("summarizer", build_diary_prompt("当天经历样本", 300), full),
        "persona_review": _request(
            "summarizer", build_prompt([MaterialLine(1, "引用经历样本")], IDENTITY), full
        ),
    }
    replacements = {
        "〔当前时间〕": format_local_time(NOW, UTC),
        "〔当前年龄〕": str(NOW.year - 2000),
        "〔聊天历史〕": f"[{NOW:%m-%d %H:%M}] 前位成员：历史材料",
        "〔当前消息〕": f"[{NOW:%m-%d %H:%M}] 当前说话人：当前材料",
        "〔当前消息记录号〕": str(incoming[0].id),
        "〔主动联系原因〕": "主动原因样本",
        "〔主动联系触发原因〕": "主动触发样本",
        "〔规划意图〕": "意图样本",
        "〔探索主题〕": "探索材料",
        "〔网页地址〕": "https://example.test/",
        "〔网页标题〕": "页面标题样本",
        "〔网页正文〕": "网页正文样本",
        "〔当天经历〕": "当天经历样本",
        "〔日记字数上限〕": "300",
        "〔可引用经历〕": "[1] 引用经历样本",
    }
    completion = FakeCompletion(*(make_response("模拟输出") for _ in cases))
    active_organs = {request.organ_id for request in cases.values()}
    previews = {
        organ: {
            scene["id"]: scene["messages"]
            for scene in rig.client.get(f"/api/organs/{organ}/prompt-preview").json()["scenes"]
        }
        for organ in active_organs
    }
    assert not completion.calls
    routes = make_config().routes_for("replyer")
    for key, request in cases.items():
        adapter = LLMOrgan(
            request.organ_id,
            routes,
            rig.models.key_resolver,
            completion,
            prompts=rig.models.prompts,
        )
        await adapter.process(request)
        expected = []
        for message in previews[request.organ_id][key]:
            content = message["content"]
            values = (
                replacements
                if key != "reply"
                else {
                    **replacements,
                    "〔发言者〕": "当前说话人",
                    "〔当前消息〕": "当前材料",
                }
            )
            for marker, value in values.items():
                content = content.replace(marker, value)
            assert "〔" not in content
            expected.append({"role": message["role"], "content": content})
        assert completion.calls[-1]["messages"] == expected

    planned = previews["replyer"]["reply_planned"]
    assert "〔记忆召回：查询词、命中数、时间、说话人和记录〕" in planned[-2]["content"]
    assert "〔回复长度〕" in planned[-1]["content"]
    assert (
        ReplyLength.SHORT.describe() in previews["replyer"]["initiative_reply_first"][1]["content"]
    )
    assert "先听完。" not in planned[0]["content"]
    assert "不得" not in previews["replyer"]["reply"][0]["content"]
    assert "不要向对方透露" in previews["replyer"]["reply_contact"][0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_planner_preview_and_captured_schema_follow_available_tools(
    rig: ConfigRig, enabled: bool
) -> None:
    scene = "plan_context" if enabled else "plan"
    preview = rig.client.get("/api/organs/planner/prompt-preview")
    assert preview.status_code == 200
    [messages] = [item["messages"] for item in preview.json()["scenes"] if item["id"] == scene]
    tools = planner_tools(
        PlannerToolOptions(agenda=enabled, interests=enabled, recall=enabled, expressions=enabled)
    )
    prompt = build_plan_prompt(
        [],
        [_record(2, "当前说话人", "当前材料")],
        IDENTITY.user.name,
        UTC,
        with_agenda=enabled,
        with_interests=enabled,
        with_recall=enabled,
        with_expressions=enabled,
        tools=tools,
    )
    completion = FakeCompletion(make_response("模拟输出"))
    adapter = LLMOrgan(
        "planner",
        make_config().routes_for("replyer"),
        rig.models.key_resolver,
        completion,
        prompts=rig.models.prompts,
    )
    await adapter.process(
        OrganRequest("planner", prompt, tools=tuple(tool.definition() for tool in tools))
    )

    [captured] = completion.calls
    schemas = {
        tool["function"]["name"]: tool["function"]["parameters"] for tool in captured["tools"]
    }
    assert set(schemas) == ({"reply", "stop", "query_memory"} if enabled else {"reply", "stop"})
    assert set(schemas["reply"]["required"]) == {"addressed_to_me", "reply_reference"}
    assert not schemas["reply"]["additionalProperties"]
    assert not schemas["stop"]["properties"]
    if enabled:
        assert set(schemas["query_memory"]["required"]) == {"keywords"}
        assert schemas["query_memory"]["properties"]["keywords"]["maxItems"] == 3
    for text in (messages[1]["content"], captured["messages"][1]["content"]):
        assert "发言必须通过reply工具" in text
        for name in schemas:
            assert f"- {name}：" in text
        for field in ("commit", "resolve", "interest", "expression_intent"):
            assert (field in schemas["reply"]["properties"]) is enabled
            assert (field in text) is enabled
        assert ("query_memory" in text) is enabled


@pytest.mark.asyncio
async def test_save_invalid_reset_restart_and_upgrade_share_the_live_default(
    rig: ConfigRig,
) -> None:
    path = rig.paths.home / "prompts.toml"
    identity_bytes = IDENTITY.model_dump_json().encode()
    rig.paths.identity_file.write_bytes(identity_bytes)
    instruction = "reply_instruction"
    default = next(
        item.default.template for item in registered_templates() if item.key == instruction
    )
    client = rig.client
    request = build_reply_request(
        "replyer",
        [],
        [_record(1, "成员", "模型输入样本")],
        IDENTITY.user.name,
        UTC,
        system_context="",
    )
    fake = FakeCompletion(*(make_response("收到。") for _ in range(3)))
    organ = LLMOrgan(
        "replyer",
        make_config().routes_for("replyer"),
        rig.models.key_resolver,
        fake,
        prompts=rig.models.prompts,
    )

    assert (
        client.put("/api/prompts/reply_length", json={"source": "短一点 $length。"}).status_code
        == 200
    )
    saved = client.put(f"/api/prompts/{instruction}", json={"source": "简短一点，保留 $$。"})
    assert saved.json()["customized"]
    await organ.process(request)
    assert fake.calls[-1]["messages"][-1]["content"].endswith("简短一点，保留 $。")
    assert "简短一点，保留 $。" in client.get("/api/organs/replyer/prompt-preview").text

    before = path.read_bytes()
    for invalid in ["$unknown", "${unterminated", "${request.__class__}", "$!"]:
        assert (
            client.put(f"/api/prompts/{instruction}", json={"source": invalid}).status_code == 422
        )
    assert (
        client.put("/api/prompts/reply_length", json={"source": "没有长度变量"}).status_code == 422
    )
    assert path.read_bytes() == before
    await organ.process(request)
    assert fake.calls[-1]["messages"][-1]["content"].endswith("简短一点，保留 $。")
    restarted = PromptStore(path, registered_templates())
    assert assemble_messages(request, restarted)[-1]["content"].endswith("简短一点，保留 $。")

    deleted = client.delete(f"/api/prompts/{instruction}").json()
    assert deleted["source"] == default and not deleted["customized"]
    assert tomllib.loads(path.read_text(encoding="utf-8")) == {"reply_length": "短一点 $length。"}
    await organ.process(request)
    assert fake.calls[-1]["messages"][-1]["content"].endswith(default)
    scenes = client.get("/api/organs/replyer/prompt-preview").json()["scenes"]
    reply = next(scene for scene in scenes if scene["id"] == "reply")
    assert reply["messages"][-1]["content"].endswith(default)
    restarted = PromptStore(path, registered_templates())
    assert assemble_messages(request, restarted)[-1]["content"].endswith(default)

    updated = PromptTemplate(instruction, "发言要求", "升级后维护的默认文字。")
    installed = tuple(
        updated if item.key == instruction else item for item in registered_templates()
    )
    upgraded = PromptStore(path, installed)
    assert (
        assemble_messages(OrganRequest("replyer", updated.fill()), upgraded)[1]["content"]
        == updated.default.template
    )
    assert upgraded.snapshot()["reply_length"].template == "短一点 $length。"
    assert rig.paths.identity_file.read_bytes() == identity_bytes
    assert json.loads(IDENTITY.model_dump_json())["facts"]["name"] == "测试助手"
