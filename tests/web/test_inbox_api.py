"""平台收件箱网页接口使用临时库，不消耗心跳未读。"""

from datetime import datetime
from pathlib import Path

from aureus.common import ManualClock
from aureus.heartbeat import MessageRoute, Perception, TrustLevel
from aureus.heartbeat.inbox_store import ConversationKey, InboxStore
from aureus.memory import HistoricalMemory, MemoryIndex, MemorySource
from aureus.timeline import ActorType, VisibilityScope
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
                "message", body, ActorType.PERSON, "3", VisibilityScope.GROUP,
                metadata={"sender_name": "同一昵称", "message_id": number, **metadata},
            )
        store.add(
            Perception(
                "qq", "3", "规范入箱", VisibilityScope.GROUP, TrustLevel.VERIFIED_CONTACT,
                route=MessageRoute("1", "group", "2", "12"),
            )
        )
        page = rig.client.get(MESSAGES, params={**GROUP, "limit": 2}).json()
        assert [(row["source"], row["content"]) for row in page["messages"]] == [
            ("inbox", "规范入箱"), ("timeline", "旧二"),
        ]
        following = rig.client.get(MESSAGES, params={
            **GROUP, "limit": 2, "before_id": page["next_before_id"],
            "before_source": page["next_before_source"],
        }).json()
        assert [row["content"] for row in following["messages"]] == ["旧一"]
        assert following["next_before_id"] is None
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
                "message", body, ActorType.PERSON, "3", VisibilityScope.USER_ONLY, metadata=metadata,
            )
        catalog = rig.client.get(CONVERSATIONS).json()["conversations"]
        assert [row["key"]["temporary_group_id"] for row in catalog] == ["2", None]
        private = {"platform": "qq", "account_id": "1", "kind": "private", "recipient_id": "3"}
        for params, body in ((private, "普通私聊"), ({**private, "temporary_group_id": "2"}, "临时私聊")):
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
        index.publish([
            HistoricalMemory(MemorySource("old", "main", "mai_messages", (1,)), "旧来信", "同名"),
            HistoricalMemory(MemorySource("old", "main", "messages", (2,)), "迁入正文", "同名"),
            HistoricalMemory(MemorySource("old", "memory", "fact_claims", (3,)), "非消息事实"),
            HistoricalMemory(MemorySource("old", "main", "messages", (4,)), "已停用", status="inactive"),
        ])
        assert rig.client.get(CONVERSATIONS).json()["archives"][0]["archive"] == "imported"
        first = rig.client.get(UNCATEGORIZED, params={"source": "imported", "limit": 1}).json()
        second = rig.client.get(UNCATEGORIZED, params={
            "source": "imported", "limit": 1, "before_id": first["next_before_id"],
        }).json()
        assert [first["messages"][0]["content"], second["messages"][0]["content"]] == [
            "迁入正文", "旧来信",
        ]
        assert first["messages"][0]["platform_message_id"] is None
        assert first["messages"][0]["visibility"] is None
        assert second["next_before_id"] is None
        assert rig.client.get(MESSAGES, params=GROUP).json()["messages"] == []
    finally:
        index.close()
        rig.close()
