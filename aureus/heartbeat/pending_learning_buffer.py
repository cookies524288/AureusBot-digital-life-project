"""MaiBot 1.3.15 会话学习缓冲（GPL-3.0）；来源和接线见 THIRD_PARTY.md。"""

from collections.abc import Callable, Iterable

MAX_PENDING_LEARNING_MESSAGES = 100


class PendingLearningBuffer[Sample]:
    """按消息 ID 去重的会话学习缓冲；超限淘汰最旧素材，避免长期积压。"""

    def __init__(self, message_id: Callable[[Sample], str]) -> None:
        self._message_id = message_id
        self._items: dict[str, Sample] = {}
        self.dropped_count = 0

    def extend(self, items: Iterable[Sample]) -> None:
        """追加去重样本，并在超过容量时淘汰最旧样本。"""
        for item in items:
            self._items.setdefault(self._message_id(item), item)
            if len(self._items) > MAX_PENDING_LEARNING_MESSAGES:
                del self._items[next(iter(self._items))]
                self.dropped_count += 1

    def snapshot(self) -> list[Sample]:
        """返回当前缓冲的有序快照。"""
        return list(self._items.values())

    def discard(self, items: Iterable[Sample]) -> None:
        """只消费已取得执行名额的批次，不影响运行期间收进来的新素材。"""
        for item in items:
            self._items.pop(self._message_id(item), None)
        self.dropped_count = 0

    def __len__(self) -> int:
        return len(self._items)

    def clear(self) -> None:
        """清空待学习样本及淘汰计数。"""
        self._items.clear()
        self.dropped_count = 0

    def waiting_detail(self, minimum: int) -> str:
        """返回面向工作台的当前积压状态说明。"""
        reason = "等待下次裁切补足" if len(self) < minimum else "已达到门槛，等待执行条件允许"
        detail = (
            f"已累计 {len(self._items)}/{minimum} 条有效消息，{reason}"
            f"（最多保留 {MAX_PENDING_LEARNING_MESSAGES} 条）"
        )
        if self.dropped_count:
            detail += f"；已淘汰最旧的 {self.dropped_count} 条素材"
        return detail
