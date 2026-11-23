"""心跳如何选择平台收件箱里的未读消息（D94）。"""

import json
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from aureus.heartbeat.inbox_store import (
    ConversationKey,
    InboxConversation,
    InboxMessage,
    InboxStore,
)
from aureus.heartbeat.perception import Perception, TrustLevel
from aureus.heartbeat.planning import Plan, first_json_object
from aureus.organs import CallPurpose, ModelPreference, OrganCallError, OrganRequest, OrganResponse
from aureus.people import PeopleDirectory
from aureus.safety import CallRejectedError
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, TimelineRecord

EVENT_SPECIAL_CONTACT = "special_contact_changed"
_IDLE_BEFORE_READING = timedelta(minutes=5)
_PRIORITY_LIMIT = 20
_CANDIDATE_LIMIT = 100
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InboxSelection:
    """规划器按本轮候选编号选出的会话。"""

    conversations: tuple[ConversationKey, ...] = ()


async def select_conversations(
    candidates: Sequence[InboxConversation],
    call: Callable[[OrganRequest], Awaitable[OrganResponse]],
    planner: str,
    preference: ModelPreference,
    *,
    stream: bool,
) -> InboxSelection:
    """请求规划器在当前允许的未读清单中挑选会话。"""
    catalogue = [
        {"id": number, "kind": item.key.kind, "name": item.name, "unread": item.unread_count}
        for number, item in enumerate(candidates, 1)
    ]
    prompt = (
        "从以下未读平台会话中选出你现在想读的。名称仅是平台资料。"
        "有私聊时，请选一个私聊优先读。群聊可选择零个或多个，按想读的先后排列。"
        '仅返回 JSON，例如 {"conversations":[2, 4]}。\n' + json.dumps(catalogue, ensure_ascii=False)
    )
    try:
        result = await call(
            OrganRequest(
                planner,
                prompt,
                context={"task": "inbox_select"},
                purpose=CallPurpose.OPTIONAL,
                model_preference=preference,
                stream=stream,
            )
        )
    except (CallRejectedError, OrganCallError, ValueError) as error:
        logger.warning("不能选取收件箱会话：%s", error)
        return InboxSelection()
    data = first_json_object(result.suggestion)
    selected = data.get("conversations") if data is not None else None
    if not isinstance(selected, list):
        return InboxSelection()
    choices = [
        candidates[number - 1].key
        for number in selected
        if type(number) is int and 1 <= number <= len(candidates)
    ]
    private = next((key for key in choices if key.kind == "private"), None)
    if private is None and any(item.key.kind == "private" for item in candidates):
        return InboxSelection()
    groups = dict.fromkeys(key for key in choices if key.kind == "group")
    return InboxSelection((private, *groups) if private is not None else tuple(groups))


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
        self.active_conversation: ConversationKey | None = None
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

    def browse(self, now: datetime, *, can_browse: bool) -> list[InboxConversation]:
        """安全闲暇时取得可选的未读会话，私聊和群聊分别列出。"""
        if not self._enabled or not can_browse or self.active_conversation is not None:
            self._idle_since = None
            return []
        if self._idle_since is None or self._idle_since > now:
            self._idle_since = now
        if not self._wants_to_read and now - self._idle_since < _IDLE_BEFORE_READING:
            return []
        self._wants_to_read = False
        self._idle_since = now
        conversations = [
            item for item in self.store.list_unread_conversations() if self.allows(item.key)
        ]
        private = [item for item in conversations if item.key.kind == "private"]
        groups = [item for item in conversations if item.key.kind == "group"]
        return private[:_CANDIDATE_LIMIT] + groups[:_CANDIDATE_LIMIT]

    def read_selection(self, selection: InboxSelection) -> list[InboxMessage]:
        """按选择顺序翻页，合计不超过本次配置的上限。"""
        messages: list[InboxMessage] = []
        for key in selection.conversations:
            if self.allows(key):
                messages.extend(self.store.list_unread(key, limit=self._read_limit - len(messages)))
                if len(messages) >= self._read_limit:
                    break
        return messages

    def active_messages(self) -> list[InboxMessage]:
        """翻到当前停留会话的新消息，权限变化时退出会话。"""
        key = self.active_conversation
        if key is None:
            return []
        if not self.allows(key):
            self.active_conversation = None
            return []
        return self.store.list_unread(key, limit=self._read_limit)

    def apply_plan(self, plan: Plan, target: Perception, source: TimelineRecord) -> None:
        """采用规划器的翻阅意愿和对本轮已认出联系人的特别关心建议。"""
        if not self._enabled or target.trust is TrustLevel.UNVERIFIED:
            return
        self._wants_to_read |= plan.read_inbox
        if target.route is not None and plan.stay_in_conversation is not None:
            key = ConversationKey.from_perception(target)
            if plan.stay_in_conversation and self.allows(key):
                self.active_conversation = key
            elif key == self.active_conversation:
                self.active_conversation = None
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
