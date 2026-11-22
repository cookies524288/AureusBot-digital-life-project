"""D90 目录、权限、持久化与真实 LLM 适配器的离线消息对照。"""

import json
import tomllib
from collections.abc import Iterator
from datetime import UTC
from pathlib import Path

import pytest

from aureus.actions.web import WebPage
from aureus.common.time_format import format_local_time
from aureus.heartbeat import ReplyLength, registered_templates
from aureus.heartbeat.context import (
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
from aureus.heartbeat.persona_growth import MaterialLine, build_prompt
from aureus.identity import Identity
from aureus.organs import (
    BUILTIN_ORGANS,
    LLMOrgan,
    OrganRequest,
    PromptStore,
    PromptTemplate,
    assemble_messages,
)
from aureus.timeline import ActorType, TimelineRecord, VisibilityScope
from tests.organs.test_llm_adapter import FakeCompletion, make_config, make_response
from tests.web.config_rig import ConfigRig
from tests.web.test_timeline_api import NOW

IDENTITY = Identity.model_validate(
    {
        "facts": {"name": "测试豆", "birthday": "2000-01-01"},
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


def test_prompt_access_and_catalog(rig: ConfigRig, tmp_path: Path) -> None:
    listing = rig.client.get("/api/prompts").json()
    assert set(listing["organs"]) == set(BUILTIN_ORGANS)
    assert set().union(*map(set, listing["organs"].values())) == {
        template["key"] for template in listing["templates"]
    } - {"empty_system"}
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
        "reply": _request("replyer", build_reply_prompt([], incoming, user, UTC), full),
        "initiative_reply_first": _request(
            "replyer", build_initiative_reply_prompt([], user, UTC, "意图样本"), reply
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
    assert set(request.organ_id for request in cases.values()) == set(BUILTIN_ORGANS)
    previews = {
        organ: {
            scene["id"]: scene["messages"]
            for scene in rig.client.get(f"/api/organs/{organ}/prompt-preview").json()["scenes"]
        }
        for organ in BUILTIN_ORGANS
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
            for marker, value in replacements.items():
                content = content.replace(marker, value)
            assert "〔" not in content
            expected.append({"role": message["role"], "content": content})
        assert completion.calls[-1]["messages"] == expected

    plan = previews["planner"]["plan_context"][1]["content"]
    for field in ["commit", "resolve", "interest", "recall"]:
        assert f'"{field}"' in plan
        assert f'"{field}"' not in previews["planner"]["plan"][1]["content"]
    planned = previews["replyer"]["reply_planned"]
    assert "〔记忆召回〕" in planned[1]["content"]
    assert "〔回复长度〕" in planned[1]["content"]
    assert (
        ReplyLength.SHORT.describe() in previews["replyer"]["initiative_reply_first"][1]["content"]
    )
    assert "先听完。" not in planned[0]["content"]
    assert "不得" not in previews["replyer"]["reply"][0]["content"]
    assert "不要向对方透露" in previews["replyer"]["reply_contact"][0]["content"]


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
    request = OrganRequest("replyer", build_reply_prompt([], "模型输入样本", IDENTITY.user.name))
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
    assert fake.calls[-1]["messages"][1]["content"].endswith("简短一点，保留 $。")
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
    assert fake.calls[-1]["messages"][1]["content"].endswith("简短一点，保留 $。")
    restarted = PromptStore(path, registered_templates())
    assert assemble_messages(request, restarted)[1]["content"].endswith("简短一点，保留 $。")

    deleted = client.delete(f"/api/prompts/{instruction}").json()
    assert deleted["source"] == default and not deleted["customized"]
    assert tomllib.loads(path.read_text(encoding="utf-8")) == {"reply_length": "短一点 $length。"}
    await organ.process(request)
    assert fake.calls[-1]["messages"][1]["content"].endswith(default)
    assert default in client.get("/api/organs/replyer/prompt-preview").text
    restarted = PromptStore(path, registered_templates())
    assert assemble_messages(request, restarted)[1]["content"].endswith(default)

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
    assert json.loads(IDENTITY.model_dump_json())["facts"]["name"] == "测试豆"
