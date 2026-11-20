"""探索状态、兴趣建议和只读探索流程；写入只在心跳中完成（D79）。"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, tzinfo

from aureus.actions.search import SearchProvider
from aureus.actions.web import FetchFailure, WebPage, WebReader
from aureus.agenda.findings import Finding, FindingStore
from aureus.agenda.interests import InterestStore
from aureus.body import EnergyTier
from aureus.common import BudgetCalendar, ExplorationConfig
from aureus.heartbeat import events
from aureus.heartbeat import exploration as rules
from aureus.heartbeat.context import build_exploration_prompt, describe_interests
from aureus.heartbeat.perception import Perception, TrustLevel
from aureus.heartbeat.planning import InterestAction, InterestChange
from aureus.organs import CallPurpose, OrganRequest, OrganResponse
from aureus.people import PeopleDirectory
from aureus.safety import PermissionChecker
from aureus.safety.permissions import ACTION_READ_WEB, ACTION_WEB_SEARCH
from aureus.timeline import (
    ActorType,
    TimelineDatabase,
    TimelineQuery,
    TimelineRecord,
    VisibilityScope,
)

logger = logging.getLogger(__name__)
_RESTORE_LIMIT = 50
_MAX_FETCHES = 2
_CONTEXT_FINDINGS = 2


@dataclass(frozen=True)
class ExplorationOutcome:
    """后台返回的资料建议；没有任何已写入状态。"""

    outcome: rules.ExplorationResult
    url: str = ""
    title: str = ""
    finding: str = ""
    failure: str = ""


class ExplorationTracker:
    """从开始记录恢复次数；写失败就停止本次运行的探索，不补记。"""

    def __init__(
        self,
        timeline: TimelineDatabase,
        calendar: BudgetCalendar,
        interests: InterestStore | None = None,
        findings: FindingStore | None = None,
        config: ExplorationConfig | None = None,
        search: SearchProvider | None = None,
        reader: WebReader | None = None,
        has_organ: bool = True,
    ) -> None:
        self.timeline, self.calendar = timeline, calendar
        self.interests, self.findings = interests, findings
        self.config = config or ExplorationConfig()
        if interests is None or not interests.config.enabled:
            self.config = self.config.model_copy(update={"enabled": False})
        self.search, self.reader = search, reader or WebReader(self.config)
        self.has_organ = has_organ
        self.state = rules.ExplorationState()
        self.last_explored: dict[int, datetime] = {}
        self.failed = self._restored = False
        self._day: date | None = None
        self._missing_logged: set[str] = set()

    def finding_to_share(self, now: datetime) -> Finding | None:
        """仅给已通过把关的主动联系附加资料；读取失败仍可按原原因联系。"""
        if self.interests is None or not self.interests.config.enabled or self.findings is None:
            return None
        try:
            return self.findings.oldest_unshared(
                now - timedelta(days=self.config.share_within_days)
            )
        except Exception:
            logger.exception("分享收获读不了，这次不附加")
            return None

    def mark_shared(self, finding_id: int, message_id: int, now: datetime) -> None:
        """主动消息已落盘才标记；失败只记日志，以后可能再提一次。"""
        try:
            if self.findings is not None:
                self.findings.mark_shared(finding_id, message_id, now)
        except Exception:
            logger.exception("收获分享标记失败，不补记")

    def restore(self, now: datetime) -> None:
        """只读最近开始的事实；读取失败允许下一拍再读。"""
        try:
            records = self.timeline.query(
                TimelineQuery(
                    event_type=events.EVENT_EXPLORATION_STARTED,
                    actor_type=ActorType.ME,
                    latest=True,
                    limit=_RESTORE_LIMIT,
                )
            )
            day = self.calendar.day_of(now)
            count = sum(self.calendar.day_of(r.timestamp) == day for r in records)
            if len(records) == _RESTORE_LIMIT and self.calendar.day_of(records[0].timestamp) == day:
                count = max(count, self.config.max_per_day)
            self.state = rules.ExplorationState(count, records[-1].timestamp if records else None)
            self.last_explored = {r.metadata["interest_id"]: r.timestamp for r in records}
            self._day, self._restored, self.failed = day, True, False
        except Exception:
            self.failed = True
            logger.exception("探索历史读不了，这一拍不探索")

    def begin_beat(self, now: datetime) -> None:
        """跨预算日清次数；读取未成功时再试，写入失败保持停止。"""
        if not self.config.enabled:
            return
        if not self._restored:
            self.restore(now)
        if self._day != self.calendar.day_of(now):
            self.state = replace(self.state, explorations_today=0)
            self._day = self.calendar.day_of(now)

    def start(self, interest_id: int, now: datetime) -> TimelineRecord | None:
        """开始记录写成功后才占次数，之后失败也算一次。"""
        item = self.interests.get(interest_id) if self.interests is not None else None
        if item is None:
            return None
        try:
            record = self.timeline.append(
                events.EVENT_EXPLORATION_STARTED,
                f"探索：{item.text}",
                ActorType.ME,
                visibility=VisibilityScope.PRIVATE,
                metadata={"interest_id": interest_id, "query": item.text},
            )
        except Exception:
            self.failed = True
            logger.exception("探索开始记录写失败，本次运行停止探索")
            return None
        self.state = rules.ExplorationState(self.state.explorations_today + 1, now)
        self.last_explored[interest_id] = now
        return record

    def finish(self, started: TimelineRecord, outcome: ExplorationOutcome) -> None:
        """心跳保存一句收获和结果，任一写入失败都不补记。"""
        finding_id = None
        if outcome.outcome is rules.ExplorationResult.FOUND:
            try:
                assert self.findings is not None
                finding_id = self.findings.add(
                    started.metadata["interest_id"], outcome.finding, outcome.title, outcome.url
                ).id
            except Exception:
                self.failed = True
                outcome = replace(outcome, outcome=rules.ExplorationResult.RECORD_FAILED)
                logger.exception("收获写失败，本次运行停止探索")
        try:
            self.timeline.append(
                events.EVENT_EXPLORATION_RESULT,
                f"探索结果：{outcome.outcome.value}",
                ActorType.ME,
                visibility=VisibilityScope.PRIVATE,
                metadata={
                    "started_id": started.id,
                    "interest_id": started.metadata["interest_id"],
                    "outcome": outcome.outcome.value,
                    "url": outcome.url,
                    "failure": outcome.failure,
                    "finding_id": finding_id,
                },
            )
        except Exception:
            self.failed = True
            logger.exception("探索结果写失败，本次运行停止探索")


def prepare_exploration(
    tracker: ExplorationTracker,
    now: datetime,
    *,
    tier: EnergyTier | None,
    night: bool,
    halted: bool,
    busy: bool,
    cautious: bool,
) -> TimelineRecord | None:
    """在拍里读取在用兴趣并把关；缺服务只记一次日志。"""
    if not tracker.config.enabled or tracker.failed or not tracker._restored:
        return None
    try:
        items = tracker.interests.active() if tracker.interests is not None else []
        decision = rules.decide(
            now=now,
            config=tracker.config,
            state=tracker.state,
            interests=[rules.InterestStatus(i.id, tracker.last_explored.get(i.id)) for i in items],
            effective_tier=tier,
            night=night,
            halted=halted,
            busy=busy,
            cautious=cautious,
            has_search=tracker.search is not None,
            has_organ=tracker.has_organ,
        )
        if (
            decision.blocked_by in ("no_search", "no_organ")
            and decision.blocked_by not in tracker._missing_logged
        ):
            tracker._missing_logged.add(decision.blocked_by)
            logger.warning(
                "%s，不探索",
                "没配搜索服务" if decision.blocked_by == "no_search" else "没配总结器官",
            )
        return (
            tracker.start(decision.interest_id, now) if decision.interest_id is not None else None
        )
    except Exception:
        logger.exception("兴趣读不了，这一拍不探索")
        return None


def interest_context(
    tracker: ExplorationTracker,
    target: Perception,
    timezone: tzinfo | None,
    people: PeopleDirectory | None,
) -> str:
    """兴趣关着、群聊或非用户对话时完全不注入兴趣信息。"""
    if (
        tracker.interests is None
        or not tracker.interests.config.enabled
        or target.trust is not TrustLevel.VERIFIED_USER
        or target.visibility is not VisibilityScope.USER_ONLY
        or people is None
    ):
        return ""
    try:
        person = people.find(target.channel, target.sender_id)
        if person is None or not person.is_user:
            return ""
        return describe_interests(
            tracker.interests.active(),
            tracker.findings.recent(_CONTEXT_FINDINGS) if tracker.findings is not None else [],
            timezone,
        )
    except Exception:
        logger.exception("兴趣上下文读不了")
        return ""


def apply_interest(
    tracker: ExplorationTracker,
    change: InterestChange | None,
    source: TimelineRecord,
    now: datetime,
) -> None:
    """只从用户档案的私聊应用建议；表或留痕失败只记日志，不补记。"""
    store = tracker.interests
    if (
        store is None
        or not store.config.enabled
        or change is None
        or source.actor_type is not ActorType.USER
        or source.visibility is not VisibilityScope.USER_ONLY
        or source.metadata.get("person_id") is None
        or store.config.max_changes_per_day == 0
    ):
        return
    try:
        records = tracker.timeline.query(
            TimelineQuery(
                event_type=events.EVENT_INTEREST_CHANGED,
                actor_type=ActorType.ME,
                latest=True,
                limit=store.config.max_changes_per_day,
            )
        )
        changes = sum(
            tracker.calendar.day_of(r.timestamp) == tracker.calendar.day_of(now) for r in records
        )
        if changes >= store.config.max_changes_per_day:
            return
        if change.action is InterestAction.ADD:
            item = store.add(change.text or "", "self", source.id)
        elif change.action is InterestAction.REMOVE:
            item = store.remove(change.interest_id or 0, "self_removed")
        else:
            item = store.replace(change.interest_id or 0, change.text or "", source.id)
        if item is not None:
            tracker.timeline.append(
                events.EVENT_INTEREST_CHANGED,
                f"兴趣改动：{item.text}",
                ActorType.ME,
                visibility=VisibilityScope.PRIVATE,
                metadata={
                    "action": change.action.value,
                    "interest_id": change.interest_id or item.id,
                    "new_interest_id": item.id
                    if change.action is not InterestAction.REMOVE
                    else None,
                    "text": item.text,
                    "reason": change.reason,
                    "source_record_id": source.id,
                },
            )
    except Exception:
        logger.exception("兴趣改动或留痕失败，不补记")


async def explore(
    config: ExplorationConfig,
    search: SearchProvider,
    reader: WebReader,
    call: Callable[[OrganRequest], Awaitable[OrganResponse]],
    checker: PermissionChecker,
    query: str,
) -> ExplorationOutcome:
    """后台只搜索、读网页、问器官，返回资料建议，不碰时间线和存储。"""
    if not checker.check(ACTION_WEB_SEARCH).allowed:
        return ExplorationOutcome(rules.ExplorationResult.BLOCKED)
    try:
        results = await search.search(query, config.max_results)
    except Exception:
        logger.exception("探索搜索失败")
        return ExplorationOutcome(rules.ExplorationResult.SEARCH_FAILED)
    if not results:
        return ExplorationOutcome(rules.ExplorationResult.NO_RESULTS)
    failure = FetchFailure.NETWORK
    for result in results[:_MAX_FETCHES]:
        if not checker.check(ACTION_READ_WEB).allowed:
            return ExplorationOutcome(rules.ExplorationResult.BLOCKED)
        page = await reader.read(result.url)
        if isinstance(page, WebPage):
            break
        failure = page
    else:
        return ExplorationOutcome(
            rules.ExplorationResult.FETCH_FAILED,
            url=results[:_MAX_FETCHES][-1].url,
            failure=failure.value,
        )
    prompt, system = build_exploration_prompt(query, page)
    try:
        response = await call(
            OrganRequest(
                config.organ,
                prompt,
                system_context=system,
                purpose=CallPurpose.OPTIONAL,
                context={"task": "exploration_summary"},
            )
        )
        summary = rules.parse_summary(response.suggestion)
    except Exception:
        logger.exception("探索总结失败")
        summary = None
    if summary is None:
        return ExplorationOutcome(rules.ExplorationResult.SUMMARY_FAILED, url=page.url)
    return ExplorationOutcome(
        rules.ExplorationResult.FOUND if summary.interesting else rules.ExplorationResult.BORING,
        url=page.url,
        title=page.title,
        finding=summary.finding,
    )
