"""收件箱的测试（路线图 1.8）：用户最先，同级先来后到，同一个对话的消息一起取。"""

from aureus.attention import Inbox, Priority


def test_an_empty_inbox_gives_nothing() -> None:
    inbox: Inbox[str] = Inbox()

    assert not inbox
    assert len(inbox) == 0
    assert inbox.take_next() == []
    assert inbox.peek_batch() == []
    assert inbox.peek_oldest() is None
    assert inbox.drain() == []


def test_the_same_priority_is_first_come_first_served() -> None:
    inbox: Inbox[str] = Inbox()
    inbox.add("甲1", Priority.CONTACT, "甲")
    inbox.add("乙1", Priority.CONTACT, "乙")

    assert inbox.take_next() == ["甲1"]
    assert inbox.take_next() == ["乙1"]


def test_the_user_goes_before_someone_who_arrived_earlier() -> None:
    inbox: Inbox[str] = Inbox()
    inbox.add("别人1", Priority.CONTACT, "别人")
    inbox.add("用户1", Priority.USER, "用户")

    assert inbox.peek_batch() == ["用户1"]
    assert len(inbox) == 2  # 只是看，没有取出
    assert inbox.take_next() == ["用户1"]
    assert inbox.take_next() == ["别人1"]


def test_messages_of_one_conversation_are_taken_together() -> None:
    inbox: Inbox[str] = Inbox()
    inbox.add("甲1", Priority.CONTACT, "甲")
    inbox.add("乙1", Priority.CONTACT, "乙")
    inbox.add("甲2", Priority.CONTACT, "甲")

    assert inbox.take_next() == ["甲1", "甲2"]
    assert list(inbox) == ["乙1"]


def test_taking_the_user_does_not_take_the_others_conversation() -> None:
    inbox: Inbox[str] = Inbox()
    inbox.add("别人1", Priority.CONTACT, "别人")
    inbox.add("用户1", Priority.USER, "用户")
    inbox.add("别人2", Priority.CONTACT, "别人")
    inbox.add("用户2", Priority.USER, "用户")

    assert inbox.take_next() == ["用户1", "用户2"]
    assert inbox.take_next() == ["别人1", "别人2"]


def test_peek_oldest_ignores_priority() -> None:
    inbox: Inbox[str] = Inbox()
    inbox.add("别人1", Priority.CONTACT, "别人")
    inbox.add("用户1", Priority.USER, "用户")

    assert inbox.peek_oldest() == "别人1"


def test_iterating_lists_in_arrival_order_without_taking() -> None:
    inbox: Inbox[str] = Inbox()
    inbox.add("别人1", Priority.CONTACT, "别人")
    inbox.add("用户1", Priority.USER, "用户")

    assert list(inbox) == ["别人1", "用户1"]
    assert len(inbox) == 2


def test_drain_empties_the_inbox() -> None:
    inbox: Inbox[str] = Inbox()
    inbox.add("甲1", Priority.CONTACT, "甲")
    inbox.add("用户1", Priority.USER, "用户")

    assert inbox.drain() == ["甲1", "用户1"]
    assert not inbox
