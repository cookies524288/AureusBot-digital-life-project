"""安全网关抛出的异常（D39、D40，06 号文档 12.1 节）。

消息是中文，说明为什么拒绝；不含密钥和提示词内容。
"""


class CallRejectedError(Exception):
    """网关拒绝了这次器官调用：请求没有发给器官，也没有花 token。

    怎么降级由调用方决定，网关不编默认回复（D35）。
    """

    def __init__(self, organ_id: str, reason: str) -> None:
        self.organ_id = organ_id
        self.reason = reason
        super().__init__(f"网关拒绝调用器官 {organ_id}：{reason}")


class BudgetExceededError(CallRejectedError):
    """本月 token 已经用到上限。"""


class RunawayBlockedError(CallRejectedError):
    """失控拦截：这个器官调用太频繁，或者连续失败后正在暂停。"""


class LedgerUnavailableError(CallRejectedError):
    """账本读不了或写不进去，用量没法记，所以不放行（D40）。"""
