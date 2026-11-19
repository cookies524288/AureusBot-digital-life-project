"""Aureus 核心模块：认知透明层（旧的生物学状态模型已在 D69 删除，激素在 hormones/）"""

from .cognitive_layer import CognitiveObserver, CognitiveState

__all__ = ["CognitiveObserver", "CognitiveState"]
