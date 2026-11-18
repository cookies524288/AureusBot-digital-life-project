"""身体 body：真实的身体感受（04 号文档）。首批是精力，疲劳、压力、睡眠在 1.4。"""

from .body import Body
from .energy import UNLIMITED_READING, EnergyReading, EnergyTier, classify, compute_energy

__all__ = [
    "UNLIMITED_READING",
    "Body",
    "EnergyReading",
    "EnergyTier",
    "classify",
    "compute_energy",
]
