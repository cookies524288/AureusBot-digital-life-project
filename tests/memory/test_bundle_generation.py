"""仅用登记过的合成来源检验本库生成和安装互通。"""

import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest

from aureus.common import ManualClock
from aureus.memory import HistoricalMemory, MemoryEvidence, MemoryIndex, MemorySource
from aureus.memory.bundle_format import MemoryBundleConflictError, load_bundle, paragraph_hash
from aureus.memory.graph import MemoryGraphEvidence, MemoryGraphSource
from aureus.memory.lifecycle import LifecycleAction
from aureus.people import PeopleDirectory
from aureus.people.history import HistoricalKind, HistoricalRecord
from aureus.people.profile_maintenance import ProfileEdit, ProfileSource
from aureus.timeline import TimelineDatabase


def test_generate_selected_snapshot_closure_and_recheck(tmp_path: Path) -> None:
    now = datetime(2026, 11, 24, tzinfo=UTC)
    database = tmp_path / "test.db"
    clock = ManualClock(now)
    timeline = TimelineDatabase(database, clock)
    index = MemoryIndex(database, timeline, clock)
    people = PeopleDirectory(database, clock)
    timeline.open()
    index.open()
    people.open()

    def location(table: str, *key: str, instance: str = "selected") -> MemorySource:
        return MemorySource(instance, "memory", table, key)

    paragraph = location("paragraphs", "original")
    episode = location("episodes", "expedition")
    episode_link = location("episode_paragraphs", "expedition", "original")
    other_episode = location("episodes", "expired-link")
    other = location("paragraphs", "other", instance="unselected")
    entity = location("entities", "observatory")
    entity_link = location("paragraph_entities", "original", "observatory")
    fact = location("fact_claims", "claim")
    fact_link = location("fact_evidence", "claim", "paragraph", "original", "support")
    fields = dict(
        scope_type="person",
        scope_id="person",
        fact_key="地点",
        value_text="月球",
        value_normalized="月球",
        polarity="positive",
        cardinality="single",
        conflict_group="地点",
        stability="stable",
        profile_section="stable_facts",
        authority="observed",
    )
    try:
        index.publish([HistoricalMemory(other, "未选段落")], snapshot=("unselected", "sample-2"))
        index.publish([HistoricalMemory(location("paragraphs", "unregistered"), "未经登记")])
        index.publish(
            [
                HistoricalMemory(paragraph, "月球合成段落"),
                HistoricalMemory(location("paragraphs", "expired"), "失效原件", valid_to=now),
                HistoricalMemory(location("paragraphs", "private"), "跨域原件", status="retracted"),
                HistoricalMemory(
                    episode_link,
                    "",
                    status="missing",
                    evidence=(
                        MemoryEvidence("episodes", "expedition", episode),
                        MemoryEvidence("paragraph", "original", paragraph),
                    ),
                ),
                HistoricalMemory(
                    other_episode,
                    "关联失效，情景仍在",
                    evidence=(
                        MemoryEvidence("paragraph", "original", paragraph),
                        MemoryEvidence(
                            "bundle_episode_fields", '{"title":"应排除","summary":"失效"}'
                        ),
                    ),
                ),
                HistoricalMemory(
                    location("episode_paragraphs", "expired-link", "original"),
                    "",
                    status="missing",
                    valid_to=now,
                    evidence=(
                        MemoryEvidence("episodes", "expired-link", other_episode),
                        MemoryEvidence("paragraph", "original", paragraph),
                    ),
                ),
                HistoricalMemory(
                    MemorySource("selected", "main", "mai_messages", ("a",)), "平台原件"
                ),
                HistoricalMemory(
                    episode,
                    "合成情景",
                    evidence=(
                        MemoryEvidence("paragraph", "original", paragraph),
                        MemoryEvidence(
                            "bundle_episode_fields", '{"title":"情景","summary":"合成事件"}'
                        ),
                    ),
                ),
                HistoricalMemory(
                    location("episodes", "cross"),
                    "跨域情景",
                    evidence=(
                        MemoryEvidence("paragraph", "other", other),
                        MemoryEvidence(
                            "bundle_episode_fields", '{"title":"不应有","summary":"跨域"}'
                        ),
                    ),
                ),
                HistoricalMemory(entity, "天文台", status="missing"),
                HistoricalMemory(
                    entity_link,
                    "",
                    status="missing",
                    evidence=(
                        MemoryEvidence("paragraphs", "original", paragraph),
                        MemoryEvidence("entity", "observatory", entity),
                    ),
                ),
                HistoricalMemory(
                    fact,
                    "地点：月球",
                    recorded_at=now,
                    evidence=(
                        MemoryEvidence("paragraph", "original", paragraph),
                        MemoryEvidence("bundle_fact_fields", json.dumps(fields)),
                    ),
                ),
                HistoricalMemory(
                    fact_link,
                    "",
                    status="missing",
                    evidence=(
                        MemoryEvidence("fact_claims", "claim", fact),
                        MemoryEvidence("paragraph", "original", paragraph),
                    ),
                ),
            ],
            snapshot=("selected", "sample-1"),
            bundle_origin="verified-source-batch",
            graph_sources=(
                MemoryGraphSource(
                    entity,
                    "entity",
                    "active",
                    name="天文台",
                    evidence=(MemoryGraphEvidence("original", paragraph, entity_link),),
                ),
            ),
        )
        people.publish_history(
            [
                HistoricalRecord(
                    instance="selected",
                    kind=HistoricalKind.PROFILE,
                    source_key="person",
                    subject_key="person",
                    account=("qq", "1234567"),
                    name="合成人物",
                ),
                HistoricalRecord(
                    instance="selected",
                    kind=HistoricalKind.PROFILE_SNAPSHOT,
                    source_key="2",
                    subject_key="person",
                    profile_version=2,
                    bundle_origin="verified-source-batch",
                    summary="当前画像",
                    occurred_at=now.timestamp(),
                    expires_at=(now + timedelta(hours=1)).timestamp(),
                    original={
                        "evidence_ids_json": '["original"]',
                        "fact_claim_ids_json": '["claim"]',
                    },
                ),
                HistoricalRecord(
                    instance="selected",
                    kind=HistoricalKind.PROFILE,
                    source_key="other-person",
                    subject_key="other-person",
                    account=("qq", "1234568"),
                    name="同源名不同批",
                ),
                HistoricalRecord(
                    instance="selected",
                    kind=HistoricalKind.PROFILE_SNAPSHOT,
                    source_key="3",
                    subject_key="other-person",
                    profile_version=1,
                    bundle_origin="other-batch",
                    summary="不能从同名来源导出",
                    occurred_at=now.timestamp(),
                    expires_at=(now + timedelta(hours=1)).timestamp(),
                    original={"evidence_ids_json": '["original"]'},
                ),
            ]
        )
        [selected, unselected] = index.list_bundle_sources()
        assert (selected.instance, unselected.instance) == ("selected", "unselected")
        with pytest.raises(MemoryBundleConflictError):
            index.generate_bundle(["not-from-publisher"], "full", people)
        preview = index.generate_bundle([selected.source_id], "full", people)
        assert preview["counts"]["paragraphs"] == 1
        assert preview["counts"]["episodes"] == 1
        assert preview["counts"]["entities"] == 1
        assert preview["counts"]["fact_evidence"] == 1
        assert preview["counts"]["person_profile_snapshots"] == 1
        output = BytesIO()
        index.generate_bundle(
            [selected.source_id],
            "full",
            people,
            output=output,
            expected_digest=preview["preview_digest"],
        )
        bundle = load_bundle(output)
        assert bundle.state is not None
        assert bundle.installation_id == preview["installation_id"]
        [doc] = bundle.knowledge["docs"]
        assert doc["id"] == paragraph_hash("月球合成段落")
        assert doc["extracted_entities"] == ["天文台"]
        [snapshot] = bundle.state["tables"]["person_profile_snapshots"]
        assert json.loads(snapshot["evidence_ids_json"]) == [doc["id"]]
        assert index.preview_bundle(output, people)["counts"]["fact_claims"] == 1
        assert index.install_bundle(output, "merge", people)["status"] == "installed"

        def maintain(action: LifecycleAction, text: str | None = None) -> str:
            plan = index.preview_lifecycle(fact, action, text, None, lambda: True)
            assert plan is not None
            identifier = str(plan["plan_id"])
            assert index.apply_lifecycle(identifier, None, lambda: True, undo=False)
            return identifier

        correction = maintain("correct", "地点：桂花港")
        changed = index.generate_bundle([selected.source_id], "full", people)
        assert changed["counts"]["fact_claims"] == 0
        assert changed["counts"]["person_profile_snapshots"] == 0
        with pytest.raises(MemoryBundleConflictError):
            index.generate_bundle(
                [selected.source_id],
                "full",
                people,
                output=BytesIO(),
                expected_digest=preview["preview_digest"],
            )
        assert index.apply_lifecycle(correction, None, lambda: True, undo=True)
        assert (
            index.generate_bundle([selected.source_id], "full", people)["counts"]["fact_claims"]
            == 1
        )
        maintain("remove")
        assert (
            index.generate_bundle([selected.source_id], "full", people)["counts"]["fact_claims"]
            == 0
        )
        maintain("restore")
        assert (
            index.generate_bundle([selected.source_id], "full", people)["counts"]["fact_claims"]
            == 1
        )

        profile = ProfileSource("selected", HistoricalKind.PROFILE_SNAPSHOT, "2")
        plan = people.preview_profile(profile, ProfileEdit(summary="本机修订"), lambda: True)
        assert plan is not None
        identifier = str(plan["plan_id"])
        assert people.change_profile(identifier, lambda: True, undo=False)
        assert (
            index.generate_bundle([selected.source_id], "full", people)["counts"][
                "person_profile_snapshots"
            ]
            == 0
        )
        assert people.change_profile(identifier, lambda: True, undo=True)
        assert (
            index.generate_bundle([selected.source_id], "full", people)["counts"][
                "person_profile_snapshots"
            ]
            == 1
        )

        minimal = index.generate_bundle([selected.source_id], "knowledge", people)
        assert minimal["components"] == ["knowledge.json"]
        index.publish([HistoricalMemory(paragraph, "登记后改变")])
        changed = index.generate_bundle([selected.source_id], "full", people)
        assert changed["excluded"]["登记后原件已变化"] == 1
        with pytest.raises(MemoryBundleConflictError):
            index.generate_bundle(
                [selected.source_id],
                "full",
                people,
                output=BytesIO(),
                expected_digest=preview["preview_digest"],
            )
    finally:
        index.close()
        timeline.close()
