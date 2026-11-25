"""平台收件箱网页接口使用临时库，不消耗心跳未读。"""

import json
from collections.abc import Iterable
from contextlib import closing
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import cast

import pytest
from PIL import Image

from aureus.common import ManualClock
from aureus.common.sqlite import open_connection
from aureus.heartbeat import MediaReference, MessageRoute, Perception, TrustLevel
from aureus.heartbeat.inbox_store import ConversationKey, InboxImageNote, InboxStore
from aureus.memory import EmojiStore, HistoricalMemory, MemoryIndex, MemorySource
from aureus.timeline import ActorType, TimelineRecord, VisibilityScope
from aureus.timeline.platform_history import LegacyPlatformHistory, LegacyRoute
from aureus.web.inbox_history import LEGACY_SCAN_BATCHES, LEGACY_SCAN_SIZE, timeline_page
from tests.web.config_rig import ConfigRig
from tests.web.test_timeline_api import NOW

CONVERSATIONS = "/api/inbox/conversations"
MESSAGES = "/api/inbox/messages"
UNCATEGORIZED = "/api/inbox/uncategorized"
GROUP = {"platform": "qq", "account_id": "1", "kind": "group", "recipient_id": "2"}


def test_requires_login_and_changed_password(tmp_path: Path) -> None:
    rig = ConfigRig(tmp_path, password_set=False)
    try:
        assert rig.client.get(CONVERSATIONS).status_code == 401
        assert rig.client.get(MESSAGES, params=GROUP).status_code == 401
        assert rig.client.get(UNCATEGORIZED, params={"source": "imported"}).status_code == 401
        rig.log_in("admin")
        assert rig.client.get(CONVERSATIONS).status_code == 403
        assert rig.client.get(MESSAGES, params=GROUP).status_code == 403
        assert rig.client.get(UNCATEGORIZED, params={"source": "timeline"}).status_code == 403
    finally:
        rig.close()


def test_empty_when_no_qq_store(tmp_path: Path) -> None:
    rig = ConfigRig(tmp_path)
    try:
        rig.log_in()
        assert rig.client.get(CONVERSATIONS).json() == {"enabled": False, "conversations": []}
        assert rig.client.get(MESSAGES, params=GROUP).json() == {
            "messages": [],
            "next_before_id": None,
        }
    finally:
        rig.close()


def test_typing_endpoints_return_only_authorized_selected_scope(tmp_path: Path) -> None:
    store = InboxStore(tmp_path / "inbox.db", ManualClock(NOW))
    group = ConversationKey(**GROUP)
    allowed = {group}
    rig = ConfigRig(tmp_path, inbox_store=store, allows_inbox_conversation=allowed.__contains__)
    try:
        assert rig.client.get("/api/chat/typing").status_code == 401
        assert rig.client.get("/api/inbox/typing", params=GROUP).status_code == 401
        rig.log_in()
        rig.heartbeat.replying_conversation = group
        res = rig.client.get("/api/inbox/typing", params=GROUP)
        assert res.json() == {"typing": True}
        assert res.headers["Cache-Control"] == "no-store"
        assert rig.client.get("/api/chat/typing").json() == {"typing": False}
        assert rig.client.get(
            "/api/inbox/typing",
            params={
                **GROUP,
                "account_id": "same-name-other-account",
            },
        ).json() == {"typing": False}
        allowed.clear()
        assert rig.client.get("/api/inbox/typing", params=GROUP).json() == {"typing": False}
        rig.heartbeat.replying_conversation = None
        rig.heartbeat.replying_web = True
        assert rig.client.get("/api/chat/typing").json() == {"typing": True}
        assert rig.client.post("/api/logout").status_code == 200
        assert rig.client.get("/api/chat/typing").status_code == 401
    finally:
        rig.close()


def test_inbox_conversations_uses_cached_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = InboxStore(tmp_path / "inbox.db", ManualClock(NOW))
    rig = ConfigRig(
        tmp_path, inbox_store=store, allows_inbox_conversation=lambda key: key.account_id == "1"
    )
    try:
        rig.timeline.append(
            "message",
            "旧群消息",
            ActorType.PERSON,
            "3",
            VisibilityScope.GROUP,
            metadata={
                "channel": "qq",
                "account_id": "1",
                "message_type": "group",
                "recipient_id": "2",
                "message_id": "old-1",
            },
        )
        rig.log_in()
        calls: list[int] = []
        original = rig.timeline.platform_history.routes

        def counting_routes() -> list[tuple[LegacyRoute, int, datetime]]:
            calls.append(1)
            return original()

        monkeypatch.setattr(rig.timeline.platform_history, "routes", counting_routes)
        first = rig.client.get(CONVERSATIONS).json()
        second = rig.client.get(CONVERSATIONS).json()
        assert [row["key"]["recipient_id"] for row in first["conversations"]] == ["2"]
        assert second == first
        assert len(calls) == 1
        rig.clock.advance(timedelta(seconds=59))
        assert rig.client.get(CONVERSATIONS).json() == first
        assert len(calls) == 1
        rig.clock.advance(timedelta(seconds=1))
        assert rig.client.get(CONVERSATIONS).json() == first
        assert len(calls) == 2
    finally:
        rig.close()


def test_inbox_conversations_scans_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = InboxStore(tmp_path / "inbox.db", ManualClock(NOW))
    rig = ConfigRig(
        tmp_path, inbox_store=store, allows_inbox_conversation=lambda key: key.account_id == "1"
    )
    bulk = json.dumps(
        {
            "channel": "qq",
            "account_id": "1",
            "message_type": "group",
            "recipient_id": "2",
            "message_id": "bulk",
        },
        ensure_ascii=False,
    )
    archive = json.dumps({"channel": "qq", "sender_name": "匿名"}, ensure_ascii=False)
    try:
        with closing(open_connection(rig.paths.database_file)) as database, database:
            database.execute(
                "INSERT INTO timeline (timestamp, actor_type, actor_id, event_type, content, "
                "visibility, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (NOW.isoformat(), "person", "3", "message", "未归类", "group", archive),
            )
            database.executemany(
                "INSERT INTO timeline (timestamp, actor_type, actor_id, event_type, content, "
                "visibility, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    (NOW.isoformat(), "person", "3", "message", "合成旧消息", "group", bulk)
                    for _ in range(200_000)
                ),
            )
        rig.log_in()
        calls = {"routes": 0, "page": 0}
        original = rig.timeline.platform_history.routes

        def counting_routes() -> list[tuple[LegacyRoute, int, datetime]]:
            calls["routes"] += 1
            return original()

        def unexpected_full_page(*_args: object, **_kwargs: object) -> list[object]:
            calls["page"] += 1
            raise AssertionError("会话目录不应再整页扫描旧时间线")

        monkeypatch.setattr(rig.timeline.platform_history, "routes", counting_routes)
        monkeypatch.setattr(rig.timeline.platform_history, "page", unexpected_full_page)
        first = rig.client.get(CONVERSATIONS).json()
        second = rig.client.get(CONVERSATIONS).json()
        assert second == first
        assert [row["key"]["recipient_id"] for row in first["conversations"]] == ["2"]
        assert first["archives"][0]["archive"] == "timeline"
        assert calls == {"routes": 1, "page": 0}
    finally:
        rig.close()


class _CappedHistory:
    """每次返回整批旧记录，用来核实扫描批数上限。"""

    def __init__(self, highest: int) -> None:
        self.highest = highest
        self.calls: list[tuple[int | None, int]] = []

    def page(self, _route: object, *, before_id: int | None, limit: int) -> list[TimelineRecord]:
        self.calls.append((before_id, limit))
        end = before_id if before_id is not None else self.highest + 1
        return [
            TimelineRecord(
                id=identifier,
                timestamp=NOW,
                actor_type=ActorType.PERSON,
                actor_id="3",
                event_type="message",
                content="旧消息",
                visibility=VisibilityScope.GROUP,
                metadata={"message_id": f"m{identifier}"},
            )
            for identifier in range(end - 1, end - 1 - limit, -1)
        ]


class _CanonicalStore:
    """把所有旧记录都当作已有规范原件，逼 timeline_page 扫到上限。"""

    def existing_message_ids(self, _key: object, ids: Iterable[str]) -> set[str]:
        return set(ids)

    def excluded_timeline_ids(self, _key: object, ids: Iterable[int]) -> set[int]:
        return set()


def test_timeline_page_caps_scan_batches_and_returns_cursor() -> None:
    key = ConversationKey(**GROUP)
    history = _CappedHistory(highest=10_000)
    store = cast(InboxStore, _CanonicalStore())
    rows, cursor = timeline_page(
        store, cast(LegacyPlatformHistory, history), key, before_id=None, limit=LEGACY_SCAN_SIZE
    )
    assert rows == []
    assert len(history.calls) == LEGACY_SCAN_BATCHES
    assert history.calls[0] == (None, LEGACY_SCAN_SIZE)
    assert history.calls[1] == (10_000 - LEGACY_SCAN_SIZE + 1, LEGACY_SCAN_SIZE)
    assert cursor == 10_000 - LEGACY_SCAN_BATCHES * LEGACY_SCAN_SIZE + 1
    following, next_cursor = timeline_page(
        store,
        cast(LegacyPlatformHistory, history),
        key,
        before_id=cursor,
        limit=LEGACY_SCAN_SIZE,
    )
    assert following == []
    assert len(history.calls) == 2 * LEGACY_SCAN_BATCHES
    assert history.calls[LEGACY_SCAN_BATCHES] == (cursor, LEGACY_SCAN_SIZE)
    assert next_cursor == cursor - LEGACY_SCAN_BATCHES * LEGACY_SCAN_SIZE


def test_first_inbox_page_returns_cursor_when_legacy_scan_is_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = InboxStore(tmp_path / "inbox.db", ManualClock(NOW))
    rig = ConfigRig(tmp_path, inbox_store=store, allows_inbox_conversation=lambda key: True)
    try:
        with closing(open_connection(rig.paths.database_file)) as database, database:
            database.executemany(
                "INSERT INTO timeline (timestamp, actor_type, actor_id, event_type, content, "
                "visibility, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        NOW.isoformat(),
                        "person",
                        "3",
                        "message",
                        "合成旧消息",
                        "group",
                        json.dumps(
                            {
                                "channel": "qq",
                                "account_id": "1",
                                "message_type": "group",
                                "recipient_id": "2",
                                "message_id": f"legacy-{number}",
                            },
                            ensure_ascii=False,
                        ),
                    )
                    for number in range(5_000)
                ),
            )
        rig.log_in()

        def all_canonical(_key: ConversationKey, ids: Iterable[str]) -> set[str]:
            return set(ids)

        monkeypatch.setattr(store, "existing_message_ids", all_canonical)
        first = rig.client.get(MESSAGES, params={**GROUP, "limit": 50}).json()
        assert first["messages"] == []
        assert first["next_before_source"] == "timeline"
        assert first["next_before_id"] is not None
        second = rig.client.get(
            MESSAGES,
            params={
                **GROUP,
                "limit": 50,
                "before_id": first["next_before_id"],
                "before_source": "timeline",
            },
        ).json()
        assert second["messages"] == []
        assert second["next_before_source"] == "timeline"
        assert second["next_before_id"] < first["next_before_id"]
    finally:
        rig.close()


def test_conversations_and_cursor_preserve_unread_and_route(tmp_path: Path) -> None:
    store = InboxStore(tmp_path / "inbox.db", ManualClock(NOW))
    rig = ConfigRig(
        tmp_path, inbox_store=store, allows_inbox_conversation=lambda key: key.account_id == "1"
    )
    try:
        rig.log_in()
        assert rig.client.get(CONVERSATIONS).json() == {"enabled": True, "conversations": []}
        for number in range(3):
            store.add(
                Perception(
                    "qq",
                    "3",
                    f"群消息 {number}",
                    VisibilityScope.GROUP,
                    TrustLevel.VERIFIED_CONTACT,
                    route=MessageRoute("1", "group", "2", str(number)),
                ),
                sender_name="测试群友",
                conversation_name="测试群",
            )
        store.add(
            Perception(
                "qq",
                "3",
                "私聊消息",
                VisibilityScope.USER_ONLY,
                TrustLevel.VERIFIED_CONTACT,
                route=MessageRoute("1", "private", "3", "4", "2"),
            ),
            sender_name="测试朋友",
            conversation_name="测试私聊",
        )
        before = rig.client.get(CONVERSATIONS).json()["conversations"]
        assert [(c["name"], c["key"]["kind"], c["unread_count"]) for c in before] == [
            ("测试私聊", "private", 1),
            ("测试群", "group", 3),
        ]
        first = rig.client.get(MESSAGES, params={**GROUP, "limit": 2}).json()
        assert [item["content"] for item in first["messages"]] == ["群消息 2", "群消息 1"]
        assert first["messages"][0]["sender_name"] == "测试群友"
        assert datetime.fromisoformat(first["messages"][0]["timestamp"]) == NOW
        assert first["next_before_id"] == first["messages"][-1]["id"]
        second = rig.client.get(
            MESSAGES, params={**GROUP, "limit": 2, "before_id": first["next_before_id"]}
        ).json()
        assert [item["content"] for item in second["messages"]] == ["群消息 0"]
        assert second["next_before_id"] is None
        private = rig.client.get(MESSAGES, params=before[0]["key"]).json()
        assert [item["content"] for item in private["messages"]] == ["私聊消息"]
        assert (
            rig.client.get(MESSAGES, params={**GROUP, "account_id": "9"}).json()["messages"] == []
        )
        assert rig.client.get(MESSAGES, params={**GROUP, "limit": 0}).status_code == 422
        assert rig.client.get(CONVERSATIONS).json()["conversations"] == before
    finally:
        rig.close()


def test_history_rechecks_complete_scope_and_visibility(tmp_path: Path) -> None:
    store = InboxStore(tmp_path / "inbox.db", ManualClock(NOW))
    allowed = {ConversationKey("qq", "1", "group", "2")}
    rig = ConfigRig(tmp_path, inbox_store=store, allows_inbox_conversation=allowed.__contains__)
    try:
        rig.log_in()
        for account, recipient, visibility, text in (
            ("1", "2", VisibilityScope.GROUP, "允许"),
            ("1", "2", VisibilityScope.PRIVATE, "独享"),
            ("7", "2", VisibilityScope.GROUP, "同名其他账号"),
            ("1", "9", VisibilityScope.GROUP, "同名其他群"),
        ):
            store.add(
                Perception(
                    "qq",
                    "3",
                    text,
                    visibility,
                    TrustLevel.VERIFIED_CONTACT,
                    route=MessageRoute(account, "group", recipient, text),
                ),
                sender_name="相似昵称",
                conversation_name="同一个名称",
            )
        page = rig.client.get(MESSAGES, params=GROUP).json()
        assert [message["content"] for message in page["messages"]] == ["允许"]
        assert page["messages"][0]["platform_message_id"] == "允许"
        assert (
            rig.client.get(MESSAGES, params={**GROUP, "account_id": "7"}).json()["messages"] == []
        )
        assert len(rig.client.get(CONVERSATIONS).json()["conversations"]) == 1
        allowed.clear()
        assert rig.client.get(MESSAGES, params=GROUP).json()["messages"] == []
        assert rig.client.get(CONVERSATIONS).json()["conversations"] == []
    finally:
        rig.close()


def test_legacy_history_pages_across_live_boundary_without_guessing(tmp_path: Path) -> None:
    store = InboxStore(tmp_path / "inbox.db", ManualClock(NOW))
    allowed = {ConversationKey("qq", "1", "group", "2"), ConversationKey("qq", "1", "group", "5")}
    rig = ConfigRig(tmp_path, inbox_store=store, allows_inbox_conversation=allowed.__contains__)
    route = {"channel": "qq", "account_id": "1", "message_type": "group", "recipient_id": "2"}
    try:
        rig.log_in()
        for number, body, metadata in (
            ("10", "旧一", route),
            ("11", "旧二", route),
            ("10", "重投重复", route),
            ("12", "入箱副本", route),
            ("13", "其他账号同名群", {**route, "account_id": "7"}),
            ("14", "未归类", {"channel": "qq", "sender_name": "同一昵称"}),
            ("15", "另一个会话", {**route, "recipient_id": "5"}),
            ("16", "外部账号缺失路由", {"channel": "qq", "account_id": "7"}),
        ):
            rig.timeline.append(
                "message",
                body,
                ActorType.PERSON,
                "3",
                VisibilityScope.GROUP,
                metadata={"sender_name": "同一昵称", "message_id": number, **metadata},
            )
        store.add(
            Perception(
                "qq",
                "3",
                "规范入箱",
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_CONTACT,
                route=MessageRoute("1", "group", "2", "12"),
            )
        )
        page = rig.client.get(MESSAGES, params={**GROUP, "limit": 2}).json()
        assert [(row["source"], row["content"]) for row in page["messages"]] == [
            ("inbox", "规范入箱"),
            ("timeline", "旧二"),
        ]
        following = rig.client.get(
            MESSAGES,
            params={
                **GROUP,
                "limit": 2,
                "before_id": page["next_before_id"],
                "before_source": page["next_before_source"],
            },
        ).json()
        assert [row["content"] for row in following["messages"]] == ["旧一"]
        assert following["next_before_id"] is None
        by_number = rig.client.get(MESSAGES, params={**GROUP, "limit": 2, "page": 2}).json()
        assert by_number == following
        assert rig.client.get(MESSAGES, params={**GROUP, "page": 0}).status_code == 422
        assert (
            rig.client.get(MESSAGES, params={**GROUP, "page": 2, "before_id": 1}).status_code == 422
        )
        catalog = rig.client.get(CONVERSATIONS).json()
        assert [row["key"]["recipient_id"] for row in catalog["conversations"]] == ["2", "5"]
        assert catalog["archives"][0]["archive"] == "timeline"
        legacy = rig.client.get(UNCATEGORIZED, params={"source": "timeline"}).json()
        assert [row["content"] for row in legacy["messages"]] == ["未归类"]
    finally:
        rig.close()


def test_legacy_private_routes_require_explicit_temporary_group(tmp_path: Path) -> None:
    store = InboxStore(tmp_path / "inbox.db", ManualClock(NOW))
    rig = ConfigRig(
        tmp_path, inbox_store=store, allows_inbox_conversation=lambda key: key.account_id == "1"
    )
    route = {"channel": "qq", "account_id": "1", "message_type": "private", "recipient_id": "3"}
    try:
        rig.log_in()
        for body, metadata in (
            ("普通私聊", {**route, "temporary_group_id": None}),
            ("临时私聊", {**route, "temporary_group_id": "2"}),
            ("缺临时群字段", route),
            ("缺账号", {"channel": "qq", "message_type": "private", "recipient_id": "3"}),
        ):
            rig.timeline.append(
                "message",
                body,
                ActorType.PERSON,
                "3",
                VisibilityScope.USER_ONLY,
                metadata=metadata,
            )
        catalog = rig.client.get(CONVERSATIONS).json()["conversations"]
        assert [row["key"]["temporary_group_id"] for row in catalog] == ["2", None]
        private = {"platform": "qq", "account_id": "1", "kind": "private", "recipient_id": "3"}
        for params, body in (
            (private, "普通私聊"),
            ({**private, "temporary_group_id": "2"}, "临时私聊"),
        ):
            page = rig.client.get(MESSAGES, params=params).json()
            assert [row["content"] for row in page["messages"]] == [body]
        archive = rig.client.get(UNCATEGORIZED, params={"source": "timeline"}).json()
        assert [row["content"] for row in archive["messages"]] == ["缺账号"]
    finally:
        rig.close()


def test_imported_archive_pages_raw_messages_without_assigning_routes(tmp_path: Path) -> None:
    rig = ConfigRig(tmp_path)
    index = MemoryIndex(rig.paths.database_file, rig.timeline, rig.clock)
    index.open()
    try:
        rig.log_in()
        index.publish(
            [
                HistoricalMemory(
                    MemorySource("old", "main", "mai_messages", (1,)), "旧来信", "同名"
                ),
                HistoricalMemory(MemorySource("old", "main", "messages", (2,)), "迁入正文", "同名"),
                HistoricalMemory(MemorySource("old", "memory", "fact_claims", (3,)), "非消息事实"),
                HistoricalMemory(
                    MemorySource("old", "main", "messages", (4,)), "已停用", status="inactive"
                ),
            ]
        )
        assert rig.client.get(CONVERSATIONS).json()["archives"][0]["archive"] == "imported"
        first = rig.client.get(UNCATEGORIZED, params={"source": "imported", "limit": 1}).json()
        second = rig.client.get(
            UNCATEGORIZED,
            params={
                "source": "imported",
                "limit": 1,
                "before_id": first["next_before_id"],
            },
        ).json()
        assert [first["messages"][0]["content"], second["messages"][0]["content"]] == [
            "迁入正文",
            "旧来信",
        ]
        assert first["messages"][0]["platform_message_id"] is None
        assert first["messages"][0]["visibility"] is None
        assert second["next_before_id"] is None
        assert (
            rig.client.get(
                UNCATEGORIZED,
                params={
                    "source": "imported",
                    "limit": 1,
                    "page": 2,
                },
            ).json()
            == second
        )
        assert rig.client.get(MESSAGES, params=GROUP).json()["messages"] == []
    finally:
        index.close()
        rig.close()


def test_inbox_image_requires_visible_message_exact_scope_and_component(tmp_path: Path) -> None:
    store = InboxStore(tmp_path / "inbox.db", ManualClock(NOW))
    emojis = EmojiStore(tmp_path / "emoji.db", tmp_path / "memory", ManualClock(NOW))
    emojis.open()
    encoded = BytesIO()
    Image.new("RGB", (2, 2), "red").save(encoded, "PNG")
    photo = encoded.getvalue()
    digest, _created = emojis.upload(photo, registration_description="编造描述")
    route = ConversationKey("qq", "1", "group", "2")
    allowed = {route, ConversationKey("qq", "7", "group", "2")}
    reads: list[str] = []

    async def read(perception: Perception, reference: MediaReference) -> tuple[bytes, str] | None:
        reads.append(reference.file_id)
        assert ConversationKey.from_perception(perception) == route
        if reference.file_id == "revoked":
            allowed.clear()
        return photo, "image/svg+xml" if reference.file_id == "bad-type" else "image/png"

    rig = ConfigRig(
        tmp_path,
        inbox_store=store,
        allows_inbox_conversation=allowed.__contains__,
        read_inbox_attachment=read,
        emoji_store=emojis,
    )
    components = (
        MediaReference("record", "voice"),
        MediaReference("image", "photo"),
        MediaReference("image", "emoji", True),
        MediaReference("image", "bad-type"),
        MediaReference("image", "revoked"),
    )
    try:
        for platform_id, visibility in (
            ("visible", VisibilityScope.GROUP),
            ("hidden", VisibilityScope.PRIVATE),
        ):
            store.add(
                Perception(
                    "qq",
                    "3",
                    "原件",
                    visibility,
                    TrustLevel.VERIFIED_CONTACT,
                    route=MessageRoute("1", "group", "2", platform_id),
                    attachments=components,
                )
            )
        visible = store.list_messages(route, visible_to_user=True)[0]
        hidden = store.list_messages(route)[0]
        endpoint = f"{MESSAGES}/{visible.id}/images"
        assert rig.client.get(f"{endpoint}/1", params=GROUP).status_code == 401
        rig.log_in()
        numbered = rig.client.get(MESSAGES, params={**GROUP, "page": 1}).json()
        assert numbered["messages"][0]["attachments"][2] == {"kind": "image", "is_emoji": True}
        cursor = rig.client.get(MESSAGES, params=GROUP).json()
        assert cursor["messages"][0]["attachments"][2]["file_id"] == "emoji"
        invalid_scope = {**GROUP, "account_id": "7"}
        for path, scope in (
            (f"{endpoint}/0", GROUP),
            (f"{endpoint}/99", GROUP),
            (f"{endpoint}/1", invalid_scope),
            (f"{MESSAGES}/{hidden.id}/images/1", GROUP),
        ):
            assert rig.client.get(path, params=scope).status_code == 404
        assert not reads
        ordinary_description = rig.client.get(
            f"{endpoint}/1", params={**GROUP, "view": "description"}
        )
        assert ordinary_description.json()["description"] is None
        assert not reads
        result = rig.client.get(f"{endpoint}/1", params=GROUP)
        assert result.content == photo
        assert result.headers["Content-Type"] == "image/png"
        assert result.headers["Cache-Control"] == "no-store"
        assert result.headers["Cross-Origin-Resource-Policy"] == "same-origin"
        store.save_image_note(
            visible.id, visible.perception, 2, InboxImageNote("emoji_catalog", digest)
        )
        description = rig.client.get(f"{endpoint}/2", params={**GROUP, "view": "description"})
        assert description.json() == {"source": "emoji_catalog", "description": "编造描述"}
        emojis.set_removed([f"asset:{digest}"], True)
        removed = rig.client.get(f"{endpoint}/2", params={**GROUP, "view": "description"})
        assert removed.json()["description"] is None
        assert rig.client.get(f"{endpoint}/3", params=GROUP).status_code == 404
        assert rig.client.get(f"{endpoint}/4", params=GROUP).status_code == 404
        assert reads == ["photo", "bad-type", "revoked"]
        assert store.list_conversations(visible_to_user=True)[0].unread_count == 1
    finally:
        rig.close()


def test_captions_follow_attachment_positions_and_complete_conversations(tmp_path: Path) -> None:
    path = tmp_path / "inbox.db"
    store = InboxStore(path, ManualClock(NOW))
    first_key, other_key = ConversationKey(**GROUP), ConversationKey("qq", "1", "group", "3")
    allowed = {first_key, other_key}
    attachments = (MediaReference("image", "a.png"), MediaReference("image", "b.png"))
    originals = []
    for key in (first_key, other_key):
        original = store.add(
            Perception(
                "qq",
                "sender",
                "原件",
                VisibilityScope.GROUP,
                TrustLevel.VERIFIED_CONTACT,
                route=MessageRoute(key.account_id, key.kind, key.recipient_id, "same-platform-id"),
                attachments=attachments,
            ),
            conversation_name="同名群",
        )
        assert original is not None
        originals.append(original)
    first, other = originals
    store.save_image_note(first.id, first.perception, 0, InboxImageNote("vlm", "甲"))
    store.save_image_note(first.id, first.perception, 1, InboxImageNote("vlm", "乙"))
    store.save_image_note(other.id, first.perception, 0, InboxImageNote("vlm", "错会话"))
    store.save_image_note(other.id, other.perception, 0, InboxImageNote("vlm", "另一群的甲"))
    rig = ConfigRig(
        tmp_path,
        inbox_store=InboxStore(path, ManualClock(NOW)),
        allows_inbox_conversation=allowed.__contains__,
    )
    try:
        rig.log_in()

        def caption(identifier: int, position: int, recipient_id: str) -> tuple[int, dict]:
            response = rig.client.get(
                f"{MESSAGES}/{identifier}/images/{position}",
                params={
                    **GROUP,
                    "recipient_id": recipient_id,
                    "view": "description",
                },
            )
            return response.status_code, response.json()

        assert caption(first.id, 0, "2") == (200, {"description": "甲", "source": "vlm"})
        assert caption(first.id, 1, "2") == (200, {"description": "乙", "source": "vlm"})
        assert caption(other.id, 0, "3")[1]["description"] == "另一群的甲"
        assert caption(other.id, 1, "3")[1]["description"] is None
        assert caption(first.id, 0, "3")[0] == 404
        with closing(open_connection(path)) as connection, connection:
            connection.execute(
                "UPDATE inbox_messages SET attachments = ? WHERE id = ?",
                (
                    json.dumps([["image", "replacement.png", False], ["image", "b.png", False]]),
                    first.id,
                ),
            )
        store.save_image_note(first.id, first.perception, 0, InboxImageNote("vlm", "迟到的甲"))
        assert caption(first.id, 0, "2")[1]["description"] is None
        assert caption(first.id, 1, "2")[1]["description"] == "乙"
        with closing(open_connection(path)) as connection, connection:
            connection.execute(
                "UPDATE inbox_messages SET attachments = ? WHERE id = ?",
                (json.dumps([["image", "a.png", False], ["image", "b.png", False]]), first.id),
            )
        assert caption(first.id, 0, "2")[1]["description"] is None
        allowed.remove(first_key)
        assert caption(first.id, 1, "2")[0] == 404
    finally:
        rig.close()


def test_caption_rechecks_permission_after_original_is_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InboxStore(tmp_path / "inbox.db", ManualClock(NOW))
    key = ConversationKey(**GROUP)
    source = Perception(
        "qq",
        "sender",
        "原件",
        VisibilityScope.GROUP,
        TrustLevel.VERIFIED_CONTACT,
        route=MessageRoute("1", "group", "2", "55"),
        attachments=(MediaReference("image", "a.png"),),
    )
    row = store.add(source)
    assert row is not None
    store.save_image_note(row.id, source, 0, InboxImageNote("vlm", "撤权前的图注"))
    allowed = {key}
    get_image = store.get_visible_image

    def read_then_revoke(
        conversation: ConversationKey,
        identifier: int,
        position: int,
    ) -> tuple[Perception, MediaReference, InboxImageNote | None] | None:
        result = get_image(conversation, identifier, position)
        allowed.clear()
        return result

    monkeypatch.setattr(store, "get_visible_image", read_then_revoke)
    rig = ConfigRig(tmp_path, inbox_store=store, allows_inbox_conversation=allowed.__contains__)
    try:
        rig.log_in()
        endpoint = f"{MESSAGES}/{row.id}/images/0"
        assert rig.client.get(endpoint, params={**GROUP, "view": "description"}).status_code == 404
    finally:
        rig.close()
