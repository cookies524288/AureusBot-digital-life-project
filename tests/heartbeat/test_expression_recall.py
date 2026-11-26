"""表达召回：快速模型选择与失败回退（D119）。"""

import json
from pathlib import Path

import pytest

from aureus.common import LanguageConfig
from aureus.heartbeat.expression_recall import ExpressionRecall
from aureus.heartbeat.planning import Plan
from aureus.memory import ExpressionVectorIndex, LanguageContent, LanguageKind, LanguageStore
from aureus.organs import LOCAL_EXPRESSION_EMBEDDING, OrganCallError, OrganRequest, OrganResponse

SPACE = "synthetic-expression-space"
PLAN = Plan(
    should_reply=True,
    intent="回应",
    length=None,
    reason="",
    reply_reference="合成回复参考",
)


class FakeCall:
    """应答本地嵌入和表达选择，并记录选择请求。"""

    def __init__(self, selected: list[object] | None = None, failure: Exception | None = None):
        self.requests: list[OrganRequest] = []
        self.selected = selected
        self.failure = failure

    async def __call__(self, request: OrganRequest) -> OrganResponse:
        if request.organ_id == LOCAL_EXPRESSION_EMBEDDING:
            return OrganResponse(
                request.organ_id,
                "",
                metadata={"embedding_space": SPACE},
                embeddings=((1.0, 0.0),),
            )
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return OrganResponse(request.organ_id, json.dumps({"selected_ids": self.selected}))

    @property
    def selection_requests(self) -> list[OrganRequest]:
        return [
            request
            for request in self.requests
            if request.context.get("task") == "expression_selection"
        ]


def build_recall(
    tmp_path: Path,
    count: int = 20,
    *,
    selected: list[object] | None = None,
    failure: Exception | None = None,
) -> tuple[ExpressionRecall, FakeCall]:
    store = LanguageStore(tmp_path / "language.sqlite")
    store.open()
    for index in range(count):
        store.add_entry(
            LanguageKind.EXPRESSION,
            LanguageContent(f"说法{index:02}", f"场景{index:02}"),
        )
    index = ExpressionVectorIndex(tmp_path / "language.sqlite")
    entries = store.expression_candidates(None)
    index.publish(SPACE, entries, [(1.0, position / 100) for position in range(len(entries))])
    call = FakeCall(selected, failure)
    recall = ExpressionRecall(store, index, LanguageConfig(), call)
    recall.space = SPACE
    return recall, call


def _candidate_styles(prompt: str) -> list[str]:
    block = prompt.split("候选表达方式：\n", 1)[1]
    return [line.split("风格=")[1] for line in block.splitlines()]


@pytest.mark.asyncio
async def test_expression_selection_uses_fast_model(tmp_path: Path) -> None:
    """召回后交给 fast_model 按 0–5 条协议挑选。"""
    recall, call = build_recall(tmp_path, selected=[2])
    block = await recall.reference(PLAN, None)
    assert block is not None
    [request] = call.selection_requests
    assert request.organ_id == "fast_model"
    assert request.context["task"] == "expression_selection"
    assert "0 到 5 条" in request.prompt
    assert '{"selected_ids":[1,2]}' in request.prompt
    styles = _candidate_styles(request.prompt)
    assert len(styles) == 20
    assert f"可用「{styles[1]}」" in block
    assert f"可用「{styles[0]}」" not in block


@pytest.mark.asyncio
async def test_expression_selection_falls_back_to_top_five(tmp_path: Path) -> None:
    """没有 fast_model 或调用失败时退回向量前 5，不整池注入。"""
    failures: list[Exception] = [
        ValueError("器官 fast_model 未注册"),
        OrganCallError("fast_model", ["合成失败"]),
    ]
    for failure in failures:
        recall, _call = build_recall(tmp_path, failure=failure)
        block = await recall.reference(PLAN, None)
        assert block is not None
        assert len([line for line in block.splitlines() if line.startswith("- 当「")]) == 5


@pytest.mark.asyncio
async def test_expression_selection_empty_array_injects_nothing(tmp_path: Path) -> None:
    """选择结果为空数组时本轮不注入表达块。"""
    recall, _call = build_recall(tmp_path, selected=[])
    assert await recall.reference(PLAN, None) == ""


@pytest.mark.asyncio
async def test_expression_selection_filters_invalid_ids(tmp_path: Path) -> None:
    """越界、重复和非整数编号被过滤，全部无效按空数组处理。"""
    recall, _call = build_recall(tmp_path, selected=[0, 1, 1, "2", 99, 2.0])
    block = await recall.reference(PLAN, None)
    assert block is not None
    assert len([line for line in block.splitlines() if line.startswith("- 当「")]) == 1
