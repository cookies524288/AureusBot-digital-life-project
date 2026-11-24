"""旧三段来源键不重复消费，也不替同名会话猜归属。"""

from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from aureus.common import ManualClock
from aureus.heartbeat.inbox_store import ConversationKey
from aureus.heartbeat.language_runner import language_sample, previous_language_scope
from aureus.memory import LanguageKind, LanguageProposal, LanguageSample, LanguageStore
from aureus.timeline import ActorType, TimelineDatabase, VisibilityScope


def test_preserve_consumed_and_pending_legacy_sources(tmp_path: Path) -> None:
    db = tmp_path / "synthetic.sqlite"
    timeline = TimelineDatabase(db, ManualClock(datetime(2026, 11, 24, tzinfo=UTC)))
    timeline.open()
    store = LanguageStore(db, old_source_scope=partial(previous_language_scope, timeline))
    store.open()

    def sample(group: str, source_id: str, *, has_route: bool = True) -> LanguageSample:
        record = timeline.append(
            "message",
            "合成来源",
            ActorType.PERSON,
            "sender",
            VisibilityScope.GROUP,
            {
                "channel": "qq",
                "source_message_id": source_id,
                **(
                    {
                        "inbox_message_id": 1,
                        "account_id": "bot",
                        "message_type": "group",
                        "recipient_id": group,
                    }
                    if has_route
                    else {}
                ),
            },
        )
        scope = ConversationKey("qq", "bot", "group", group).language_scope_key()
        return language_sample(record, source_id, scope)

    def old(source: LanguageSample) -> LanguageSample:
        assert source.previous_source_key is not None
        return replace(source, source_key=source.previous_source_key, previous_source_key=None)

    def extract(source: LanguageSample) -> None:
        assert source.scope is not None
        proposal = LanguageProposal(
            LanguageKind.EXPRESSION, "好耶", "合成庆祝", (source.record_id,)
        )
        store.observe_extraction(
            proposal.kind,
            source.scope,
            [source],
            {source.record_id: source.content},
            [proposal],
        )

    try:
        origin = sample("10", "same-id")
        pending = sample("10", "pending-id")
        ambiguous = sample("10", "ambiguous-id", has_route=False)
        store.observe([old(origin), old(pending), old(ambiguous)], ())
        extract(old(origin))
        replay = sample("10", "same-id")
        replay_pending = sample("10", "pending-id")
        different = sample("20", "same-id")
        unknown = sample("20", "ambiguous-id")
        fresh_inputs = [origin, replay, replay_pending, pending, different, unknown]
        assert store.unseen(fresh_inputs) == [different]
        store.observe(fresh_inputs, ())
        assert store.pending_extraction(LanguageKind.EXPRESSION, fresh_inputs) == [different]
        assert store.pending_extraction(LanguageKind.EXPRESSION, [pending]) == [pending]
        extract(replay)
        extract(replay_pending)
        extract(unknown)
        extract(pending)
        extract(different)
        assert not store.pending_extraction(LanguageKind.EXPRESSION, fresh_inputs)
        entries = store.entries(LanguageKind.EXPRESSION)
        assert {entry.learning_source: entry.evidence_ids for entry in entries} == {
            origin.scope: (origin.record_id, pending.record_id),
            different.scope: (different.record_id,),
        }
    finally:
        timeline.close()
