"""心跳写进时间线的事件类型（D41）。"""

# 一条对话消息：用户说的，或者它说的。
EVENT_MESSAGE = "message"
# 醒来：metadata 里有停机时长（第 5 条）。
EVENT_WOKE_UP = "woke_up"
# 关闭：每次有序关闭都写，下次醒来据此算出准确的停机时长（第 5、6 条）。
EVENT_SHUT_DOWN = "shut_down"
# 它对自己认知状态的自述变了（D34、第 4 条），只有自己可见。
EVENT_COGNITIVE_STATE = "cognitive_state"
# 关闭时等不到进行中的器官调用，只好取消；这次可能用掉的 token 没有记进账本（第 6 条）。
EVENT_ORGAN_CALL_ABANDONED = "organ_call_abandoned"
# 急停生效和解除（D54）：心跳发现标记文件出现或消失时各记一条，metadata 里有原因。
EVENT_EMERGENCY_STOP = "emergency_stop"
EVENT_EMERGENCY_RESUME = "emergency_resume"
# 压力偏高时的自检（D57）：每次进入偏高状态记一条，内容就是发给用户的自检通知。
EVENT_SELF_CHECK = "self_check"
# 规划器看了刚收到的消息，决定不回复（D59）：只有自己可见，metadata 里有规划器的原话和这次的做法。
EVENT_REPLY_DECLINED = "reply_declined"
# 用户在 identity.toml 里改了设定（D62）：醒来后记一条，metadata 里有改了哪几层、
# 和已经记到哪个版本（下次醒来从它之后找新的修改）。
EVENT_IDENTITY_CHANGED = "identity_changed"
# 判断器官看了刚收到的消息（D68）：只有自己可见，metadata 里有读懂的结果，
# 或者没判断出来的原因（失败、超时、被拒、看不懂）。
EVENT_JUDGMENT = "judgment"
# 激素被触发一次（D69）：只有自己可见，metadata 是 HormoneTrigger.to_metadata() 的样子
# （种类、上涨量、时刻、对象、原因、来源记录序号和来源的可见范围）。重启时靠它还原浓度。
EVENT_HORMONE_TRIGGERED = "hormone_triggered"
# 日程（D74）：记下一条约定、到期提醒、结束（完成、放弃、过期、取消）。只有自己可见，
# metadata 里有条目编号、对象和内容。
EVENT_AGENDA_ADDED = "agenda_added"
EVENT_AGENDA_DUE = "agenda_due"
EVENT_AGENDA_CLOSED = "agenda_closed"
# 主动联系（D76）：每次考虑和之后有没有得到回应，只有自己可见。
EVENT_INITIATIVE_CONSIDERED = "initiative_considered"
EVENT_INITIATIVE_RESULT = "initiative_result"
