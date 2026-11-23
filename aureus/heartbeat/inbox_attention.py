"""心跳如何选择平台收件箱里的未读消息（D94）。"""

from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from aureus.heartbeat.inbox_store import InboxMessage, InboxStore
from aureus.heartbeat.perception import Perception, TrustLevel
from aureus.heartbeat.planning import Plan
from aureus.people import PeopleDirectory
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, TimelineRecord

EVENT_SPECIAL_CONTACT = "special_contact_changed"
_IDLE_BEFORE_READING = timedelta(minutes=5)
_READ_LIMIT = 20


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
    ) -> None:
        self.store = store
        self._timeline = timeline
        self._people = people
        self._user_accounts = frozenset(user_accounts)
        self._enabled = enabled
        self._special: set[int] = set()
        self._idle_since: datetime | None = None
        self._wants_to_read = False
        self.allows: Callable[[Perception], bool] = lambda _perception: True

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
        if (perception.channel, perception.sender_id) in self._user_accounts:
            return True
        person = (
            self._people.find(perception.channel, perception.sender_id) if self._people else None
        )
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
        conversations = self.store.list_conversations()
        receivers = {
            (item.key.platform, item.key.account_id)
            for item in conversations
            if item.unread_count and accounts.get(item.key.platform)
        }
        messages = [
            row
            for platform, account_id in receivers
            for row in self.store.list_priority_unread(
                platform, account_id, accounts[platform], limit=_READ_LIMIT
            )
            if self.allows(row.perception) and self.is_priority(row.perception)
        ]
        return sorted(messages, key=lambda row: row.id)[:_READ_LIMIT]

    def browse(self, now: datetime, *, can_browse: bool) -> list[InboxMessage]:
        """可选翻阅只在安全闲暇时进行，每次只翻一个会话。"""
        if not self._enabled or not can_browse:
            self._idle_since = None
            return []
        if self._idle_since is None or self._idle_since > now:
            self._idle_since = now
        if not self._wants_to_read and now - self._idle_since < _IDLE_BEFORE_READING:
            return []
        self._wants_to_read = False
        self._idle_since = now
        for item in sorted(self.store.list_conversations(), key=lambda item: item.last_message_id):
            if item.unread_count:
                messages = self.store.list_unread(item.key, limit=_READ_LIMIT)
                if messages and self.allows(messages[0].perception):
                    return messages
        return []

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
