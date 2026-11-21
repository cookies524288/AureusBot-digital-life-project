"""回复和网页共用的最近对话查询（D84）。"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.common import ManualClock
from aureus.heartbeat import recent_dialogue
from aureus.timeline import ActorType, TimelineDatabase, VisibilityScope


@pytest.mark.parametrize(
    ("visibility", "expected"),
    [(VisibilityScope.USER_ONLY, [2, 4]), (None, [4, 5])],
)
def test_recent_dialogue_filters_visibility_and_takes_latest_messages(
    tmp_path: Path, visibility: VisibilityScope | None, expected: list[int]
) -> None:
    timeline = TimelineDatabase(
        tmp_path / "aureus.db", ManualClock(datetime(2026, 11, 21, tzinfo=UTC))
    )
    timeline.open()
    try:
        for scope in (
            VisibilityScope.USER_ONLY,
            VisibilityScope.USER_ONLY,
            VisibilityScope.GROUP,
            VisibilityScope.USER_ONLY,
            VisibilityScope.PRIVATE,
        ):
            timeline.append("message", "对话", ActorType.USER, visibility=scope)
        timeline.append("thought", "不是对话", ActorType.ME)
        records = recent_dialogue(timeline, visibility=visibility, limit=2)
        assert [record.id for record in records] == expected
        assert timeline.count_records() == 6
    finally:
        timeline.close()
