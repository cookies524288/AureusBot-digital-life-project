"""假器官（用于测试和演示）"""

from __future__ import annotations

import json

from aureus.organs.interface import OrganRequest, OrganResponse


class FakeReplyer:
    """假的回复器官，返回固定模板"""

    def __init__(self, organ_id: str = "fake_replyer"):
        self.organ_id = organ_id

    async def process(self, request: OrganRequest) -> OrganResponse:
        """返回一个简单的模板回复（D66、D69：假器官不再读旧的生物模型）。"""
        reply = f"收到。{request.prompt[:20]}... 让我想想。"
        return OrganResponse(
            organ_id=self.organ_id,
            suggestion=reply,
            confidence=0.7,
            metadata={"type": "fake"},
        )


class FakePlanner:
    """假的规划器官，返回固定建议"""

    def __init__(self, organ_id: str = "fake_planner"):
        self.organ_id = organ_id

    async def process(self, request: OrganRequest) -> OrganResponse:
        """返回一个简单的规划建议（D66、D69：假器官不再读旧的生物模型）。"""
        task = request.context.get("task")
        if task in ("reply", "initiative_reply"):
            return await FakeReplyer(self.organ_id).process(request)
        if task == "inbox_select":
            candidates = json.loads(request.prompt.splitlines()[-1])
            private = [item["id"] for item in candidates if item["kind"] == "private"]
            groups = [item["id"] for item in candidates if item["kind"] == "group"]
            plan = json.dumps({"conversations": private[:1] + groups})
        elif task == "initiative_plan":
            plan = '{"reach_out":false,"reason":"模拟规划建议"}'
        else:
            plan = '{"reply":true,"intent":"回应用户","reason":"模拟规划建议"}'
        return OrganResponse(
            organ_id=self.organ_id,
            suggestion=plan,
            confidence=0.8,
            metadata={"type": "fake"},
        )
