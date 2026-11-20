"""每日日记的把关、材料和收结果；器官只提正文建议（D80）。"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum

from aureus.agenda import FindingStore
from aureus.body import EnergyTier
from aureus.common import BudgetCalendar, DiaryConfig
from aureus.heartbeat import events
from aureus.heartbeat.context import build_diary_prompt, build_system_context
from aureus.heartbeat.recall import memory_entry
from aureus.identity import Identity
from aureus.memory import DiaryBook, MemoryEntry
from aureus.organs import CallPurpose, OrganRequest, OrganResponse
from aureus.people import PeopleDirectory
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, VisibilityScope

logger = logging.getLogger(__name__)
# D80 第 6 条：有上限地读当天最新记录，每条正文最多 200 字。
_MATERIAL_RECORD_LIMIT = 1000
_MATERIAL_ENTRY_CHARS = 200
_MATERIAL_EVENTS = {
    events.EVENT_MESSAGE,
    events.EVENT_INITIATIVE_RESULT,
    events.EVENT_EXPLORATION_RESULT,
    events.EVENT_AGENDA_ADDED,
    events.EVENT_AGENDA_CLOSED,
    events.EVENT_INTEREST_CHANGED,
    events.EVENT_COGNITIVE_STATE,
}


class DiaryFailure(StrEnum):
    """当天不补写的具体失败原因（D80 第 7 条）。"""

    ORGAN_FAILED = "organ_failed"
    EMPTY = "empty"
    TOO_LONG = "too_long"
    FILE_EXISTS = "file_exists"
    WRITE_FAILED = "write_failed"


@dataclass(frozen=True)
class DiaryOutcome:
    """后台建议；保存文件与留痕交回心跳。"""

    day: date
    text: str | None = None
    failure: DiaryFailure | None = None


@dataclass(frozen=True)
class DiaryJob:
    """拍里准备的请求和输出上限，后台不再读身份或时间线。"""

    day: date
    request: OrganRequest
    max_chars: int


def should_write(
    config: DiaryConfig,
    *,
    now: datetime,
    calendar: BudgetCalendar,
    attempted_today: bool,
    has_organ: bool,
    busy: bool,
    halted: bool,
    tier: EnergyTier | None,
) -> bool:
    """从预算日起点往后找第一个写作时刻；夜间照写，透支保护不写（D55、D80）。"""
    if (
        not config.enabled
        or not has_organ
        or attempted_today
        or busy
        or halted
        or tier is EnergyTier.OVERDRAFT
    ):
        return False
    start = calendar.day_bounds(now)[0].astimezone(calendar.timezone).replace(tzinfo=None)
    due = start.replace(hour=config.write_after_hour, minute=0, second=0, microsecond=0)
    if due < start:
        due += timedelta(days=1)
    return now >= due.replace(tzinfo=calendar.timezone).astimezone(UTC)


def read_material(
    timeline: TimelineDatabase,
    calendar: BudgetCalendar,
    now: datetime,
    max_chars: int,
    *,
    people: PeopleDirectory | None,
    findings: FindingStore | None,
) -> str:
    """读当日经历；称呼和网页资料复用索引取法，超长只删最旧整行。"""
    records = timeline.query(TimelineQuery(latest=True, limit=_MATERIAL_RECORD_LIMIT))
    day = calendar.day_of(now)
    lines: list[str] = []
    for record in records:
        if (
            record.event_type not in _MATERIAL_EVENTS
            or record.timestamp > now
            or calendar.day_of(record.timestamp) != day
        ):
            continue
        if record.event_type in (events.EVENT_COGNITIVE_STATE, events.EVENT_AGENDA_CLOSED):
            entry = MemoryEntry("我", record.content)
        else:
            entry = memory_entry(record, people=people, findings=findings)
        if entry is not None:
            stamp = record.timestamp.astimezone(calendar.timezone).strftime("%H:%M")
            text = " ".join(entry.text.split())[:_MATERIAL_ENTRY_CHARS]
            lines.append(f"{stamp} {entry.actor_label}：{text}")
    omitted = 0
    while True:
        notice = [f"（前面省略了 {omitted} 条）"] if omitted else []
        material = "\n".join([*notice, *lines[omitted:]])
        if len(material) <= max_chars:
            return material
        omitted += 1


class DiaryTracker:
    """今天只试一次；读不清就算试过，写失败不补记（D78、D80）。"""

    def __init__(
        self,
        timeline: TimelineDatabase,
        calendar: BudgetCalendar,
        book: DiaryBook,
        config: DiaryConfig,
        people: PeopleDirectory | None,
        findings: FindingStore | None,
        has_organ: bool,
    ) -> None:
        self.timeline, self.calendar, self.book = timeline, calendar, book
        self.config, self.people, self.findings = config, people, findings
        self.has_organ = has_organ
        self.attempted_day: date | None = None
        if config.enabled and not has_organ:
            logger.warning("没有总结器官，不写日记")

    def restore(self, now: datetime) -> None:
        """醒来时只取最近成功或失败记录；读取失败则当天不写。"""
        if not self.config.enabled:
            return
        day = self.calendar.day_of(now)
        try:
            records = [
                record
                for kind in (events.EVENT_DIARY_WRITTEN, events.EVENT_DIARY_FAILED)
                for record in self.timeline.query(
                    TimelineQuery(event_type=kind, latest=True, limit=1)
                )
            ]
            latest = max(records, key=lambda record: record.id, default=None)
            if latest is not None and latest.metadata.get("day") == day.isoformat():
                self.attempted_day = day
        except Exception:
            self.attempted_day = day
            logger.exception("日记历史读不了，今天不写日记")

    def prepare(
        self,
        now: datetime,
        identity: Identity,
        *,
        tier: EnergyTier | None,
        halted: bool,
        busy: bool,
    ) -> DiaryJob | None:
        """拍里把关并先占当天次数，再读取材料和身份快照。"""
        day = self.calendar.day_of(now)
        if not should_write(
            self.config,
            now=now,
            calendar=self.calendar,
            attempted_today=self.attempted_day == day,
            has_organ=self.has_organ,
            busy=busy,
            halted=halted,
            tier=tier,
        ):
            return None
        self.attempted_day = day
        try:
            material = read_material(
                self.timeline,
                self.calendar,
                now,
                self.config.max_input_chars,
                people=self.people,
                findings=self.findings,
            )
            prompt = build_diary_prompt(material, self.config.max_chars)
            system = build_system_context(identity, now, None, self.calendar.timezone)
            request = OrganRequest(
                self.config.organ,
                prompt,
                system_context=system,
                purpose=CallPurpose.OPTIONAL,
                context={"task": "diary"},
            )
            return DiaryJob(day, request, self.config.max_chars)
        except Exception:
            logger.exception("日记材料读不了，今天不写日记")
            return None

    def finish(self, outcome: DiaryOutcome) -> None:
        """主线程先写文件再留痕，任何失败都不补写。"""
        failure = outcome.failure
        name = ""
        if outcome.text is not None and failure is None:
            try:
                name = self.book.write(outcome.day, outcome.text)
            except FileExistsError:
                failure = DiaryFailure.FILE_EXISTS
                logger.warning("当天日记文件已存在，不覆盖")
            except OSError:
                failure = DiaryFailure.WRITE_FAILED
                logger.exception("日记文件写失败，今天不补写")
        metadata = {"day": outcome.day.isoformat()}
        if failure is not None:
            metadata["failure"] = failure.value
        else:
            metadata.update(file_name=name, organ=self.config.organ)
        try:
            self.timeline.append(
                events.EVENT_DIARY_FAILED if failure is not None else events.EVENT_DIARY_WRITTEN,
                f"日记失败：{failure.value}" if failure is not None else outcome.text or "",
                ActorType.ME,
                visibility=VisibilityScope.PRIVATE,
                metadata=metadata,
            )
        except Exception:
            logger.exception("日记时间线写失败，不补记")


async def write_diary(
    job: DiaryJob,
    call: Callable[[OrganRequest], Awaitable[OrganResponse]],
) -> DiaryOutcome:
    """后台只调用总结器官并校验正文，不写文件或状态。"""
    try:
        response = await call(job.request)
    except Exception:
        logger.exception("日记总结器官调用失败")
        return DiaryOutcome(job.day, failure=DiaryFailure.ORGAN_FAILED)
    text = response.suggestion.strip()
    if not text:
        return DiaryOutcome(job.day, failure=DiaryFailure.EMPTY)
    if len(text) > job.max_chars:
        return DiaryOutcome(job.day, failure=DiaryFailure.TOO_LONG)
    return DiaryOutcome(job.day, text)
