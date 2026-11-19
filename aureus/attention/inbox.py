"""收件箱和优先级：先回谁（路线图 1.8，03 号文档「注意力」一节）。

优先级写在 03 号文档里：用户 > 进行中的任务 > 熟人 > 群聊 > 自己的日程 > 闲时探索。
Phase 1 只有前面几种会出现，枚举里先只放用得到的两种，别的等对应的子系统建好再加。

这里只管排队，不读也不写任何状态：取出的顺序完全由优先级和到达的先后决定，
不靠大模型。同一时间只处理一个对话里的消息（D34：前额叶是串行的）。
"""

from collections.abc import Hashable, Iterator
from dataclasses import dataclass
from enum import IntEnum


class Priority(IntEnum):
    """优先级，数字越小越先处理。数字之间留了空，以后插入新的种类不用改已有的。"""

    USER = 0  # 用户本人的消息
    CONTACT = 20  # 其他人的消息


@dataclass(frozen=True)
class _Entry[T]:
    item: T
    priority: Priority
    # 属于哪个对话；同一次只回复同一个对话里的消息，回复才知道发给谁。
    conversation: Hashable
    # 到达的先后，由收件箱自己编号：不用时间戳，同一时刻来的两条也分得出先后。
    order: int


class Inbox[T]:
    """待处理事项的队列。"""

    def __init__(self) -> None:
        self._entries: list[_Entry[T]] = []
        self._counter = 0

    def add(self, item: T, priority: Priority, conversation: Hashable) -> None:
        """放进一件待处理的事。"""
        self._entries.append(_Entry(item, priority, conversation, self._counter))
        self._counter += 1

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)

    def __iter__(self) -> Iterator[T]:
        """按到达先后列出所有待处理的事，不取出。"""
        return iter([entry.item for entry in self._entries])

    def _first(self) -> _Entry[T] | None:
        """优先级最高的里面，最早到的那一件。"""
        if not self._entries:
            return None
        return min(self._entries, key=lambda entry: (entry.priority, entry.order))

    def peek_oldest(self) -> T | None:
        """到得最早的一件（不管优先级），没有时是 None。疲劳偏高时算「已经等了多久」用它。"""
        return self._entries[0].item if self._entries else None

    def _next_batch(self) -> list[_Entry[T]]:
        """下一个要处理的对话里的全部消息：先选出优先级最高、最早到的那一件，再加上同一对话的。"""
        first = self._first()
        if first is None:
            return []
        return [entry for entry in self._entries if entry.conversation == first.conversation]

    def peek_batch(self) -> list[T]:
        """看一眼下一批要处理的消息（同一个对话里的全部，按到达先后），不取出。

        先看再取：组装回复材料时出了错，消息还留在队里（D41）。
        """
        return [entry.item for entry in self._next_batch()]

    def take_next(self) -> list[T]:
        """取出下一个要处理的对话里的全部消息，按到达先后。

        先选出优先级最高、最早到的那一件，再把同一个对话里的其他消息一起取出，
        这样同一个人的几句话合成一次回复；别的对话的消息留在队里。
        """
        taken = self._next_batch()
        if not taken:
            return []
        conversation = taken[0].conversation
        self._entries = [entry for entry in self._entries if entry.conversation != conversation]
        return [entry.item for entry in taken]

    def drain(self) -> list[T]:
        """取出全部，按到达先后。"""
        items = [entry.item for entry in self._entries]
        self._entries = []
        return items
