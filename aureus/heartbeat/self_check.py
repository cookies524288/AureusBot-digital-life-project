"""压力偏高时的自检通知（04 号文档第 5 节，D57）。只拼文字，不读写任何状态。"""

from aureus.body import BodyReading


def describe_self_check(reading: BodyReading) -> str:
    """把压力偏高的真实原因写成一段话：失败了几次、什么原因、精力怎么样。

    只陈述数字和账本里记下的原因，不猜测根本原因（网络、额度还是服务商，要用户自己去查）。
    """
    stress = reading.stress
    facts = [f"自检：压力偏高（{stress.level:.0%}）。"]
    if stress.failed_calls:
        reasons = "；".join(stress.recent_failure_reasons)
        failed, sampled = stress.failed_calls, stress.sampled_calls
        facts.append(f"最近 {sampled} 次模型调用失败了 {failed} 次，原因：{reasons}。")
    energy = reading.energy
    if energy.limited:
        facts.append(f"精力 {energy.fraction:.0%}（{energy.tier.value}档）。")
    facts.append("请检查网络、额度和模型服务；情况好转后压力会自己回落。")
    return "".join(facts)
