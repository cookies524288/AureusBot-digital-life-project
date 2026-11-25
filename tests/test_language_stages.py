"""用合成学习结果验证独立抽取、会话证据和黑话推断。"""

import json
from pathlib import Path

import pytest

from aureus.heartbeat.language_runner import learn_language
from aureus.memory import LanguageKind, LanguageSample, LanguageStore
from aureus.organs import OrganRequest, OrganResponse
from aureus.safety import CallRejectedError


@pytest.mark.asyncio
async def test_distinct_extractions_promote_only_compared_jargon(tmp_path: Path) -> None:
    store = LanguageStore(tmp_path / "language.sqlite")
    store.open()
    requests: list[OrganRequest] = []

    async def call(request: OrganRequest) -> OrganResponse:
        requests.append(request)
        task = request.context["task"]
        candidates: dict[str, object] = {
            "learn_style": [{"style": "使用 月兔冲个XX", "situation": "跑步庆祝", "id": "1"}],
            "jargon_inference_with_context": {"meaning": "抵达终点", "no_info": False},
            "jargon_inference_content_only": {"meaning": "游戏结束"},
            "jargon_compare_inference": {"is_similar": False, "reason": "场景含义不同"},
        }
        if task == "learn_jargon":
            candidates[task] = [
                {"content": term, "id": str(json.loads(message)["id"])}
                for _, message in request.preceding_messages
                for term in ("GG", "伪造词")
            ]
        return OrganResponse("summarizer", json.dumps(candidates.get(task, {}), ensure_ascii=False))

    samples = [
        LanguageSample(f"event:{number}", number, "跑步人", f"月兔冲个线！第{number}次 GG")
        for number in range(1, 5)
    ]

    async def learn(batch: list[LanguageSample], scope: str = "甲群") -> None:
        await learn_language(store, "summarizer", call, batch, conversation_key=scope)

    await learn(samples[:2])
    await learn(samples[2:], "乙群")
    assert [request.context["task"] for request in requests] == ["learn_style", "learn_jargon"] * 2
    assert not store.entries(LanguageKind.JARGON)
    new = [
        LanguageSample(f"event:{number}", number, "跑步人", f"月兔冲个线！第{number}次 GG")
        for number in range(5, 7)
    ]
    await learn(new)
    await learn(new)
    assert [request.context["task"] for request in requests[4:]] == [
        "learn_style",
        "learn_jargon",
        "jargon_inference_with_context",
        "jargon_inference_content_only",
        "jargon_compare_inference",
    ]
    [entry] = store.entries(LanguageKind.JARGON)
    assert entry.text == "GG" and entry.evidence_ids == (1, 2, 5, 6)
    assert "抵达终点" in store.jargon_reference(["GG"], conversation_key="甲群")
    assert "抵达终点" in store.jargon_reference(["GG"], conversation_key="乙群")
    assert "抵达终点" in store.jargon_reference(["GG"])
    expressions = store.entries(LanguageKind.EXPRESSION)
    assert {item.text for item in expressions} == {"月兔冲个XX"}
    assert {item.evidence_ids for item in expressions} == {(1, 5), (3,)}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rejected", "invalid_json"])
async def test_failed_extraction_keeps_sources_for_next_attempt(
    tmp_path: Path, failure: str
) -> None:
    store = LanguageStore(tmp_path / "language.sqlite")
    store.open()
    sample = LanguageSample("retry-source", 1, "speaker", "GG，月兔冲线。")
    should_fail = True

    async def call(request: OrganRequest) -> OrganResponse:
        nonlocal should_fail
        task = str(request.context["task"])
        if task == "learn_style" and should_fail:
            should_fail = False
            if failure == "rejected":
                raise CallRejectedError(request.organ_id, "合成预算拒绝")
            return OrganResponse(request.organ_id, "{")
        if task == "learn_style":
            return OrganResponse(
                request.organ_id,
                json.dumps([{"id": 1, "style": "月兔冲线", "situation": "庆祝"}]),
            )
        return OrganResponse(request.organ_id, "[]")

    assert not await learn_language(store, "summarizer", call, [sample], conversation_key="会话甲")
    assert store.pending_extraction(LanguageKind.EXPRESSION, [sample]) == [sample]
    assert await learn_language(store, "summarizer", call, [sample], conversation_key="会话甲")
    assert store.pending_extraction(LanguageKind.EXPRESSION, [sample]) == []
    assert [entry.text for entry in store.entries(LanguageKind.EXPRESSION)] == ["月兔冲线"]


@pytest.mark.asyncio
async def test_extraction_failure_is_independent_and_jargon_retries_at_new_evidence(
    tmp_path: Path,
) -> None:
    store = LanguageStore(tmp_path / "language.sqlite")
    store.open()
    tasks: list[str] = []
    style_blocked = True
    comparison: bool | str = "invalid"
    contextual_counts: list[int] = []

    async def call(request: OrganRequest) -> OrganResponse:
        task = str(request.context["task"])
        tasks.append(task)
        result: object
        if task == "learn_style":
            if style_blocked:
                raise CallRejectedError(request.organ_id, "合成拒绝")
            result = [{"id": "1", "style": "慢慢XX", "situation": "散步"}]
        elif task == "learn_jargon":
            result = [
                {"content": term, "id": json.loads(message)["id"]}
                for _, message in request.preceding_messages
                for term in ("GG", "截断黑话")
            ]
        elif task == "jargon_inference_with_context":
            count = len(request.preceding_messages)
            contextual_counts.append(count)
            result = {"meaning": "" if count == 4 else "群内集合", "no_info": count == 4}
            assert "截断黑话" not in request.prompt
        elif task == "jargon_inference_content_only":
            assert not request.preceding_messages
            result = {"meaning": "网络致意"}
        else:
            result = {"is_similar": comparison}
        return OrganResponse("summarizer", json.dumps(result, ensure_ascii=False))

    samples = [
        LanguageSample(
            f"source:{number}", number, "人", f"GG 第{number}次" + "文" * 400 + "截断黑话"
        )
        for number in range(1, 10)
    ]

    async def learn(batch: list[LanguageSample]) -> None:
        await learn_language(store, "summarizer", call, batch, conversation_key="甲群")

    await learn(samples[:4])
    assert tasks == ["learn_style", "learn_jargon", "jargon_inference_with_context"]
    assert not store.entries(LanguageKind.EXPRESSION)
    assert not store.entries(LanguageKind.JARGON)
    style_blocked = False
    tasks.clear()
    await learn(samples[:4])
    await learn(samples[:4])
    assert tasks == ["learn_style"]
    assert store.entries(LanguageKind.EXPRESSION)[0].evidence_ids == (1,)
    await learn(samples[4:7])
    assert contextual_counts == [4]
    await learn(samples[7:8])
    assert contextual_counts == [4, 8]
    assert not store.entries(LanguageKind.JARGON)
    await learn(samples[7:8])
    assert contextual_counts == [4, 8]
    comparison = True
    await learn(samples[8:])
    assert contextual_counts == [4, 8, 9]
    assert not store.jargon_reference(["GG"], conversation_key="甲群")
