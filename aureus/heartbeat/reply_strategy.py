"""按精力档位决定这次怎么回复（04 号文档第 4 节的档位表，路线图 1.6，D59）。

行为由代码定，不靠大模型自觉：档位越低，调用越少、模型越便宜、带的对话越短、回复越短。
- 正常（和「不限」）：先由规划器决定回不回、说什么、多长，再由回复器写成话；用首选模型；
- 节省：规划和回复合并成一次调用，从便宜一档的模型开始，对话和回复都变短；
- 低电量、透支保护：合并成一次调用，从最便宜的模型开始，只带最近几条对话，回复一两句。
这里只做选择，不读写任何状态。
"""

from dataclasses import dataclass

from aureus.body import EnergyTier
from aureus.common import HeartbeatConfig
from aureus.heartbeat.planning import ReplyLength
from aureus.organs import ModelPreference

_PREFERENCE_DESCRIPTIONS = {
    ModelPreference.PREFERRED: "首选",
    ModelPreference.CHEAPER: "便宜一档",
    ModelPreference.CHEAPEST: "最便宜",
}


@dataclass(frozen=True)
class ReplyStrategy:
    """一次回复的做法。"""

    # 这一刻的精力档位；没有身体读数时是 None，按正常档处理。
    tier: EnergyTier | None
    # 先调规划器（两次调用），还是合并成一次。
    plan_first: bool
    # 器官从模型列表的哪里开始用。
    model_preference: ModelPreference
    # 带上最近多少条对话（包括这次要回复的）。
    message_limit: int
    # 回复最长多长；None 表示不限，由规划器或模型自己定。
    length_limit: ReplyLength | None

    def describe(self) -> str:
        """一句话说明这次怎么回，放进系统上下文，让它被问起时能如实说。"""
        steps = "规划和回复合并成一次调用"
        if self.plan_first:
            steps = "先想好要不要回、说什么，再写成话"
        parts = [
            steps,
            f"优先用{_PREFERENCE_DESCRIPTIONS[self.model_preference]}的模型",
            f"只看最近 {self.message_limit} 条对话",
        ]
        if self.length_limit is not None:
            parts.append(f"回复控制在{self.length_limit.describe()}")
        return "，".join(parts)

    def to_metadata(self) -> dict[str, object]:
        """写进时间线 metadata 的样子。"""
        return {
            "energy_tier": self.tier.value if self.tier is not None else None,
            "plan_first": self.plan_first,
            "model_preference": self.model_preference.value,
            "message_limit": self.message_limit,
            "length_limit": self.length_limit.value if self.length_limit is not None else None,
        }


def choose_strategy(tier: EnergyTier | None, config: HeartbeatConfig) -> ReplyStrategy:
    """按精力档位选这次回复的做法。

    Args:
        tier: 此刻的精力档位；None（没有身体、还没读到）按正常档处理。
        config: 心跳配置：规划器开关和各档带多少条对话。各档的条数不会超过
            recent_message_limit。

    Returns:
        这次回复的做法。
    """
    full = config.recent_message_limit
    if tier is None or tier in (EnergyTier.NORMAL, EnergyTier.UNLIMITED):
        return ReplyStrategy(
            tier=tier,
            plan_first=config.planning_enabled,
            model_preference=ModelPreference.PREFERRED,
            message_limit=full,
            length_limit=None,
        )
    if tier is EnergyTier.SAVING:
        return ReplyStrategy(
            tier=tier,
            plan_first=False,
            model_preference=ModelPreference.CHEAPER,
            message_limit=min(config.saving_message_limit, full),
            length_limit=ReplyLength.MEDIUM,
        )
    # 低电量和透支保护走同一条路：透支时网关只放行回应用户的调用，这里本来就是。
    return ReplyStrategy(
        tier=tier,
        plan_first=False,
        model_preference=ModelPreference.CHEAPEST,
        message_limit=min(config.low_message_limit, full),
        length_limit=ReplyLength.SHORT,
    )
