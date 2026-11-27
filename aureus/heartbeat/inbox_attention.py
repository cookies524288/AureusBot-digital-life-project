"""心跳如何选择平台收件箱里的未读消息（D94）。"""

import json
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from aureus.common import Clock
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
MENTION_PULL_LIMIT = 3
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


@dataclass(frozen=True)
class MentionPull:
    """其他会话的展示名和待处理提及数；路由只用于内部核对。"""

    conversation: ConversationKey
    name: str
    count: int
    special_count: int = 0


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
    """按优先级和连续空闲时间选择未读，管理当次运行中的会话驻留。"""

    def __init__(
        self,
        store: InboxStore,
        timeline: TimelineDatabase,
        people: PeopleDirectory | None,
        user_accounts: Iterable[tuple[str, str]],
        *,
        enabled: bool,
        read_limit: int,
        clock: Clock,
        residence_seconds: float,
        mention_pull_enabled: bool,
    ) -> None:
        self.store = store
        self._timeline = timeline
        self._clock = clock
        self._people = people
        self._user_accounts = frozenset(user_accounts)
        self._enabled = enabled
        self.read_limit = read_limit
        self._residence_window = timedelta(seconds=residence_seconds) if residence_seconds else None
        self._mention_pull_enabled = mention_pull_enabled
        self._special: set[int] = set()
        self._idle_since: datetime | None = None
        self._wants_to_read = False
        self.residence: InboxResidence | None = None
        self._residence_deadline: datetime | None = None
        self._staying = False
        self.allows: Callable[[ConversationKey], bool] = lambda _conversation: True

    @property
    def active_conversation(self) -> ConversationKey | None:
        """当前仍驻留的路由；主动退出后可送毕正在回复的这一轮。"""
        return self.residence.conversation if self._staying and self.residence is not None else None

    @property
    def deadline(self) -> datetime | None:
        """当前驻留窗口的截止时间；没有计时窗口时为 None。"""
        return self._residence_deadline

    def renew(self, now: datetime) -> None:
        """从最近一次活动重新计时，不改变驻留身份（D148）。"""
        if self.active_conversation is not None and self._residence_window is not None:
            self._residence_deadline = now + self._residence_window

    def expire_if_due(self, now: datetime) -> bool:
        """在调用方确认没有在途工作后结束到期驻留，保留原身份供投递。"""
        if not self._staying or self._residence_deadline is None or now < self._residence_deadline:
            return False
        self._leave()
        self._idle_since = now - _IDLE_BEFORE_READING
        self._wants_to_read = True
        return True

    def mention_pull_summary(
        self,
        exclude: ConversationKey | None,
        *,
        limit: int | None = MENTION_PULL_LIMIT,
        pending_ids: Iterable[int] = (),
    ) -> tuple[MentionPull, ...]:
        """只列获准的会话。授权过滤在截断之前，避免泄露范围外的会话数量。"""
        if not self._mention_pull_enabled:
            return ()
        candidates = self.store.list_mention_conversations(
            limit=-1, pending_ids=pending_ids, special_accounts=self._special_accounts()
        )
        return tuple(
            MentionPull(
                key,
                "未命名私聊" if key.kind == "private" and name == key.recipient_id else name,
                count,
                special_count,
            )
            for key, name, count, special_count in candidates
            if key != exclude and self.allows(key)
        )[:limit]

    def _leave(self) -> None:
        """不再接新消息，保留本轮的投递资格。"""
        self._staying = False
        self._residence_deadline = None

    def permits(self, residence: InboxResidence) -> bool:
        """仅当前未被抢占且仍获授权的驻留可处理或投递。"""
        return self.residence is residence and self.allows(residence.conversation)

    def interrupt(self) -> None:
        """撤销旧驻留和此前所有未投递内容的资格。"""
        self.residence = None
        self._leave()

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
        """已验证的用户、特别关心或被点名的来信能唤醒心跳（D94、D137）。"""
        if perception.trust is TrustLevel.UNVERIFIED:
            return False
        if perception.mentioned or self.preempts(perception) or self.is_special(perception):
            return True
        if perception.route is None or not perception.quoted_message_id:
            return False
        key = ConversationKey.from_perception(perception)
        if not self.allows(key):
            return False
        original = self.store.find_platform_message(key, perception.quoted_message_id)
        return (
            original is not None
            and original.direction == "outgoing"
            and original.perception.sender_id == key.account_id
            and original.perception.visibility is perception.visibility
        )

    def preempts(self, perception: Perception) -> bool:
        """只有通过通道验证、仍绑定为用户的账号可以抢占（D149）。"""
        if perception.trust is TrustLevel.UNVERIFIED:
            return False
        if self._people is None:
            return (perception.channel, perception.sender_id) in self._user_accounts
        person = self._people.find(perception.channel, perception.sender_id)
        return person is not None and person.is_user

    def is_special(self, perception: Perception) -> bool:
        """特别关心只能唤醒和优先排队，不能取得用户权限（D149）。"""
        if perception.trust is TrustLevel.UNVERIFIED or self._people is None or not self._special:
            return False
        person = self._people.find(perception.channel, perception.sender_id)
        return person is not None and person.id in self._special

    def yields_to_when_idle(self, perception: Perception) -> bool:
        """摘要开启时，特别关心和点名同级；纯引用保留 D147 的空闲切换。"""
        return (
            perception.route is not None
            and self.allows(ConversationKey.from_perception(perception))
            and (
                not self._mention_pull_enabled
                or not (perception.mentioned or self.is_special(perception))
            )
            and self.is_priority(perception)
        )

    def _special_accounts(self) -> Iterable[tuple[str, str]]:
        if self._people is not None:
            for person_id in self._special:
                yield from self._people.accounts_of(person_id)

    def priority_messages(self) -> list[InboxMessage]:
        """先收绑定用户，再按到达次序收其他优先来信（D149）。"""
        bound: dict[str, set[str]] = defaultdict(set)
        special: dict[str, set[str]] = defaultdict(set)
        users = self._people.user_accounts() if self._people is not None else self._user_accounts
        for platform, account_id in users:
            bound[platform].add(account_id)
        for platform, account_id in self._special_accounts():
            special[platform].add(account_id)
        identity: dict[int, InboxMessage] = {}
        mentions: dict[int, InboxMessage] = {}
        for item in self.store.list_unread_conversations():
            if item.key == self.active_conversation or not self.allows(item.key):
                continue
            owners = bound[item.key.platform]
            for row in self._attention_page(item.key, owners, special[item.key.platform]):
                if row.perception.sender_id in owners:
                    identity[row.id] = row
                else:
                    mentions[row.id] = row
        ordered = [
            *sorted(identity.values(), key=lambda row: row.id),
            *sorted(mentions.values(), key=lambda row: row.id),
        ]
        return ordered[:_PRIORITY_LIMIT]

    def _attention_page(
        self, key: ConversationKey, owners: set[str], special: set[str]
    ) -> list[InboxMessage]:
        """每页只查一次时间线，过滤掉已经处理过的未读；身份匹配的行排在最前。"""
        messages: list[InboxMessage] = []
        after: tuple[int, int] | None = None
        while len(messages) < _PRIORITY_LIMIT:
            remaining = _PRIORITY_LIMIT - len(messages)
            rows = self.store.list_attention_unread(
                key, owners, special_senders=special, after=after, limit=remaining
            )
            if not rows:
                break
            known = self._timeline.inbox_message_ids(row.id for row in rows)
            messages.extend(row for row in rows if row.id not in known)
            if len(rows) < remaining:
                break
            last = rows[-1]
            after = (0 if last.perception.sender_id in owners else 1, last.id)
        return messages

    def browse(self, now: datetime | None = None, *, can_browse: bool) -> list[InboxConversation]:
        """安全闲暇时取得可选的未读会话，私聊和群聊分别列出。"""
        if not self._enabled or not can_browse or self.active_conversation is not None:
            self._idle_since = None
            return []
        if now is None:
            now = self._clock.now()
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

    def read_selection(self, selection: InboxSelection, *, now: datetime) -> list[InboxMessage]:
        """复验选择与路由权限，读取最新一批并清空该会话未读后才占住会话（D145）。

        入场条数仍不超过 inbox_read_limit；实际读最新 min(count, 上限) 条，
        更早的未读一并标已读丢弃，不再逐条追赶（D145 第五条）。
        """
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
        messages = self.store.read_latest_and_clear(
            key, limit=min(selection.count, self.read_limit)
        )
        if messages:
            self.residence = InboxResidence(key, selection.count)
            self._staying = True
            self.renew(now)
        return messages

    def active_messages(self, *, now: datetime) -> list[InboxMessage]:
        """读取当前停留会话最新的至多 inbox_read_limit 条未读，并清空该会话未读（D145）。

        入场时规划器选的条数只管「进入哪个会话」，后续补读固定按 inbox_read_limit
        取最新一批；更早没读到的直接标已读丢弃，不堆积（D145 取代 D116 第三、四条）。
        """
        residence = self.residence
        if not self._staying or residence is None or not self.permits(residence):
            return []
        messages = self.store.read_latest_and_clear(residence.conversation, limit=self.read_limit)
        if messages:
            self.renew(now)
        return messages

    def debounce_messages(self, key: ConversationKey) -> list[InboxMessage]:
        """尚未驻留的优先来信读最新一批并清空该会话未读（D145）。"""
        return (
            self.store.read_latest_and_clear(key, limit=self.read_limit) if self.allows(key) else []
        )

    def apply_plan(
        self, plan: Plan, target: Perception, source: TimelineRecord, *, now: datetime
    ) -> None:
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
                self.renew(now)
            elif key == self.active_conversation:
                self._leave()
        if (
            target.route is None
            or target.route.kind != "private"
            or target.route.temporary_group_id is not None
            or self._people is None
            or plan.special_contact is None
        ):
            # 只有普通私聊能改特别关心，避免群成员借提示注入把自己升级（D137）。
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
