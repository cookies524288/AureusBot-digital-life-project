"""用独立 SQLite 检查网页私聊原件、投递和 QQ 总结边界。"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aureus.channels.web import WebChannel
from aureus.common import ManualClock, MemoryConfig
from aureus.heartbeat.inbox_store import ConversationKey, InboxStore
from aureus.heartbeat.memory_summary_source import (
    WEB_PRIVATE_SCOPE,
    PlatformSummarySource,
    summary_scope_for,
)
from aureus.heartbeat.perception import MessageRoute, Perception, TrustLevel
from aureus.heartbeat.reply_records import delivery_callbacks
from aureus.memory import MemoryIndex, parse_delta
from aureus.timeline import ActorType, TimelineDatabase, VisibilityScope


def test_web_and_qq_consume_separate_originals_and_recall_only_same_scope(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.db"
    clock = ManualClock(datetime(2026, 11, 24, tzinfo=UTC))
    timeline = TimelineDatabase(path, clock)
    timeline.open()
    index = MemoryIndex(
        path,
        timeline,
        clock,
        config=MemoryConfig(consolidation_enabled=True, consolidation_extract_enabled=True),
    )
    index.open()
    store = InboxStore(path, clock)
    source = PlatformSummarySource(store, index, timeline)
    key = ConversationKey("qq", "100", "private", "200")
    scope = key.language_scope_key(), VisibilityScope.USER_ONLY
    source.allows = lambda route: route == key
    source.web_enabled = lambda: True
    index.summary_evidence = source.check_evidence
    visibility = VisibilityScope.USER_ONLY

    class VerifiedWebInput:
        def submit(self, perception: Perception) -> bool:
            timeline.append(
                "message",
                perception.content,
                ActorType.USER,
                actor_id=perception.sender_id,
                visibility=perception.visibility,
                metadata={"channel": perception.channel, "trust": perception.trust.value},
            )
            assert (
                summary_scope_for(
                    perception.channel,
                    perception.sender_id,
                    perception.trust,
                    perception.visibility,
                    None,
                )
                == WEB_PRIVATE_SCOPE
            )
            return True

    try:
        timeline.append(
            "message",
            "终端绝不能入窗",
            ActorType.USER,
            actor_id="local",
            visibility=visibility,
            metadata={"channel": "terminal", "trust": "verified_user"},
        )
        timeline.append(
            "message",
            "未验证也不能入窗",
            ActorType.USER,
            actor_id="local",
            visibility=visibility,
            metadata={"channel": "web", "trust": "unverified"},
        )
        web = WebChannel(VerifiedWebInput())
        assert web.send_from_user("星舟项目选择海桐馆演练。")
        metadata = {"channel": "web", "to_user": True, "outbox_internal": True}
        draft = timeline.append(
            "message",
            "正在准备的内部草稿",
            ActorType.ME,
            visibility=visibility,
            metadata=metadata,
        )
        draft2 = timeline.append(
            "message",
            "没有投递的回复",
            ActorType.ME,
            visibility=visibility,
            metadata=metadata,
        )
        confirm, _ = delivery_callbacks(timeline, draft, "已发回复只做上下文。", None)
        _, fail = delivery_callbacks(timeline, draft2, "没发送成功。", None)
        fail("合成取消")
        confirm()
        clock.advance(timedelta(seconds=1))
        for identifier in ("m1", "m2"):
            assert store.add(
                Perception(
                    "qq",
                    "200",
                    "另一个平台的私聊",
                    visibility,
                    TrustLevel.VERIFIED_USER,
                    route=MessageRoute("100", "private", "200", identifier),
                )
            )
        assert summary_scope_for("web", "local", TrustLevel.UNVERIFIED, visibility, None) is None
        assert summary_scope_for("web", "other", TrustLevel.VERIFIED_USER, visibility, None) is None
        window = source.next_window(2)
        assert window is not None and window.scope == WEB_PRIVATE_SCOPE
        user, assistant = window.messages
        assert user.is_human and assistant.content == "已发回复只做上下文。"
        assert not assistant.is_human and not (user.images or assistant.images)
        assert user.message_id.startswith("web:") and assistant.message_id.startswith("web:")
        delta = parse_delta(
            json.dumps(
                {
                    "summary": "此次演练将于海桐馆举行，安排已确定。",
                    "entities": [],
                    "relations": [],
                    "facts": [
                        {
                            "fact_id": "f1",
                            "text": "星舟项目的演练地点选定为海桐馆。",
                            "predicate": "演练地点",
                            "value": "海桐馆",
                            "shared": True,
                            "source": {
                                "message_id": user.message_id,
                                "sender_id": user.sender_id,
                                "excerpt": "选择海桐馆演练",
                            },
                            "image_evidence": [],
                        }
                    ],
                }
            ),
            window,
        )
        assert index.commit_summary(window, delta, source.check_window)
        assert index.summary_reference(WEB_PRIVATE_SCOPE)[0] == assistant.sequence
        qq = source.next_window(2)
        assert qq is not None and qq.scope == scope and qq.start == 0
        assert index.search(["海桐馆"], (), 3, summary_scope=scope) == []
        assert index.search(["海桐馆"], (), 3) == []
        hits = index.search(["海桐馆"], (), 3, summary_scope=WEB_PRIVATE_SCOPE)
        assert len(hits) == 1 and hits[0].evidence[0].reference == user.message_id
        assert hits[0].text != user.content
        source.web_enabled = lambda: False
        assert index.search(["海桐馆"], (), 3, summary_scope=WEB_PRIVATE_SCOPE) == []
    finally:
        index.close()
        timeline.close()
