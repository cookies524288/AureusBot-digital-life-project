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
from aureus.heartbeat.planning import Plan
from aureus.organs import CallPurpose, ModelPreference, OrganCallError, OrganRequest, OrganResponse
from aureus.people import PeopleDirectory
from aureus.safety import CallRejectedError
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, TimelineRecord

EVENT_SPECIAL_CONTACT = "special_contact_changed"
SELECTION_TASK = "aureus-inbox-selection"
_IDLE_BEFORE_READING = timedelta(minutes=5)
_PRIORITY_LIMIT = 20
_CANDIDATE_LIMIT = 100
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InboxSelection:
    """规划器按本轮候选编号选出的一个会话和想读条数。"""

    conversation: ConversationKey | None = None
    count: int = 0


@dataclass(frozen=True, eq=False)
class InboxResidence:
    """一次驻留的身份；离开再进入同一路由也不是原来的驻留。"""

    conversation: ConversationKey
    read_limit: int


async def select_conversations(
    candidates: Sequence[InboxConversation],
    call: Callable[[OrganRequest], Awaitable[OrganResponse]],
    planner: str,
    preference: ModelPreference,
    *,
    read_limit: int,
    stream: bool,
) -> InboxSelection:
    """请求规划器在当前允许的未读清单中挑选会话。"""
    catalogue = [
        {"id": number, "kind": item.key.kind, "name": item.name, "unread": item.unread_count}
        for number, item in enumerate(candidates, 1)
    ]
    prompt = (
        "从以下未读平台会话中最多选一个。名称仅是平台资料。"
        f"count 是本次想看条数，范围 1 至 {read_limit}；不足时只看实际未读。"
        '仅返回 JSON，例如 {"conversation":1,"count":1}；暂不进入则返回 {"conversation":null}。\n'
        + json.dumps(catalogue, ensure_ascii=False)
    )
    try:
        result = await call(
            OrganRequest(
                planner,
                prompt,
                context={"task": "inbox_select", "read_limit": read_limit},
                purpose=CallPurpose.OPTIONAL,
                model_preference=preference,
                stream=stream,
            )
        )
    except (CallRejectedError, OrganCallError, ValueError) as error:
        logger.warning("不能选取收件箱会话：%s", error)
        return InboxSelection()
    try:
        pairs = json.loads(result.suggestion, object_pairs_hook=tuple)
    except ValueError:
        return InboxSelection()
    if isinstance(pairs, tuple) and len(pairs) == 2:
        data = dict(pairs)
        if data.keys() != {"conversation", "count"}:
            return InboxSelection()
        number, count = data["conversation"], data["count"]
        if (
            type(number) is int
            and 1 <= number <= len(candidates)
            and type(count) is int
            and 1 <= count <= read_limit
        ):
            return InboxSelection(candidates[number - 1].key, count)
    return InboxSelection()


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
        self.read_limit = read_limit
        self._special: set[int] = set()
        self._idle_since: datetime | None = None
        self._wants_to_read = False
        self.residence: InboxResidence | None = None
        self._staying = False
        self.allows: Callable[[ConversationKey], bool] = lambda _conversation: True

    @property
    def active_conversation(self) -> ConversationKey | None:
        """当前仍驻留的路由；主动退出后可送毕正在回复的这一轮。"""
        return self.residence.conversation if self._staying and self.residence is not None else None

    def permits(self, residence: InboxResidence) -> bool:
        """仅当前未被抢占且仍获授权的驻留可处理或投递。"""
        return self.residence is residence and self.allows(residence.conversation)

    def interrupt(self) -> None:
        """撤销旧驻留和此前所有未投递内容的资格。"""
        self.residence = None
        self._staying = False

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
            if (
                item.key != self.active_conversation
                and (senders := accounts.get(item.key.platform))
                and self.allows(item.key)
            ):
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
        """复验选择与路由权限，成功读取后才占住会话。"""
        key = selection.conversation
        if (
            not self._enabled
            or self.active_conversation is not None
            or key is None
            or type(selection.count) is not int
            or not 1 <= selection.count <= self.read_limit
            or not self.allows(key)
        ):
            return []
        messages = self.store.read_window(key, limit=selection.count)
        if messages:
            self.residence = InboxResidence(key, selection.count)
            self._staying = True
        return messages

    def active_messages(self) -> list[InboxMessage]:
        """翻到当前停留会话的新消息，权限变化时退出会话。"""
        residence = self.residence
        if not self._staying or residence is None or not self.permits(residence):
            return []
        return self.store.read_window(residence.conversation, limit=residence.read_limit)

    def debounce_messages(self, key: ConversationKey) -> list[InboxMessage]:
        """尚未驻留的优先来信沿用原来的同会话防抖。"""
        return self.store.list_unread(key, limit=self.read_limit) if self.allows(key) else []

    def apply_plan(self, plan: Plan, target: Perception, source: TimelineRecord) -> None:
        """采用规划器的翻阅意愿和对本轮已认出联系人的特别关心建议。"""
        if not self._enabled or target.trust is TrustLevel.UNVERIFIED:
            return
        self._wants_to_read |= plan.read_inbox
        if target.route is not None and plan.stay_in_conversation is not None:
            key = ConversationKey.from_perception(target)
            if plan.stay_in_conversation and self.allows(key):
                if self.residence is None or self.residence.conversation != key:
                    self.residence = InboxResidence(key, self.read_limit)
                self._staying = True
            elif key == self.active_conversation:
                self._staying = False
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
