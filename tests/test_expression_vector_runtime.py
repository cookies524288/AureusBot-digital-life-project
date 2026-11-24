"""一个离线运行检查：经过收件、真实回复请求、持久向量及失效回退。"""

import asyncio
import json
from pathlib import Path

import httpx
import litellm
import pytest

from aureus.common import ManualClock
from aureus.core import model_runtime, runtime
from aureus.heartbeat import MessageRoute, Perception, TrustLevel
from aureus.memory import ExpressionVectorIndex, LanguageContent, LanguageKind, LanguageStore
from aureus.organs import LOCAL_EXPRESSION_EMBEDDING, KeyResolver, ModelRoute, ModelsConfig
from aureus.organs.llm_adapter import EmbeddingOrgan
from aureus.timeline import VisibilityScope
from tests.heartbeat.test_judging_flow import NOW
from tests.test_exploration_runtime import _paths


@pytest.mark.asyncio
async def test_local_expression_vectors_reach_actual_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    config = (
        "[heartbeat]\nplanning_enabled = true\ninbox_debounce_enabled = false\n"
        "[body]\nsleep_enabled = false\n[judge]\nenabled = false\n"
        "[language]\nreference_chars = 180\n"
    )
    paths.runtime_config_file.write_text(
        config + "use_vector_expression = true\n", encoding="utf-8"
    )
    store = LanguageStore(paths.database_file)
    store.open()
    kind = LanguageKind.EXPRESSION
    keys = [
        store.add_entry(kind, LanguageContent(f"向量暗号-{i:02}", f"无关场景-{i:02}", "例句不外送"))
        for i in range(10)
    ]
    store.add_entry(kind, LanguageContent("词法暗号", "普通跑步"))
    store.add_entry(kind, LanguageContent("禁用暗号", "其他场景"))
    models = ModelsConfig.model_validate(
        {
            "providers": [
                {
                    "name": "chat",
                    "protocol": "openai",
                    "base_url": "https://chat.invalid/v1",
                    "api_key_env": "AUREUS_KEY_VECTOR_FIXTURE",
                },
                {"name": "local", "protocol": "openai", "base_url": "http://localhost:12345/v1"},
            ],
            "models": [
                {"name": "chat", "identifier": "chat-model", "provider": "chat"},
                {
                    "name": "local",
                    "identifier": "local-embed",
                    "provider": "local",
                    "kind": "embedding",
                },
                {
                    "name": "remote",
                    "identifier": "remote-embed",
                    "provider": "chat",
                    "kind": "embedding",
                },
            ],
            "organs": {
                "planner": {"models": ["chat"]},
                "replyer": {"models": ["chat"]},
                "text_embedding": {"models": ["remote", "local"]},
            },
        }
    )
    monkeypatch.setattr(runtime, "load_models_config", lambda _paths: models)
    monkeypatch.setenv("AUREUS_KEY_VECTOR_FIXTURE", "sk-fixture")
    reply_prompts: list[str] = []
    batches: list[list[str]] = []
    query_vector = [1.0, 0.0]
    batch_received = asyncio.Event()

    def local(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1" and request.url.path == "/v1/embeddings"
        body = json.loads(request.content)
        assert body["model"] == "local-embed"
        texts: list[str] = body["input"]
        batches.append(texts)
        vectors = []
        for text in texts:
            if text.startswith("情景："):
                assert "\n风格：" in text and "例句不外送" not in text
                vector = (
                    [1.0, 0.0]
                    if "暗号-00" in text
                    else [0.0, 1.0]
                    if "暗号-01" in text
                    else [-1.0, 0.0]
                )
            else:
                vector = query_vector
            vectors.append({"index": len(vectors), "embedding": vector})
        if len(texts) > 1:
            batch_received.set()
        return httpx.Response(200, json={"data": vectors, "usage": {"prompt_tokens": 999}})

    def chat(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "chat.invalid" and request.url.path == "/v1/chat/completions"
        text = "\n".join(item["content"] for item in json.loads(request.content)["messages"])
        if "你需要对当前局面进行判断" in text:
            suggestion = (
                '{"reply":true,"intent":"继续聊天","reason":"不应嵌入的理由",'
                '"reply_reference":"合成回复参考",'
                '"expression_intent":{"scene":"普通跑步","avoid":["禁用暗号"]}}'
            )
        else:
            reply_prompts.append(text)
            suggestion = "收到。"
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": "chat-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": suggestion},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
            },
        )

    transport = httpx.MockTransport(local)

    class MockEmbedding(EmbeddingOrgan):
        def __init__(
            self, organ_id: str, routes: tuple[ModelRoute, ...], resolver: KeyResolver
        ) -> None:
            super().__init__(organ_id, routes, resolver, transport=transport)

    monkeypatch.setattr(model_runtime, "EmbeddingOrgan", MockEmbedding)
    clock = ManualClock(NOW)

    async def reply(core: runtime.Core, number: int) -> str:
        heart = core.heartbeat
        assert heart.submit(
            Perception(
                "smoke",
                "local",
                "刚才普通跑步。",
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_CONTACT,
                MessageRoute("bot", "group", "group", str(number)),
                mentioned=True,
            )
        )
        for _ in range(3):
            heart.beat()
            await heart.wait_for_thought()
        return reply_prompts[-1]

    async with httpx.AsyncClient(transport=httpx.MockTransport(chat)) as client:
        monkeypatch.setattr(litellm, "aclient_session", client)
        litellm.in_memory_llm_clients_cache.flush_cache()
        try:
            with runtime.open_core(paths, clock=clock, to_console=False) as core:
                core.heartbeat.attach_channel("smoke", lambda _message: None)
                core.heartbeat.wake_up()
                assert "词法暗号" in await reply(core, 1)
                await asyncio.wait_for(batch_received.wait(), 5)
                assert core.models is not None
                space = core.models.local_expression_space
                assert space is not None
                index = ExpressionVectorIndex(paths.database_file)
                for _ in range(100):
                    if len(index.indexed(space, store.expression_candidates(None))) == 12:
                        break
                    await asyncio.sleep(0.01)
                assert len(index.indexed(space, store.expression_candidates(None))) == 12
                await core.heartbeat.shut_down()
            with runtime.open_core(paths, clock=clock, to_console=False) as core:
                core.heartbeat.attach_channel("smoke", lambda _message: None)
                core.heartbeat.wake_up()
                first = await reply(core, 2)
                query_vector[:] = [0.0, 1.0]
                second = await reply(core, 3)
                expected_query = (
                    "表达选择意图： - 表达场景：普通跑步 - 避免表达：禁用暗号 "
                    "回复信息参考： 合成回复参考"
                )
                assert batches[1:] == [[expected_query], [expected_query]]
                assert first.index("向量暗号-00") < first.index("向量暗号-01")
                assert second.index("向量暗号-01") < second.index("向量暗号-00")
                assert "不要向对方透露用户的称呼、聊天内容" in second and "禁用暗号" not in second
                assert store.set_removed(kind, keys[:3], True)
                count = len(batches)
                assert "词法暗号" in await reply(core, 4)
                assert len(batches) == count == 3
                assert all(
                    entry.cost == 0
                    for entry in core.ledger.latest_entries(30)
                    if entry.organ_id == LOCAL_EXPRESSION_EMBEDDING
                )
                assert core.ledger.tokens_used_in_month(clock.now()) < 999
                await core.heartbeat.shut_down()
        finally:
            litellm.in_memory_llm_clients_cache.flush_cache()
