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
