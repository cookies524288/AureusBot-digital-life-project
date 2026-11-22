"""3.3 人际发布和按账号查询，数据均为合成记录。"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from aureus.common import ManualClock
from aureus.people import HistoricalKind, HistoricalRecord, PeopleDirectory, PeopleError
from tests.heartbeat.test_people_flow import NOW


@pytest.fixture
def directory(tmp_path: Path) -> Iterator[PeopleDirectory]:
    people = PeopleDirectory(tmp_path / "aureus.db", ManualClock(NOW))
    people.open()
    try:
        yield people
    finally:
        people.close()


def test_publish_profiles_and_directed_evidence(directory: PeopleDirectory) -> None:
    user = directory.ensure_user("本机用户", [("qq", "100"), ("web", "local")])
    records = [
        HistoricalRecord(
            instance="example",
            kind=HistoricalKind.RELATION,
            source_key="4",
            subject_key="alice",
            other_key="bob",
            relation_type="共同创作",
            weight=-1.75,
            evidence={"message_ids": [45, 67], "excerpt": "一起写的"},
            occurred_at=123456.75,
        ),
        HistoricalRecord(
            instance="example",
            kind=HistoricalKind.STATE,
            source_key="1",
            subject_key="alice",
            impression="喜欢观察星空",
            memory_points=["见过月食"],
            relationship_state={"affinity": -1.75},
        ),
        HistoricalRecord(
            instance="example",
            kind=HistoricalKind.PROFILE,
            source_key="1",
            subject_key="alice",
            account=("qq", "100"),
            name="小青",
            known_since=120000,
        ),
        HistoricalRecord(
            instance="example",
            kind=HistoricalKind.PROFILE,
            source_key="2",
            subject_key="bob",
            account=("qq", "200"),
            name="小青",
            original={"is_user": True},
        ),
        HistoricalRecord(
            instance="example",
            kind=HistoricalKind.EVENT,
            source_key="5",
            subject_key="alice",
            summary="提出观星计划",
        ),
        HistoricalRecord(
            instance="example",
            kind=HistoricalKind.THREAD_ITEM,
            source_key="6",
            other_key="bob",
            summary="讨论由对方接续",
        ),
    ]

    assert [row.status for row in directory.publish_history(records)] == ["written"] * len(records)
    assert [row.status for row in directory.publish_history(records)] == ["existing"] * len(records)
    directory.close()
    directory.open()
    assert directory.find("qq", "100") == directory.find("web", "local") == user
    bob = directory.find("qq", "200")
    assert bob is not None and not bob.is_user and bob.id != user.id
    alice_entries = {entry.record.kind: entry for entry in directory.history_for(user.id)}
    bob_entries = {entry.record.kind: entry for entry in directory.history_for(bob.id)}
    assert alice_entries[HistoricalKind.PROFILE].record == records[2]
    assert bob_entries[HistoricalKind.PROFILE].record == records[3]
    assert alice_entries[HistoricalKind.STATE].record == records[1]
    edge = alice_entries[HistoricalKind.RELATION]
    assert bob_entries[HistoricalKind.RELATION] == edge
    assert (edge.person_id, edge.other_person_id, edge.record) == (user.id, bob.id, records[0])
    assert alice_entries[HistoricalKind.EVENT].record == records[4]
    assert alice_entries[HistoricalKind.EVENT].record.occurred_at is None
    assert bob_entries[HistoricalKind.THREAD_ITEM].person_id is None
    assert bob_entries[HistoricalKind.THREAD_ITEM].other_person_id == bob.id


def test_pending_records_keep_raw_data_and_resolve_only_by_source_key(
    directory: PeopleDirectory,
) -> None:
    known = HistoricalRecord(
        instance="example",
        kind=HistoricalKind.PROFILE,
        source_key="known",
        subject_key="alice",
        account=("qq", "100"),
        name="小青",
    )
    missing = HistoricalRecord(
        instance="example",
        kind=HistoricalKind.PROFILE,
        source_key="missing",
        subject_key="bob",
        name="小青",
        original={"user_id": None},
    )
    edge = HistoricalRecord(
        instance="example",
        kind=HistoricalKind.RELATION,
        source_key="edge",
        subject_key="alice",
        other_key="bob",
        original={"right": "bob"},
    )
    unrelated_instance = HistoricalRecord(
        instance="another",
        kind=HistoricalKind.PROFILE,
        source_key="missing",
        subject_key="bob",
        account=("qq", "200"),
        name="小青",
    )
    for _ in range(2):
        report = directory.publish_history([known, missing, edge, unrelated_instance])
        assert [(row.status, row.reason) for row in report[1:3]] == [
            ("pending", "missing_account"),
            ("pending", "unresolved_relation_endpoint"),
        ]
    alice = directory.find("qq", "100")
    assert alice is not None
    assert [row.record for row in directory.history_for(alice.id)] == [known]
    for expected in (missing, edge):
        stored = directory.historical_record(expected.instance, expected.kind, expected.source_key)
        assert stored is not None and stored.record == expected
        assert stored.person_id is None and stored.other_person_id is None

    explicit = HistoricalRecord(
        instance="example",
        kind=HistoricalKind.STATE,
        source_key="resolved",
        subject_key="bob",
        account=("qq", "300"),
    )
    assert [row.status for row in directory.publish_history([edge, missing, explicit])] == [
        "written",
        "written",
        "written",
    ]
    assert [row.status for row in directory.publish_history([edge, missing, explicit])] == [
        "existing",
        "existing",
        "existing",
    ]
    bob = directory.find("qq", "300")
    assert bob is not None
    assert {row.record.source_key for row in directory.history_for(bob.id)} == {
        "missing",
        "edge",
        "resolved",
    }


def test_publish_conflict_rolls_back_new_accounts_and_records(directory: PeopleDirectory) -> None:
    known = HistoricalRecord(
        instance="example",
        kind=HistoricalKind.PROFILE,
        source_key="1",
        subject_key="alice",
        account=("qq", "100"),
        name="小青",
    )
    directory.publish_history([known])
    new = HistoricalRecord(
        instance="example",
        kind=HistoricalKind.PROFILE,
        source_key="2",
        subject_key="bob",
        account=("qq", "200"),
    )
    changed = known.model_copy(update={"name": "更新"})
    with pytest.raises(PeopleError, match="内容已变更"):
        directory.publish_history([new, changed])
    other_account = known.model_copy(update={"source_key": "3", "account": ("web", "100")})
    with pytest.raises(PeopleError, match="多个不同"):
        directory.publish_history([new, other_account])
    assert directory.find("qq", "200") is None
    assert directory.find("web", "100") is None
    assert directory.historical_record("example", HistoricalKind.PROFILE, "2") is None
    report = directory.publish_history([new, known])
    assert [row.status for row in report] == ["written", "existing"]
