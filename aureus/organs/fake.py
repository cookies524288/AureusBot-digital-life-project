"""假器官（用于测试和演示）"""

from __future__ import annotations

from aureus.organs.interface import OrganRequest, OrganResponse


class FakeReplyer:
    """假的回复器官，返回固定模板"""

    def __init__(self, organ_id: str = "fake_replyer"):
        self.organ_id = organ_id

    async def process(self, request: OrganRequest) -> OrganResponse:
        """返回一个简单的模板回复"""
        bio_state = request.bio_state
        emotion = bio_state.compute_emotion()
        valence = emotion.compute_valence()

        # 根据情绪生成不同的回复
        if valence > 0.6:
            reply = f"好的！{request.prompt[:20]}... 我很开心能帮你。"
        elif valence < 0.4:
            reply = f"嗯... {request.prompt[:20]}... 我觉得有点累。"
        else:
            reply = f"收到。{request.prompt[:20]}... 让我想想。"

        return OrganResponse(
            organ_id=self.organ_id,
            suggestion=reply,
            confidence=0.7,
            metadata={"type": "fake", "emotion_used": emotion.system_prompt_modifier()},
        )


class FakePlanner:
    """假的规划器官，返回固定建议"""

    def __init__(self, organ_id: str = "fake_planner"):
        self.organ_id = organ_id

    async def process(self, request: OrganRequest) -> OrganResponse:
        """返回一个简单的规划建议"""
        bio_state = request.bio_state
        cognitive = bio_state.observe_cognitive_state({"task": "规划任务"})

        # 根据规划信心（激素算出来的）生成不同的规划
        if cognitive.planning_confidence > 0.7:
            plan = "建议：开始一项长期任务，深入分析问题。"
        elif cognitive.planning_confidence > 0.4:
            plan = "建议：处理中等难度任务，保持专注。"
        else:
            plan = "建议：处理简单任务或休息。"

        return OrganResponse(
            organ_id=self.organ_id,
            suggestion=plan,
            confidence=0.8,
            metadata={
                "type": "fake",
                "confidence_used": cognitive.planning_confidence,
                "planning_depth": cognitive.planning_depth,
            },
        )
