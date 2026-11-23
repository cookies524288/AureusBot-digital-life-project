"""心跳如何选择平台收件箱里的未读消息（D94）。"""

from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from aureus.heartbeat.inbox_store import ConversationKey, InboxMessage, InboxStore
from aureus.heartbeat.perception import Perception, TrustLevel
from aureus.heartbeat.planning import Plan
from aureus.people import PeopleDirectory
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, TimelineRecord

EVENT_SPECIAL_CONTACT = "special_contact_changed"
_IDLE_BEFORE_READING = timedelta(minutes=5)
_PRIORITY_LIMIT = 20


class InboxAttention:
    """按用户绑定、特别关心和连续空闲时间选择未读，状态由时间线恢复。"""

    def __init__(
        self,
        store: InboxStore,
        timeline: TimelineDatabase,
        people: PeopleDirectory | None,
        user_accounts: Iterable[tuple[str, str]],
        *,
        enabled: bool,
        read_limit: int,
    ) -> None:
        self.store = store
        self._timeline = timeline
        self._people = people
        self._user_accounts = frozenset(user_accounts)
        self._enabled = enabled
        self._read_limit = read_limit
        self._special: set[int] = set()
        self._idle_since: datetime | None = None
        self._wants_to_read = False
        self.allows: Callable[[ConversationKey], bool] = lambda _conversation: True

    def restore(self) -> None:
        """重放特别关心变动；用户绑定不受规划器改动。"""
        for record in self._timeline.query(
            TimelineQuery(event_type=EVENT_SPECIAL_CONTACT, limit=-1)
        ):
            person_id = record.metadata.get("person_id")
            if type(person_id) is int:
                if record.metadata.get("add") is True:
                    self._special.add(person_id)
                else:
                    self._special.discard(person_id)

    def is_priority(self, perception: Perception) -> bool:
        """只有已验证的发信账号能以用户或特别关心的身份唤醒。"""
        if perception.trust is TrustLevel.UNVERIFIED:
            return False
        if self._people is None:
            return (perception.channel, perception.sender_id) in self._user_accounts
        person = self._people.find(perception.channel, perception.sender_id)
        return person is not None and (person.is_user or person.id in self._special)

    def priority_messages(self) -> list[InboxMessage]:
        """先收绑定用户和特别关心的未读，重启后也会检查。"""
        accounts: dict[str, set[str]] = defaultdict(set)
        users = self._people.user_accounts() if self._people is not None else self._user_accounts
        for platform, account_id in users:
            accounts[platform].add(account_id)
        if self._people is not None:
            for person_id in self._special:
                for platform, account_id in self._people.accounts_of(person_id):
                    accounts[platform].add(account_id)
        if not accounts:
            return []
        messages: list[InboxMessage] = []
        for item in self.store.list_unread_conversations():
            if (senders := accounts.get(item.key.platform)) and self.allows(item.key):
                messages.extend(self._priority_page(item.key, senders))
        return sorted(messages, key=lambda row: row.id)[:_PRIORITY_LIMIT]

    def _priority_page(self, key: ConversationKey, senders: set[str]) -> list[InboxMessage]:
        messages: list[InboxMessage] = []
        after_id = None
        while len(messages) < _PRIORITY_LIMIT:
            remaining = _PRIORITY_LIMIT - len(messages)
            rows = self.store.list_priority_unread(key, senders, after_id=after_id, limit=remaining)
            messages.extend(
                row
                for row in rows
                if not self._timeline.query(
                    TimelineQuery(
                        event_type="message", metadata={"inbox_message_id": row.id}, limit=1
                    )
                )
            )
            if len(rows) < remaining:
                break
            after_id = rows[-1].id
        return messages

    def browse(self, now: datetime, *, can_browse: bool) -> list[InboxMessage]:
        """安全闲暇时先翻私聊，再用剩余条数翻群聊。"""
        if not self._enabled or not can_browse:
            self._idle_since = None
            return []
        if self._idle_since is None or self._idle_since > now:
            self._idle_since = now
        if not self._wants_to_read and now - self._idle_since < _IDLE_BEFORE_READING:
            return []
        self._wants_to_read = False
        self._idle_since = now
        conversations = self.store.list_unread_conversations()
        messages: list[InboxMessage] = []
        for item in sorted(conversations, key=lambda item: item.key.kind != "private"):
            if self.allows(item.key):
                messages.extend(
                    self.store.list_unread(item.key, limit=self._read_limit - len(messages))
                )
                if len(messages) >= self._read_limit:
                    break
        return messages

    def apply_plan(self, plan: Plan, target: Perception, source: TimelineRecord) -> None:
        """采用规划器的翻阅意愿和对本轮已认出联系人的特别关心建议。"""
        if not self._enabled or target.trust is TrustLevel.UNVERIFIED:
            return
        self._wants_to_read |= plan.read_inbox
        if target.route is None or self._people is None or plan.special_contact is None:
            return
        person = self._people.find(target.channel, target.sender_id)
        if person is None or person.is_user or (person.id in self._special) == plan.special_contact:
            return
        self._timeline.append(
            EVENT_SPECIAL_CONTACT,
            "添加特别关心。" if plan.special_contact else "移除特别关心。",
            ActorType.ME,
            metadata={
                "person_id": person.id,
                "add": plan.special_contact,
                "source_record_id": source.id,
                "reason": plan.reason,
            },
        )
        if plan.special_contact:
            self._special.add(person.id)
        else:
            self._special.discard(person.id)
