"""用合成平台原件、时间线及网页登录测试清理的实际范围。"""

import json
from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from aureus.common import AureusPaths, ManualClock
from aureus.common.sqlite import open_connection
from aureus.heartbeat.history_retention import HistoryRetention
from aureus.heartbeat.inbox_images import InboxImageIndex
from aureus.heartbeat.inbox_store import ConversationKey, InboxImageNote, InboxStore
from aureus.heartbeat.perception import MediaReference
from aureus.heartbeat.platform_history import HistoryQuery, PlatformHistory
from aureus.memory import HistoricalMemory, MemoryIndex, MemorySource
from aureus.timeline import TimelineQuery
from aureus.timeline.platform_history import LegacyPlatformHistory
from tests.heartbeat.test_platform_history_tools import KEY, VISIBILITY, perception, record_legacy
from tests.web.config_rig import ConfigRig
from tests.web.test_timeline_api import GOOD, NOW

PATH = "/api/inbox/history-cleanup"
SCOPE = dict(platform=KEY.platform, account_id=KEY.account_id, kind=KEY.kind,
             recipient_id=KEY.recipient_id)
ORIGIN = {"Origin": "http://testserver"}


class RetentionRig:
    def __init__(self, tmp_path: Path) -> None:
        paths = AureusPaths(tmp_path)
        paths.ensure_directories()
        self.path = paths.database_file
        self.clock = ManualClock(NOW - timedelta(days=2))
        self.store = InboxStore(self.path, self.clock)
        self.retention = HistoryRetention(self.store, LegacyPlatformHistory(self.path), self.clock, None)
        self.allowed = {KEY}
        self.web = ConfigRig(tmp_path, clock=self.clock, inbox_store=self.store, inbox_retention=self.retention,
                             allows_inbox_conversation=lambda key: key in self.allowed)
        self.client = self.web.client
        self.cutoff = NOW.date()

    def advance_to_today(self) -> None:
        self.clock.advance(timedelta(days=2))

    def preview(self, cutoff: date | None = None) -> dict:
        preview = self.retention.preview(KEY, VISIBILITY, cutoff or self.cutoff)
        assert preview is not None
        self.retention.publish(preview)
        response = self.client.get(PATH, params=SCOPE)
        assert response.status_code == 200
        return response.json()["preview"]

    def confirm(self, preview: dict, password: str = GOOD, params: dict | None = None):
        return self.client.post(PATH, headers=ORIGIN, params=params or SCOPE,
                                json={"proposal_id": preview["id"], "password": password})


@pytest.fixture
def rig(tmp_path: Path):
    fixture = RetentionRig(tmp_path)
    yield fixture
    fixture.web.close()


def test_confirmation_rechecks_auth_only_removes_fixed_scoped_originals(rig: RetentionRig) -> None:
    store, timeline = rig.store, rig.web.timeline
    old = store.add(perception(KEY, "same", "old-original"))
    assert old is not None
    store.mark_read([old.id])
    other_key = ConversationKey("qq", "other-account", "group", KEY.recipient_id)
    other = store.add(perception(other_key, "same", "other-account"))
    unread = store.add(perception(KEY, "unread", "unread"))
    store.add_delivered(perception(KEY, "delivered", "delivered"), sender_name="bot")
    store.archive(perception(KEY, "archive", "archived"), sender_name="old", conversation_name="群", notice=True)
    record_legacy(timeline, KEY, "same", "same-legacy-copy")
    record_legacy(timeline, KEY, "legacy", "legacy-only")
    record_legacy(timeline, other_key, "legacy", "other-legacy")
    record_legacy(timeline, ConversationKey("qq", "", "group", KEY.recipient_id), "na", "unclassified")

    async def no_model(_request):
        pytest.fail("不能请求模型")

    index = InboxImageIndex(rig.path, no_model, lambda: True)
    assert other is not None
    index._save(old.id, "shared", "space", (1.0,))
    index._save(other.id, "shared", "space", (1.0,))
    memory = MemoryIndex(rig.path, timeline)
    memory.open()
    fact = HistoricalMemory(MemorySource("synthetic", "memory", "entities", (1,)), "保留派生事实")
    memory.publish([fact], snapshot=("synthetic", "fixture"))
    history = PlatformHistory(store, timeline.platform_history)
    try:
        rig.advance_to_today()
        assert rig.client.get(PATH, params=SCOPE).status_code == 401
        rig.web.log_in()
        preview = rig.preview()
        assert (preview["originals"], preview["timeline_only"], preview["image_vectors"]) == (2, 1, 1)
        assert "legacy-only" not in json.dumps(preview)
        assert rig.client.post(PATH, params=SCOPE, json={"proposal_id": preview["id"], "password": GOOD}).status_code == 403
        assert rig.confirm(preview, "wrong").status_code == 403
        rig.allowed.add(other_key)
        assert rig.confirm(preview, params={**SCOPE, "account_id": other_key.account_id}).status_code == 409
        rig.allowed.remove(KEY)
        assert rig.confirm(preview).status_code == 403
        rig.allowed.add(KEY)
        assert rig.confirm(preview).status_code == 409
        preview = rig.preview()
        late = store.add(replace(perception(KEY, "late", "late with old timestamp"),
                                 source_timestamp=int((NOW - timedelta(days=10)).timestamp())))
        assert late is not None
        record_legacy(timeline, KEY, "new-legacy", "late legacy")
        timeline_before = timeline.query(TimelineQuery(limit=100))
        assert rig.confirm(preview).status_code == 200
        assert rig.confirm(preview).status_code == 409
        assert store.add(perception(KEY, "same", "refetched copy")) is None
        view = rig.client.get("/api/inbox/messages", params=SCOPE).json()["messages"]
        assert {item["content"] for item in view} == {"unread", "archived", "late legacy", "late with old timestamp"}
        bot_view = json.loads(history.read(KEY, VISIBILITY, HistoryQuery(10, None, None)))["messages"]
        assert [item["content"] for item in bot_view] == ["late legacy"]
        assert unread is not None
        assert [message.id for message in store.list_unread(KEY)] == [unread.id, late.id]
        assert store.get_visible_message(other_key, other.id) == other
        assert timeline.query(TimelineQuery(limit=100)) == timeline_before
        assert memory.get_history(fact.source) == fact
        index._save(old.id, "shared", "space", (0.9,))
        with closing(open_connection(rig.path, read_only=True)) as connection:
            assert [row[0] for row in connection.execute("SELECT message_id FROM inbox_image_vectors")] == [other.id]
            audit = [tuple(row) for row in connection.execute("SELECT * FROM inbox_history_cleanup_audit")]
            assert len(audit) == 1
            assert "old-original" not in str(audit) and "legacy-only" not in str(audit)
    finally:
        memory.close()


def test_confirmed_cleanup_removes_caption_and_discards_late_write(rig: RetentionRig) -> None:
    source = replace(perception(KEY, "same", "原件"),
                     attachments=(MediaReference("image", "same-name"),))
    old = rig.store.add(source)
    assert old is not None
    rig.store.mark_read([old.id])
    caption = InboxImageNote("vlm", "只属于将清理的旧图")
    rig.store.save_image_note(old.id, source, 0, caption)
    rig.advance_to_today()
    rig.web.log_in()
    endpoint = f"/api/inbox/messages/{old.id}/images/0"
    params = {**SCOPE, "view": "description"}
    assert rig.client.get(endpoint, params=params).json() == {
        "source": "vlm", "description": caption.value,
    }
    preview = rig.preview()
    assert rig.confirm(preview).status_code == 200
    assert rig.store.add(source) is None
    rig.store.save_image_note(old.id, source, 0, caption)
    assert rig.client.get(endpoint, params=params).status_code == 404
    with closing(open_connection(rig.path, read_only=True)) as connection:
        assert not connection.execute(
            "SELECT 1 FROM inbox_messages WHERE id = ? OR image_notes <> '{}'", (old.id,)
        ).fetchone()


@pytest.mark.parametrize("change", ["zero", "read_water", "canonical_arrives", "vectors"])
def test_confirmation_requires_fresh_preview_after_source_or_water_changes(rig: RetentionRig, change: str) -> None:
    rig.web.log_in()
    store = rig.store
    old = store.add(perception(KEY, "old", "old"))
    assert old is not None
    store.mark_read([old.id])
    fresh = store.add(perception(KEY, "fresh", "fresh"))
    assert fresh is not None
    record_legacy(rig.web.timeline, KEY, "legacy", "legacy")
    rig.advance_to_today()
    preview = rig.preview((NOW - timedelta(days=3)).date() if change == "zero" else None)
    if change == "zero":
        assert (preview["originals"], preview["timeline_only"]) == (0, 0)
    elif change == "read_water":
        store.mark_read([fresh.id])
    elif change == "canonical_arrives":
        store.add(perception(KEY, "legacy", "late matching original"))
    else:
        async def no_model(_request):
            pytest.fail("不能请求模型")

        InboxImageIndex(rig.path, no_model, lambda: True)._save(old.id, "linked", "space", (1.0,))
    assert rig.confirm(preview).status_code == 409
    assert store.get_visible_message(KEY, old.id) is not None


def test_future_date_and_recent_receipt_with_old_display_time_are_not_candidates(rig: RetentionRig) -> None:
    rig.advance_to_today()
    rig.web.log_in()
    late = rig.store.add(replace(perception(KEY, "late", "旧日期迟到"),
                                 source_timestamp=int((NOW - timedelta(days=15)).timestamp())))
    assert late is not None
    rig.store.mark_read([late.id])
    assert late.timestamp.date() < rig.cutoff
    older_unknown = rig.store.add(replace(perception(KEY, "unknown", "未知本地收录时间"),
        source_timestamp=int((NOW - timedelta(days=20)).timestamp())))
    assert older_unknown is not None
    rig.store.mark_read([older_unknown.id])
    with closing(open_connection(rig.path)) as connection, connection:
        connection.execute("UPDATE inbox_messages SET received_at=NULL WHERE id=?", (older_unknown.id,))
    assert rig.retention.preview(KEY, VISIBILITY, NOW.date() + timedelta(days=1)) is None
    preview = rig.preview()
    assert preview["originals"] == 0
    assert rig.confirm(preview).status_code == 409
