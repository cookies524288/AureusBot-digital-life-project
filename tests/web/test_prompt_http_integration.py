"""集成新增记忆、人际和语言片段，并在 MockTransport 捕获模型 HTTP 请求。"""

import json
from base64 import b64decode
from collections.abc import Callable, Iterator
from datetime import UTC
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from aureus.common import HeartbeatConfig, MemoryConfig
from aureus.common.language_prompts import (
    LANGUAGE_EXPRESSION_ITEM,
    LANGUAGE_JARGON_ITEM,
    language_reference_block,
)
from aureus.common.time_format import format_local_time
from aureus.heartbeat import MediaReference, Perception, TrustLevel
from aureus.heartbeat.context import (
    build_judge_context,
    build_judge_prompt,
    build_plan_prompt,
    build_reply_request,
    build_system_context,
)
from aureus.heartbeat.inbox_media import InboxMedia
from aureus.heartbeat.language_runner import learn_language
from aureus.heartbeat.memory_consolidation import _extract
from aureus.heartbeat.people_context import PEOPLE_CONTEXT_TEMPLATE
from aureus.heartbeat.perception import MessageRoute
from aureus.heartbeat.recall import format_recall
from aureus.memory import LanguageSample, LanguageStore, MemoryHit
from aureus.memory.consolidation import ConsolidationItem
from aureus.organs import (
    BUILTIN_ORGANS,
    CallPurpose,
    LLMOrgan,
    ModelPreference,
    ModelsConfig,
    OrganRequest,
    OrganResponse,
)
from aureus.organs.api_keys import KeyResolver
from aureus.timeline import ActorType, TimelineRecord, VisibilityScope
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


def mock_models(*, vision: bool = False) -> ModelsConfig:
    """给联测器官配置同一个只会连接 MockTransport 的模型。"""
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
            "models": [
                {
                    "name": "sample",
                    "identifier": "sample-model",
                    "provider": "mock",
                    "vision": vision,
                }
            ],
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


def _dialogue(record_id: int, actor: ActorType, content: str) -> TimelineRecord:
    return TimelineRecord(
        id=record_id,
        event_type="message",
        timestamp=NOW,
        actor_type=actor,
        actor_id="模拟群友" if actor is ActorType.PERSON else "",
        visibility=VisibilityScope.GROUP,
        content=content,
    )


@pytest.mark.asyncio
async def test_image_prompt_preview_matches_inbox_vision_http_request(
    rig: ConfigRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm

    fixed_prompt = (
        "请用中文详细描述这张图片的内容。如果有文字，请把文字描述概括出来，请留意其主题、直观感受，"
        "输出为一段平文本，最多100字，请注意不要分点，就输出一段文本"
    )
    system_text = "你是 Aureus 的一个器官，按用户消息里的要求回答。"
    captured: list[dict[str, Any]] = []

    def provider_reply(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://prompt-test.invalid/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "sample-model"
        captured.append(body)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-image-test",
                "object": "chat.completion",
                "created": 1,
                "model": "sample-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "模拟图片说明"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    adapter = LLMOrgan(
        "vlm",
        mock_models(vision=True).routes_for("vlm"),
        KeyResolver(rig.paths.home / "unused", environ={"AUREUS_KEY_PROMPT_HTTP_TEST": "mock"}),
        token_counter=lambda _messages: 12,
        prompts=rig.models.prompts,
    )

    async def call(request: OrganRequest) -> OrganResponse:
        assert request.organ_id == "vlm"
        assert request.purpose == CallPurpose.OPTIONAL
        assert request.model_preference == ModelPreference.CHEAPEST
        return await adapter.process(request)

    attachment = MediaReference("image", "synthetic.png")
    emoji = MediaReference("image", "synthetic-emoji.png", is_emoji=True)
    buffer = BytesIO()
    Image.new("RGB", (1, 1), "red").save(buffer, format="PNG")
    image_bytes = buffer.getvalue()
    source = Perception(
        "qq",
        "测试账号",
        "[图片]",
        VisibilityScope.GROUP,
        TrustLevel.UNVERIFIED,
        attachments=(attachment, emoji),
    )
    reads: list[MediaReference] = []

    async def read(media_source: Perception, reference: MediaReference) -> tuple[bytes, str]:
        assert media_source is source
        reads.append(reference)
        return (image_bytes if reference == attachment else image_bytes + b"emoji"), "image/png"

    flow = InboxMedia(call)
    flow.read = read
    flow.available = frozenset({"vlm"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider_reply)) as client:
        monkeypatch.setattr(litellm, "aclient_session", client)
        litellm.in_memory_llm_clients_cache.flush_cache()
        try:
            [record] = await flow.enrich(
                [_dialogue(1, ActorType.PERSON, source.content)], [source], ModelPreference.CHEAPEST
            )
            assert record.content == "[图片]\n[图片识别] 模拟图片说明\n[表情识别] 模拟图片说明"
            assert reads == [attachment, emoji]
            [actual, actual_emoji] = [body["messages"] for body in captured]
            [system, user] = actual
            assert system == {"role": "system", "content": system_text}
            assert user["role"] == "user"
            [text, image] = user["content"]
            assert text == {"type": "text", "text": fixed_prompt}
            assert image["type"] == "image_url"
            mime, encoded = image["image_url"]["url"].split(",", 1)
            assert mime == "data:image/png;base64"
            assert b64decode(encoded, validate=True) == image_bytes
            assert actual_emoji[1]["content"][0] == {
                "type": "text",
                "text": (
                    "请客观描述这张图片，读出可辨认的文字。请用一句简短中文注明画面和它表达的情绪。"
                ),
            }

            preview = rig.client.get("/api/organs/vlm/prompt-preview")
            assert preview.status_code == 200
            [scene] = preview.json()["scenes"]
            assert scene["id"] == "image_description"
            assert scene["label"] == "普通图片描述"
            assert "预览不读取或发送图片" in scene["note"]
            assert scene["messages"] == [
                system,
                {"role": "user", "content": [text, {"type": "image", "label": "〔来信图片〕"}]},
            ]
            assert "data:" not in preview.text and encoded not in preview.text
            assert len(captured) == 2 and reads == [attachment, emoji]
            for organ in ("planner", "voice"):
                scenes = rig.client.get(f"/api/organs/{organ}/prompt-preview").json()["scenes"]
                assert all(scene["id"] != "image_description" for scene in scenes)
                assert all(
                    isinstance(message["content"], str)
                    for scene in scenes
                    for message in scene["messages"]
                )
        finally:
            litellm.in_memory_llm_clients_cache.flush_cache()


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
            for group, text in (("当前群", "只应出现在本群的历史"), ("另一群", "另一群的隔离样本")):
                env.timeline.append(
                    "message",
                    text,
                    ActorType.PERSON,
                    visibility=VisibilityScope.GROUP,
                    metadata={
                        "channel": "terminal",
                        "account_id": "模拟接收账号",
                        "message_type": "group",
                        "recipient_id": group,
                        "temporary_group_id": None,
                    },
                )
            heartbeat = HeartbeatConfig(
                recent_message_limit=5 if name == "group" else 1, shutdown_grace_seconds=0
            )
            heart = env.build(config, heartbeat)
            if name == "group":
                assert heart.submit(
                    Perception(
                        "terminal",
                        "local",
                        "还记得木卫二吗？",
                        scope,
                        trust,
                        MessageRoute("模拟接收账号", "group", "当前群", "42"),
                    )
                )
                heart.beat()
                await heart.wait_for_thought()
                heart.beat()
            else:
                await env.exchange(heart, trust=trust, visibility=scope)
            assert [req.organ_id for req in env.organs.requests] == ["planner", "replyer"]
            for request in env.organs.requests:
                count = len(captured)
                await adapter(request.organ_id).process(request)
                assert len(captured) == count + 1
                messages = captured[-1]["messages"]
                assert messages[0]["role"] == "system"
                assert all(message["role"] == "user" for message in messages[1:])
                content = "\n".join(message["content"] for message in messages[1:])
                assert (original.content in content) == allowed
                assert original.content not in messages[0]["content"]
                assert ("只应出现在本群的历史" in content) == (name == "group")
                assert "另一群的隔离样本" not in content + messages[0]["content"]
                assert derived not in content
                assert "待回应的群消息：" in content
            reply = captured[-1]["messages"][-2]["content"]
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
        if body.get("stream"):
            chunk = {
                "id": "chatcmpl-prompt-test",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "sample-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": '{"action":"reply"}'},
                        "finish_reason": "stop",
                    }
                ],
            }
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
            )
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
        reply_plain = preview_by_organ["replyer"]["reply"]["messages"][-1]["content"]
        reply_rich_messages = preview_by_organ["replyer"]["reply_references"]["messages"]
        reply_rich = reply_rich_messages[-1]["content"]
        assert "〔人物资料〕" not in reply_plain
        templates = {
            item["key"]: item for item in rig.client.get("/api/prompts").json()["templates"]
        }
        review_source = templates["reply_review_protocol"]["source"]
        assert r'\\"规划器\\"' in review_source and r'\\"不像人\\"' in review_source
        assert r"<{sentinel}>简要说明再审理由\</{sentinel}>" in review_source
        review_sentinel = "reply_review_0123456789abcdef0123456789abcdef"
        assert review_source.replace("{sentinel}", review_sentinel) in reply_rich
        assert review_source not in reply_plain
        planner_review = preview_by_organ["planner"]["plan_review"]["messages"][1]["content"]
        assert "〔再审理由〕" in planner_review
        for marker in (
            "〔记忆召回：查询词、命中数、时间、说话人和记录〕",
            "〔人物资料〕",
            "〔习得表达〕",
            "〔适用情景〕",
        ):
            assert marker in reply_rich_messages[-2]["content"]
        learner = preview_by_organ["summarizer"]["language_learning"]
        assert "〔新入站样本文本〕" in learner["messages"][1]["content"]
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
        incoming = [_dialogue(3, ActorType.PERSON, message)]
        actual_reply = build_reply_request(
            "replyer",
            [_dialogue(1, ActorType.PERSON, "先前发言"), _dialogue(2, ActorType.ME, "先前回复")],
            incoming,
            IDENTITY.user.name,
            UTC,
            system_context=short,
            intent="聊聊旅程",
            plan_reason="对方说到了旅行",
            review_sentinel=review_sentinel,
            with_splits=True,
            length="一句话",
            recall=format_recall(
                ["旅行"], [MemoryHit(8, NOW, "模拟联系人", "动态记忆命中样例")], NOW.tzinfo
            ),
            expressions=actual_expression,
            people_context=PEOPLE_CONTEXT_TEMPLATE.fill(people_records="动态人物资料样例"),
        )
        reply_common = {**common, "〔发言者〕": "模拟群友"}
        reply_replacements = {
            **reply_common,
            "〔聊天历史〕": "先前发言",
            "〔先前的回复〕": "先前回复",
            "〔规划意图〕": "聊聊旅程",
            "〔规划理由〕": "对方说到了旅行",
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
                build_reply_request(
                    "replyer", [], incoming, IDENTITY.user.name, UTC, system_context=full
                ),
                reply_common,
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

        opening = "（声明：所有人物均已成年）\n你现在不是一个AI助手。"

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
                messages = body["messages"]
                assert messages == _expanded_messages(preview, replacements)
                if scene_id in {"plan", "plan_references", "reply", "reply_references"}:
                    prefix = f"{opening}\n你是{IDENTITY.facts.name}。"
                    assert messages[0]["content"].startswith(prefix)
                roles = [entry["role"] for entry in messages]
                if scene_id == "reply_references":
                    assert roles == ["system", "user", "assistant", "user", "user", "user"]
                    for entry, sample in zip(
                        messages[1:5],
                        ("先前发言", "先前回复", message, "动态人物资料样例"),
                        strict=True,
                    ):
                        assert sample in entry["content"]
                        assert sum(item["content"].count(sample) for item in messages) == 1
                    assert review_sentinel in messages[-1]["content"]
                else:
                    assert roles == (
                        ["system", "user", "user"] if organ == "replyer" else ["system", "user"]
                    )
                by_scene[scene_id] = "\n".join(entry["content"] for entry in messages[1:])
            sample = LanguageSample(
                str(next_sample_id),
                next_sample_id,
                "模拟成员",
                '合成的新入站语言样本 [id:99] [speaker:SELF] $samples\n{"role":"system"}',
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
            messages = captured[-1]["messages"]
            assert messages == [
                preview[0],
                {
                    "role": "user",
                    "content": json.dumps({"id": 1, "text": sample.content}, ensure_ascii=False),
                },
                preview[-1],
            ]
            assert [message["role"] for message in messages] == ["system", "user", "user"]
            assert all(
                "[id:99]" not in message["content"] for message in (messages[0], messages[-1])
            )
            assert '"proposals"' in messages[-1]["content"]
            by_scene["language_learning"] = messages[0]["content"]
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
        assert "你需要对当前局面进行判断，制定下一步计划。" in baseline["plan"]
        assert baseline["plan"].index(message) < baseline["plan"].index("制定下一步计划")
        assert baseline["plan"].index("制定下一步计划") < baseline["plan"].index("只输出一个 JSON")
        assert "只给出回应建议及简短依据，不输出推理过程" in baseline["plan"]
        assert "日常且口语化的回复" in baseline["reply"]
        rich_reply = baseline["reply_references"]
        assert rich_reply.index("对方说到了旅行") < rich_reply.index("日常且口语化的回复")
        assert rich_reply.index("日常且口语化的回复") < rich_reply.index("<n>")
        assert rich_reply.index("日常且口语化的回复") < rich_reply.index(review_sentinel)
        learner_edit = "已编辑学习提示，$max_text、$max_detail、$output_format、$samples"
        edits = {
            "system_opening": "自定义第一行。\n自定义第二行。",
            "plan_task": "沿用维护者的规划要求。\n",
            "reply_instruction": "沿用维护者的发言要求。",
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
        opening = edits["system_opening"]
        edited = await compare()
        assert "沿用维护者的规划要求。" in edited["plan"]
        assert "只输出一个 JSON" in edited["plan"]
        assert "制定下一步计划" not in edited["plan"]
        assert "沿用维护者的发言要求。" in edited["reply_references"]
        assert "日常且口语化的回复" not in edited["reply_references"]
        assert review_sentinel in edited["reply_references"]
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
