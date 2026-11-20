"""身体 body：真实的身体感受（04 号文档）：精力、疲劳、压力、睡眠。"""

from .body import Body, BodyReading
from .energy import UNLIMITED_READING, EnergyReading, EnergyTier, classify, compute_energy
from .samples import BodySample, BodySampleStore, sample_from_reading, sample_of
from .signals import (
    FatigueReading,
    SleepReading,
    SocialNeedReading,
    StressReading,
    compute_fatigue,
    compute_sleep,
    compute_social_need,
    compute_stress,
)

__all__ = [
    "UNLIMITED_READING",
    "Body",
    "BodyReading",
    "BodySample",
    "BodySampleStore",
    "EnergyReading",
    "EnergyTier",
    "FatigueReading",
    "SleepReading",
    "SocialNeedReading",
    "StressReading",
    "classify",
    "compute_energy",
    "compute_fatigue",
    "compute_sleep",
    "compute_social_need",
    "compute_stress",
    "sample_from_reading",
    "sample_of",
]
