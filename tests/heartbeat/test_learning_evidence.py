"""语言资料回查必须核对认证身份、完整路由和稳定来源键。"""

import json
from pathlib import Path

import pytest

from aureus.common import ManualClock
from aureus.heartbeat.inbox_store import ConversationKey, InboxStore
from aureus.heartbeat.language_runner import language_sample, learning_evidence_available
from aureus.heartbeat.perception import MessageRoute, Perception, TrustLevel
from aureus.timeline import ActorType, TimelineDatabase, VisibilityScope
from tests.heartbeat.test_judging_flow import NOW


@pytest.mark.parametrize("channel", ["web", "terminal"])
@pytest.mark.parametrize("change", ["none", "actor", "trust", "visibility", "source", "scope"])
def test_local_evidence_requires_verified_identity(
    tmp_path: Path, channel: str, change: str
) -> None:
    timeline = TimelineDatabase(tmp_path / "synthetic.db", ManualClock(NOW))
    timeline.open()
    try:
        scope = json.dumps((channel, "local"), separators=(",", ":"))
        record = timeline.append(
            "message",
            "合成正文",
            ActorType.USER,
            "another" if change == "actor" else "local",
            VisibilityScope.PUBLIC if change == "visibility" else VisibilityScope.USER_ONLY,
            {"channel": channel, "trust": "unverified" if change == "trust" else "verified_user"},
        )
        key = language_sample(record, None, scope).source_key
        assert learning_evidence_available(
            timeline,
            None,
            "[" if change == "scope" else scope,
            "unknown" if change == "source" else key,
            record.id,
        ) is (change == "none")
    finally:
        timeline.close()


@pytest.mark.parametrize(
    "change", ["none", "legacy", "account", "sender", "visibility", "source", "missing"]
)
def test_platform_evidence_requires_same_visible_original(tmp_path: Path, change: str) -> None:
    clock = ManualClock(NOW)
    database = tmp_path / "synthetic.db"
    timeline = TimelineDatabase(database, clock)
    timeline.open()
    try:
        inbox = InboxStore(database, clock)
        perception = Perception(
            "qq",
            "sender",
            "合成正文",
            VisibilityScope.GROUP,
            TrustLevel.VERIFIED_CONTACT,
            route=MessageRoute("42", "group", "10", "m1"),
            source_message_id="original:m1",
        )
        original = inbox.add(perception)
        assert original is not None
        scope = ConversationKey.from_perception(perception).language_scope_key()
        record = timeline.append(
            "message",
            perception.content,
            ActorType.PERSON,
            "other" if change == "sender" else "sender",
            VisibilityScope.PUBLIC if change == "visibility" else VisibilityScope.GROUP,
            {
                "channel": "qq",
                "account_id": "99" if change == "account" else "42",
                "message_type": "group",
                "recipient_id": "10",
                "message_id": "m1",
                "trust": "verified_contact",
                "source_message_id": "original:m1",
                "inbox_message_id": 9999 if change == "missing" else original.id,
            },
        )
        sample = language_sample(record, perception.source_message_id, scope)
        key = sample.previous_source_key if change == "legacy" else sample.source_key
        assert key is not None
        assert learning_evidence_available(
            timeline, inbox, scope, "unknown" if change == "source" else key, record.id
        ) is (change in ("none", "legacy"))
        assert not learning_evidence_available(timeline, None, scope, key, record.id)
    finally:
        timeline.close()
