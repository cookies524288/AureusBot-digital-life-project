"""集成新增记忆、人际和语言片段，并在 MockTransport 捕获模型 HTTP 请求。"""

import json
from collections.abc import Callable, Iterator
from datetime import UTC
from pathlib import Path
from typing import Any

import httpx
import pytest

from aureus.common import MemoryConfig
from aureus.common.language_prompts import (
    LANGUAGE_EXPRESSION_ITEM,
    LANGUAGE_JARGON_ITEM,
    language_reference_block,
)
from aureus.common.time_format import format_local_time
from aureus.heartbeat import TrustLevel
from aureus.heartbeat.context import (
    build_judge_context,
    build_judge_prompt,
    build_plan_prompt,
    build_reply_prompt,
    build_system_context,
)
from aureus.heartbeat.language_runner import learn_language
from aureus.heartbeat.memory_consolidation import _extract
from aureus.heartbeat.people_context import PEOPLE_CONTEXT_TEMPLATE
from aureus.heartbeat.recall import format_recall
from aureus.memory import LanguageSample, LanguageStore, MemoryHit
from aureus.memory.consolidation import ConsolidationItem
from aureus.organs import (
    BUILTIN_ORGANS,
    LLMOrgan,
    ModelPreference,
    ModelsConfig,
    OrganRequest,
)
from aureus.organs.api_keys import KeyResolver
from aureus.timeline import ActorType, VisibilityScope
from tests.heartbeat.test_recall_flow import CountingIndex, MemoryEnv
from tests.web.config_rig import ConfigRig
from tests.web.test_prompt_api import IDENTITY
from tests.web.test_timeline_api import NOW


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[ConfigRig]:
    """使用合成身份、临时目录和网页会话。"""
    item = ConfigRig(tmp_path, prompt_identity=IDENTITY)
    item.log_in()
    yield item
    item.close()


def mock_models() -> ModelsConfig:
    """给四个联测器官配置同一个只会连接 MockTransport 的模型。"""
    return ModelsConfig.model_validate(
        {
            "providers": [
                {
                    "name": "mock",
                    "protocol": "openai",
                    "base_url": "https://prompt-test.invalid/v1",
                    "api_key_env": "AUREUS_KEY_PROMPT_HTTP_TEST",
                    "max_retry": 0,
                }
            ],
            "models": [{"name": "sample", "identifier": "sample-model", "provider": "mock"}],
            "organs": {organ: {"models": ["sample"]} for organ in BUILTIN_ORGANS},
        }
    )


def _expanded_messages(
    messages: list[dict[str, str]], replacements: dict[str, str]
) -> list[dict[str, str]]:
    expanded = []
    for message in messages:
        content = message["content"]
        for marker, value in replacements.items():
            content = content.replace(marker, value)
        assert "〔" not in content
        expanded.append({"role": message["role"], "content": content})
    return expanded


async def _check_derived_recall(
    path: Path, adapter: Callable[[str], LLMOrgan], captured: list[dict[str, Any]]
) -> None:
    """将正常心跳产生的规划和回复请求送过模拟 HTTP，核对原文和可见范围。"""
    config = MemoryConfig(
        consolidation_enabled=True,
        consolidation_extract_enabled=True,
        heuristic_recall_enabled=True,
        max_results=1,
    )
    for name, trust, scope, allowed in (
        ("owner", TrustLevel.VERIFIED_USER, VisibilityScope.USER_ONLY, True),
        ("group", TrustLevel.VERIFIED_USER, VisibilityScope.GROUP, False),
        ("contact", TrustLevel.VERIFIED_CONTACT, VisibilityScope.USER_ONLY, False),
    ):
        root = path / name
        root.mkdir()
        env = MemoryEnv(root)
        try:
            env.index.close()
            env.index = CountingIndex(
                root / "aureus.db", env.timeline, clock=env.clock, config=config
            )
            env.index.open()
            original = env.timeline.append(
                "message",
                "我喜欢那颗有冰壳的卫星。",
                ActorType.USER,
                visibility=VisibilityScope.USER_ONLY,
            )
            env.index_more()
            batch = env.index.consolidation_batch(1)
            assert batch is not None
            derived = "偏好木卫二"
            assert env.index.save_consolidation(batch, [("preference", derived, batch.items[0])])
            await env.exchange(env.build(config), trust=trust, visibility=scope)
            assert [req.organ_id for req in env.organs.requests] == ["planner", "replyer"]
            for request in env.organs.requests:
                count = len(captured)
                await adapter(request.organ_id).process(request)
                assert len(captured) == count + 1
                messages = captured[-1]["messages"]
                assert [message["role"] for message in messages] == ["system", "user"]
                content = messages[1]["content"]
                assert (original.content in content) == allowed
                assert derived not in content
                assert "待回应的群消息：" in content
            reply = captured[-1]["messages"][1]["content"]
            assert "已编辑按需召回标题" in reply
            assert f"命中：{int(allowed)} 条" in reply
            assert env.records("message")[-1].metadata["recall_record_ids"] == (
                [original.id] if allowed else []
            )
        finally:
            for heart in env.rigs:
                await heart.shut_down()
            for store in (env.index, env.findings, env.people, env.timeline):
                store.close()


@pytest.mark.asyncio
async def test_integrated_prompt_scenes_match_captured_http_and_template_edits(
    rig: ConfigRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm

    captured: list[dict[str, Any]] = []

    def provider_reply(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.host == "prompt-test.invalid"
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "sample-model"
        captured.append(body)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-prompt-test",
                "object": "chat.completion",
                "created": 1,
                "model": "sample-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "模拟回复"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider_reply))
    monkeypatch.setattr(litellm, "aclient_session", client)
    litellm.in_memory_llm_clients_cache.flush_cache()
    resolver = KeyResolver(
        rig.paths.home / "does-not-exist.env",
        environ={"AUREUS_KEY_PROMPT_HTTP_TEST": "sk-mock-only"},
    )
    models = mock_models()
    store = rig.models.prompts
    assert store is not None

    def adapter(organ: str) -> LLMOrgan:
        return LLMOrgan(
            organ,
            models.routes_for(organ),
            resolver,
            token_counter=lambda _messages: 12,
            prompts=rig.models.prompts,
        )

    restarted_rig = None
    try:
        preview_by_organ = {
            organ: {
                scene["id"]: scene
                for scene in rig.client.get(f"/api/organs/{organ}/prompt-preview").json()["scenes"]
            }
            for organ in BUILTIN_ORGANS
        }
        planner_plain = preview_by_organ["planner"]["plan"]["messages"][1]["content"]
        planner_rich = preview_by_organ["planner"]["plan_references"]["messages"][1]["content"]
        assert "expression_intent" not in planner_plain
        assert "expression_intent" in planner_rich
        assert "〔启发式记忆召回：查询词、命中数、时间、说话人和记录〕" in planner_rich
        reply_plain = preview_by_organ["replyer"]["reply"]["messages"][1]["content"]
        reply_rich = preview_by_organ["replyer"]["reply_references"]["messages"][1]["content"]
        assert "〔人物资料〕" not in reply_plain
        for marker in (
            "〔记忆召回：查询词、命中数、时间、说话人和记录〕",
            "〔人物资料〕",
            "〔习得表达〕",
            "〔适用情景〕",
        ):
            assert marker in reply_rich
        learner = preview_by_organ["summarizer"]["language_learning"]
        assert "〔新入站样本 JSON〕" in learner["messages"][1]["content"]
        assert "增量学习" in learner["note"]

        full = build_system_context(IDENTITY, NOW, None, UTC)
        short = build_system_context(IDENTITY, NOW, None, UTC, include_behavior_style=False)
        message = "合成旅行消息"
        common = {
            "〔当前时间〕": format_local_time(NOW, UTC),
            "〔当前年龄〕": str(NOW.year - 2000),
            "〔当前消息〕": message,
        }
        actual_jargon = language_reference_block(
            [LANGUAGE_JARGON_ITEM.fill(text="GG", detail="起跑暗号")], jargon=True
        )
        actual_plan = OrganRequest(
            "planner",
            build_plan_prompt(
                [],
                message,
                IDENTITY.user.name,
                with_recall=True,
                jargon=actual_jargon,
                with_expressions=True,
                heuristic_recall=format_recall(
                    ["旅行"],
                    [MemoryHit(7, NOW, "模拟说话人", "动态记忆命中样例")],
                    NOW.tzinfo,
                    heuristic=True,
                ),
            ),
            system_context=full,
        )
        stamp = NOW.strftime("%Y-%m-%d %H:%M")
        recall_counts = f"查询：旅行\n命中：1 条\n1. {stamp} "
        heuristic_body = recall_counts + "模拟说话人：动态记忆命中样例"
        requested_body = recall_counts + "模拟联系人：动态记忆命中样例"
        plan_replacements = {
            **common,
            "〔习得黑话〕": "GG",
            "〔黑话释义〕": "起跑暗号",
            "〔启发式记忆召回：查询词、命中数、时间、说话人和记录〕": heuristic_body,
        }

        actual_expression = language_reference_block(
            [LANGUAGE_EXPRESSION_ITEM.fill(text="慢慢看海", detail="分享旅行")], jargon=False
        )
        actual_reply = OrganRequest(
            "replyer",
            build_reply_prompt(
                [],
                message,
                IDENTITY.user.name,
                intent="聊聊旅程",
                length="一句话",
                recall=format_recall(
                    ["旅行"], [MemoryHit(8, NOW, "模拟联系人", "动态记忆命中样例")], NOW.tzinfo
                ),
                expressions=actual_expression,
                people_context=PEOPLE_CONTEXT_TEMPLATE.fill(people_records="动态人物资料样例"),
            ),
            system_context=short,
        )
        reply_replacements = {
            **common,
            "〔规划意图〕": "聊聊旅程",
            "〔回复长度〕": "一句话",
            "〔记忆召回：查询词、命中数、时间、说话人和记录〕": requested_body,
            "〔人物资料〕": "动态人物资料样例",
            "〔习得表达〕": "慢慢看海",
            "〔适用情景〕": "分享旅行",
        }
        language = LanguageStore(rig.paths.database_file)
        language.open()
        next_sample_id = 42

        cases: list[tuple[str, str, OrganRequest, dict[str, str]]] = [
            (
                "planner",
                "plan",
                OrganRequest(
                    "planner",
                    build_plan_prompt([], message, IDENTITY.user.name),
                    system_context=full,
                ),
                common,
            ),
            ("planner", "plan_references", actual_plan, plan_replacements),
            (
                "replyer",
                "reply",
                OrganRequest(
                    "replyer",
                    build_reply_prompt([], message, IDENTITY.user.name),
                    system_context=full,
                ),
                common,
            ),
            ("replyer", "reply_references", actual_reply, reply_replacements),
            (
                "judge",
                "judge",
                OrganRequest(
                    "judge",
                    build_judge_prompt([], message, IDENTITY.user.name),
                    system_context=build_judge_context(IDENTITY),
                ),
                common,
            ),
        ]

        async def compare() -> dict[str, str]:
            nonlocal next_sample_id
            by_scene = {}
            for organ, scene_id, request, replacements in cases:
                scenes = rig.client.get(f"/api/organs/{organ}/prompt-preview").json()["scenes"]
                preview = next(scene["messages"] for scene in scenes if scene["id"] == scene_id)
                count = len(captured)
                await adapter(organ).process(request)
                body = captured[-1]
                assert len(captured) == count + 1
                assert [message["role"] for message in body["messages"]] == ["system", "user"]
                assert body["messages"] == _expanded_messages(preview, replacements)
                by_scene[scene_id] = body["messages"][1]["content"]
            sample = LanguageSample(
                str(next_sample_id), next_sample_id, "模拟成员", "合成的新入站语言样本"
            )
            next_sample_id += 1
            count = len(captured)
            for _ in range(2):
                await learn_language(
                    language, "summarizer", adapter("summarizer").process, [sample]
                )
            assert len(captured) == count + 1
            previews = rig.client.get("/api/organs/summarizer/prompt-preview").json()["scenes"]
            preview = next(
                scene["messages"] for scene in previews if scene["id"] == "language_learning"
            )
            data = json.dumps({sample.record_id: sample.content}, ensure_ascii=False)
            assert captured[-1]["messages"] == _expanded_messages(
                preview, {"〔新入站样本 JSON〕": data}
            )
            by_scene["language_learning"] = captured[-1]["messages"][1]["content"]
            items = [
                ConsolidationItem("timeline:17", (), "模拟发言甲", None, True),
                ConsolidationItem("timeline:43", (), "模拟发言乙：$messages", None, True),
            ]
            previous = len(captured)
            await _extract(items, adapter("summarizer").process, ModelPreference.CHEAPEST)
            assert len(captured) == previous + 1
            scene = next(scene for scene in previews if scene["id"] == "memory_consolidation")
            assert "夜间" in scene["note"]
            numbered = '{"1": "模拟发言甲", "2": "模拟发言乙：$messages"}'
            assert captured[-1]["messages"] == _expanded_messages(
                scene["messages"], {"〔编号发言 JSON〕": numbered}
            )
            by_scene["memory_consolidation"] = captured[-1]["messages"][1]["content"]
            return by_scene

        baseline = await compare()
        learner_edit = "已编辑学习提示，$max_text、$max_detail、$output_format、$samples"
        edits = {
            "extraction_prompt": "合成提取：$utterances\n$output_format",
            "new_messages": "待回应的群消息：\n$messages",
            "people_context": "已编辑人物资料：$people_records",
            "heuristic_memory_reference": "已编辑启发式标题",
            "replyer_memory_reference": "已编辑按需召回标题",
            "language_expression_header": "已编辑习得表达标题",
            "language_learner_prompt": learner_edit,
        }
        for key, source in edits.items():
            assert rig.client.put(f"/api/prompts/{key}", json={"source": source}).status_code == 200
        edited = await compare()
        await _check_derived_recall(rig.paths.home, adapter, captured)
        assert edited["memory_consolidation"].startswith('合成提取：{"1": "模拟发言甲"')
        assert "待回应的群消息：" in edited["plan_references"]
        assert "已编辑人物资料：动态人物资料样例" in edited["reply_references"]
        assert "已编辑按需召回标题" in edited["reply_references"]
        assert "已编辑启发式标题" in edited["plan_references"]
        assert "已编辑学习提示" in edited["language_learning"]
        assert not rig.client.delete("/api/prompts/people_context").json()["customized"]
        restored = await compare()
        reply = restored["reply_references"]
        assert "已编辑人物资料" not in reply and "已编辑习得表达标题" in reply
        assert set(store.snapshot()) == set(edits) - {"people_context"}
        restarted_rig = ConfigRig(rig.paths.home, prompt_identity=IDENTITY)
        restarted_rig.log_in()
        rig = restarted_rig
        restarted = await compare()
        assert restarted["memory_consolidation"] == edited["memory_consolidation"]
        assert not rig.client.delete("/api/prompts/extraction_prompt").json()["customized"]
        reset = await compare()
        assert reset["memory_consolidation"] == baseline["memory_consolidation"]
        assert reset["plan_references"] == edited["plan_references"]
        assert restarted["reply_references"] == restored["reply_references"]
        assert restarted["plan_references"] == edited["plan_references"]
        assert "已编辑学习提示" in restarted["language_learning"]
    finally:
        if restarted_rig is not None:
            restarted_rig.close()
        litellm.in_memory_llm_clients_cache.flush_cache()
        await client.aclose()
