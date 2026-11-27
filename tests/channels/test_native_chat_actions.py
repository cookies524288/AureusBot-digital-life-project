"""用假 SnowLuma 核对从决策、原件复验到实际请求和回执，不调用真实模型或平台。"""

import asyncio
import json
from base64 import b64encode
from collections.abc import Callable
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from websockets.asyncio.server import ServerConnection

from aureus.common import ManualClock
from aureus.heartbeat.inbox_store import ConversationKey
from aureus.organs import OrganRequest, OrganResponse, ToolCall
from tests.channels import test_qq_behavior_replay as scenarios
from tests.channels.test_qq_behavior_replay import START, Replay, open_replay
from tests.channels.test_qq_runtime import ACCOUNT, GROUP, FakeOneBot, event
from tests.planner_fixtures import plan_response
from tests.prompt_assertions import dialogue_text

pytestmark = pytest.mark.asyncio
KEY = ConversationKey("qq", str(ACCOUNT), "group", str(GROUP))
SENDER = 3100
PLATFORM_URL = "https://multimedia.nt.qq.com.cn/download?fileid=scope"


class ActionPeer(FakeOneBot):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.originals: dict[str, dict[str, Any]] = {}
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.media: dict[str, bytes] = {}
        self.failure = ""
        self.bad_member = False
        self.file_url = PLATFORM_URL
        self.hold: asyncio.Event | None = None
        self.seen = asyncio.Event()
        self.on_member: Callable[[], None] | None = None

    async def send(self, remote: ServerConnection, **payload: Any) -> None:
        if payload.get("post_type") == "message":
            self.originals[str(payload["message_id"])] = dict(sub_type="friend", **payload)
        await super().send(remote, **payload)

    async def handle(self, remote: ServerConnection) -> None:
        async for frame in remote:
            req = json.loads(frame)
            method, params = req["action"], req["params"]
            envelope = {"status": "ok", "retcode": 0, "echo": req["echo"]}
            self.requests.append((method, params))
            data: Any = {}
            if method == "get_login_info":
                data = {"user_id": ACCOUNT}
                self.connections.put_nowait(remote)
            elif method == "get_group_info":
                data = {"group_name": "合成群"}
            elif method == "get_group_list":
                data = [{"group_id": GROUP, "group_name": "合成群"}]
            elif method == "get_group_member_info":
                if self.on_member:
                    self.on_member()
                data = {**params, "nickname": "" if self.bad_member else "群友"}
            elif method == "get_msg":
                data = self.originals.get(params["message_id"], {})
            elif method in ("get_group_file_url", "get_private_file_url"):
                data = {"url": self.file_url}
            elif method in ("download_file_stream", "download_file_image_stream"):
                content = self.media.get(params.get("file_id", params.get("file")), b"")
                for packet in (
                    {"type": "stream", "data_type": "file_info", "file_size": len(content)},
                    {
                        "type": "stream",
                        "data_type": "file_chunk",
                        "index": 0,
                        "data": b64encode(content).decode(),
                        "size": len(content),
                    },
                    {
                        "type": "response",
                        "data_type": "file_complete",
                        "total_chunks": 1,
                        "total_bytes": len(content),
                    },
                ):
                    await remote.send(
                        json.dumps({**envelope, "stream": "stream-action", "data": packet})
                    )
                continue
            elif method == "get_forward_msg":
                data = {"messages": [{"sender": {"nickname": "回声"}, "message": "苹果换成梨"}]}
            else:
                self.writes.append((method, params))
                self.seen.set()
                if self.hold:
                    await self.hold.wait()
                if self.failure == "reject":
                    envelope.update(status="failed", retcode=100)
                elif self.failure == "timeout":
                    continue
                elif self.failure != "missing" and method == "send_msg":
                    identifier = -6000 - len(self.writes)
                    data = {"message_id": identifier}
                    self.originals[str(identifier)] = {
                        **params,
                        **data,
                        "target_id": params.get("user_id"),
                        "user_id": ACCOUNT,
                        "sub_type": "friend",
                    }
                elif self.failure != "missing" and method.startswith("upload_"):
                    data = {"file_id": "scoped-file"}
            await remote.send(json.dumps({**envelope, "data": data}))


@pytest.fixture
def peer(monkeypatch: pytest.MonkeyPatch) -> ActionPeer:
    instance = ActionPeer()
    monkeypatch.setattr(scenarios, "FakeOneBot", lambda: instance)
    return instance


async def settle(replay: Replay) -> None:
    assert replay.core.qq is not None
    await asyncio.gather(*replay.core.qq._sending)


def choose(replay: Replay, kind: str, **arguments: Any) -> None:
    replay.planner.plan = {
        "reply": True,
        "intent": "按当前真实消息回应",
        "qq_action": {"kind": kind, **arguments},
    }


async def until(check: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not check():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    "kind,group",
    [
        ("face", GROUP),
        ("face", None),
        ("react", GROUP),
        ("poke", GROUP),
        ("poke", None),
        ("at", None),
        ("react", None),
    ],
)
async def test_native_action_is_one_platform_request_without_fake_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peer: ActionPeer, kind: str, group: int | None
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        choose(replay, kind, **({"emoji_id": "0"} if kind in {"react", "face"} else {}))
        await replay.receive(event(7101, SENDER, "收到这张卡了吗？", group=group))
        await replay.step()
        await settle(replay)
        if kind == "face":
            assert peer.writes == [
                (
                    "send_msg",
                    {
                        "message_type": "group" if group else "private",
                        "group_id" if group else "user_id": str(group or SENDER),
                        "message": [{"type": "face", "data": {"id": "0"}}],
                    },
                )
            ]
            assert replay.records("reply_delivered")[0].metadata["message_id"] == "-6001"
        elif group is None:
            assert not peer.writes and not replay.records("reply_delivered")
        else:
            assert peer.writes == (
                [("send_poke", {"group_id": str(group), "user_id": str(SENDER)})]
                if kind == "poke"
                else [("set_msg_emoji_like", {"message_id": "7101", "emoji_id": "0", "set": True})]
            )
            assert (
                len(replay.records("reply_delivered")) == 1
                and "message_id" not in replay.records("reply_delivered")[0].metadata
            )
        assert not replay.replyer.calls


@pytest.mark.parametrize("failure", ["reject", "timeout", "missing"])
async def test_failed_or_unidentified_receipt_is_not_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peer: ActionPeer, failure: str
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        qq = replay.core.qq
        assert qq is not None
        monkeypatch.setattr(
            qq._transport, "_config", qq._config.model_copy(update={"action_timeout_seconds": 0.1})
        )
        peer.failure = failure
        choose(replay, "face", emoji_id="264")
        await replay.receive(event(7101, SENDER, "笑一个", group=GROUP))
        await replay.step()
        await settle(replay)
        assert len(peer.writes) == 1 and len(replay.records("reply_delivery_failed")) == 1
        assert not replay.records("reply_delivered") and not replay.replyer.calls


async def test_inbound_reaction_keeps_actor_target_and_emits_no_extra_bubble(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peer: ActionPeer
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        replay.planner.plan = {"reply": True, "intent": "先回答这条问题"}
        await replay.receive(event(7150, SENDER, "先收到一条问题", group=GROUP))
        await replay.step()
        await settle(replay)
        assert len(peer.writes) == 1 and peer.writes[0][0] == "send_msg"
        replay.planner.plan = {"reply": False, "reason": "只记录这次表情回应"}

        qq, store = replay.core.qq, replay.core.inbox_store
        assert qq and store
        actor = SENDER + 1
        event_fields = {
            "post_type": "notice",
            "notice_type": "group_msg_emoji_like",
            "self_id": ACCOUNT,
            "group_id": GROUP,
            "user_id": actor,
            "operator_id": actor,
            "message_id": -6001,
            "message_seq": 771,
            "time": 1795360000,
        }
        for index, (change, count, verb) in enumerate(
            (("add", 2, "贴了"), ("remove", 1, "取消了")), 1
        ):
            notice = {
                **event_fields,
                "sub_type": change,
                "likes": [{"emoji_id": "264", "count": count}],
            }
            await peer.send(replay.remote, **notice)
            await peer.send(replay.remote, **notice)
            await until(lambda expected=index: len(qq._recent_notices) >= expected)
            await replay.step()
            prompt = dialogue_text(replay.planner.calls[-1][1])
            assert f"{actor}：对方{verb}你那条消息下的表情：[捂脸]（当前 {count}）" in prompt
            assert (
                "引用消息 -6001" in prompt
                and '表情回应的原件：{"作者": "你", "原话": "收到。"}' in prompt
            )
            assert len(replay.planner.calls) == index + 1
        await peer.send(
            replay.remote,
            post_type="notice",
            notice_type="group_recall",
            self_id=ACCOUNT,
            user_id=ACCOUNT,
            operator_id=ACCOUNT,
            group_id=GROUP,
            message_id=-6001,
        )
        await until(lambda: store.find_platform_message(KEY, "-6001") is None)
        notice = {**event_fields, "sub_type": "add", "likes": [{"emoji_id": "66", "count": 1}]}
        await peer.send(replay.remote, **notice)
        await peer.send(replay.remote, **{**notice, "message_id": 7150})
        await until(lambda: len(qq._recent_notices) == 5)
        await replay.step()
        assert len(replay.planner.calls) == 3
        [archived] = [
            item
            for item in store.list_messages(KEY)
            if item.direction == "notice"
            and item.perception.components[0].target_message_id == "7150"
        ]
        reaction = archived.perception.components[0]
        assert archived.perception.sender_id == str(actor)
        assert (reaction.kind, reaction.action, reaction.emoji_id, reaction.count) == (
            "reaction",
            "add",
            "66",
            1,
        )
        await replay.receive(event(7151, actor, "还有新事吗", group=GROUP))
        await replay.step()
        assert "收到。" not in dialogue_text(replay.planner.calls[-1][1])
        assert len(peer.writes) == 1 and len(replay.replyer.calls) == 1


@pytest.mark.parametrize("invalid", ["", "membership", "revoked", "another_group"])
async def test_mention_quotes_the_current_request_but_tags_the_quoted_member_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peer: ActionPeer, invalid: str
) -> None:
    clock = ManualClock(START)
    async with open_replay(tmp_path, monkeypatch, clock, split_reply=True) as replay:
        await replay.receive(
            event(7200, SENDER + 1, "我是接收人", group=GROUP + int(invalid == "another_group"))
        )
        await replay.step()
        peer.bad_member = invalid == "membership"
        if invalid == "revoked":
            peer.on_member = lambda: replay.core.heartbeat.set_inbox_route_filter(lambda _: False)

        async def words(request: OrganRequest) -> OrganResponse:
            return OrganResponse(request.organ_id, "第一句<n>第二句")

        monkeypatch.setattr(replay.replyer, "process", words)
        choose(replay, "at", source="quoted")
        replay.planner.plan["quote_source"] = True
        await replay.receive(
            event(
                7201,
                SENDER,
                "",
                group=GROUP,
                message=[
                    {"type": "reply", "data": {"id": "7200"}},
                    {"type": "text", "data": {"text": "喊一下那位群友"}},
                ],
            )
        )
        await replay.step()
        await settle(replay)
        clock.advance(timedelta(seconds=20))
        replay.core.heartbeat.beat()
        await settle(replay)
        if invalid:
            assert not peer.writes
        else:
            assert [params["message"] for _, params in peer.writes] == [
                [
                    {"type": "reply", "data": {"id": "7201"}},
                    {"type": "at", "data": {"qq": str(SENDER + 1)}},
                    {"type": "text", "data": {"text": "第一句"}},
                ],
                [{"type": "text", "data": {"text": "第二句"}}],
            ]


@pytest.mark.parametrize("group", [GROUP, None])
async def test_recall_only_ones_own_delivered_quote_and_do_not_treat_refusal_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peer: ActionPeer, group: int | None
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        replay.planner.plan = {"reply": True, "intent": "接一句"}
        await replay.receive(event(7301, SENDER, "问你一件事", group=group))
        await replay.step()
        await settle(replay)
        key = KEY if group else ConversationKey("qq", str(ACCOUNT), "private", str(SENDER))
        store = replay.core.inbox_store
        assert store and store.find_platform_message(key, "-6001")
        choose(replay, "recall", source="quoted")
        for identifier, target, reject in [
            (7302, "7301", False),
            (7303, "-6001", True),
            (7304, "-6001", False),
        ]:
            peer.failure = "reject" if reject else ""
            await replay.receive(
                event(
                    identifier,
                    SENDER,
                    "",
                    group=group,
                    message=[
                        {"type": "reply", "data": {"id": target}},
                        {"type": "text", "data": {"text": "撤回这条"}},
                    ],
                )
            )
            await replay.step()
            await settle(replay)
            assert bool(store.find_platform_message(key, "-6001")) is (identifier != 7304)
        assert [method for method, _ in peer.writes] == ["send_msg", "delete_msg", "delete_msg"]
        assert peer.writes[-1][1] == {"message_id": "-6001"}
        assert len(replay.records("reply_delivery_failed")) == 1 and len(replay.replyer.calls) == 1


@pytest.mark.parametrize("reject", [False, True])
async def test_no_typing_delay_still_waits_for_first_mention_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peer: ActionPeer, reject: bool
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START), split_reply=True) as replay:
        outbox = replay.core.heartbeat._reply_outbox
        monkeypatch.setattr(
            outbox, "_config", outbox._config.model_copy(update={"typing_delay_enabled": False})
        )

        async def words(request: OrganRequest) -> OrganResponse:
            return OrganResponse(request.organ_id, "第一段<n>第二段")

        monkeypatch.setattr(replay.replyer, "process", words)
        choose(replay, "at")
        peer.hold = asyncio.Event()
        try:
            await replay.receive(event(7220, SENDER, "请喊我一下", group=GROUP))
            await replay.step()
            await asyncio.wait_for(peer.seen.wait(), 2)
            replay.core.heartbeat.beat()
            assert len(peer.writes) == 1 and not replay.records("reply_delivered")
            assert peer.writes[0][1]["message"][0] == {"type": "at", "data": {"qq": str(SENDER)}}
            assert outbox.has_pending_conversation(KEY)
            peer.failure = "reject" if reject else ""
        finally:
            peer.hold.set()
        await settle(replay)
        replay.core.heartbeat.beat()
        await settle(replay)
        assert not outbox.has_pending_conversation(KEY)
        if reject:
            assert len(peer.writes) == 1 and not replay.records("reply_delivered")
        else:
            assert len(replay.records("reply_delivered")) == 2
            assert peer.writes[1][1]["message"] == [{"type": "text", "data": {"text": "第二段"}}]


@pytest.mark.parametrize(
    "late,group,enabled",
    [(False, GROUP, True), (True, GROUP, True), (False, None, True), (False, GROUP, False)],
)
async def test_follow_recall_waits_for_real_receipt_and_only_deletes_the_direct_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    peer: ActionPeer,
    late: bool,
    group: int | None,
    enabled: bool,
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        qq, store = replay.core.qq, replay.core.inbox_store
        assert qq and store
        monkeypatch.setattr(
            qq, "_config", qq._config.model_copy(update={"follow_recall_enabled": enabled})
        )
        replay.planner.plan = {"reply": True, "intent": "不显示引用地回应"}
        for identifier in (7350, 7351):
            if late and identifier == 7351:
                peer.seen.clear()
                peer.hold = asyncio.Event()
            await replay.receive(event(identifier, SENDER, "回应这条", group=group))
            await replay.step()
            await asyncio.wait_for(peer.seen.wait(), 2)
            if not peer.hold:
                await settle(replay)
        assert all(params["message"][0]["type"] == "text" for _, params in peer.writes)
        key = KEY if group else ConversationKey("qq", str(ACCOUNT), "private", str(SENDER))
        notice = {
            "post_type": "notice",
            "self_id": ACCOUNT,
            "notice_type": "group_recall" if group else "friend_recall",
            "user_id": SENDER,
            "message_id": 7351,
            **({"group_id": group, "operator_id": SENDER} if group else {}),
        }
        try:
            await peer.send(replay.remote, **notice)
            await until(lambda: store.find_platform_message(key, "7351") is None)
            await peer.send(replay.remote, **notice)
        finally:
            if peer.hold:
                peer.hold.set()
        await settle(replay)
        if enabled:
            await until(
                lambda: store.find_platform_message(key, "-6002") is None and len(peer.writes) == 3
            )
            assert peer.writes[-1] == ("delete_msg", {"message_id": "-6002"})
        else:
            assert len(peer.writes) == 2 and store.find_platform_message(key, "-6002")
        assert store.find_platform_message(key, "-6001")


@pytest.mark.parametrize("group", [GROUP, None])
async def test_received_poke_filters_bystanders_own_echo_and_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peer: ActionPeer, group: int | None
) -> None:
    clock = ManualClock(START)
    async with open_replay(tmp_path, monkeypatch, clock) as replay:
        qq = replay.core.qq
        assert qq
        choose(replay, "poke")

        async def poke(actor: int, target: int) -> None:
            count = len(qq._recent_notices)
            payload = {
                "post_type": "notice",
                "sub_type": "poke",
                "notice_type": "notify",
                "self_id": ACCOUNT,
                "time": int(clock.now().timestamp()),
                "user_id": actor if group else SENDER,
                "target_id": target,
                **({"group_id": GROUP} if group else {"sender_id": actor}),
            }
            await peer.send(replay.remote, **payload)
            await until(lambda: len(qq._recent_notices) > count)
            await replay.step()
            await settle(replay)

        if group:
            await poke(SENDER, SENDER + 1)
        await poke(ACCOUNT, SENDER)
        assert not replay.planner.calls
        await poke(SENDER, ACCOUNT)
        await poke(SENDER, ACCOUNT)
        assert len(replay.planner.calls) == 1 and not replay.replyer.calls
        assert peer.writes == [
            ("send_poke", {"user_id": str(SENDER), **({"group_id": str(GROUP)} if group else {})})
        ]
        clock.advance(timedelta(seconds=65))
        replay.planner.plan = {"reply": False}
        await poke(SENDER, ACCOUNT)
        assert len(replay.planner.calls) == 2 and len(peer.writes) == 1


def png(colour: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 2), colour).save(output, "PNG")
    return output.getvalue()


@pytest.mark.parametrize("status", ["approved", "unregistered", "banned", "revoked_after_read"])
async def test_resending_sticker_original_rechecks_the_same_catalog_entry_at_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peer: ActionPeer, status: str
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        catalog, qq, inbox = replay.core.emoji_store, replay.core.qq, replay.core.inbox_store
        assert catalog and qq and inbox
        content = peer.media["sticker"] = png("green")
        entry = catalog.collect(content, description="合成表情", register=status != "unregistered")
        assert entry and entry.digest
        digest = entry.digest
        if status == "banned":
            catalog.set_status(digest, is_registered=True, is_banned=True)
        await replay.receive(
            event(
                7420,
                SENDER,
                "",
                group=GROUP,
                message=[{"type": "image", "data": {"file": "sticker", "sub_type": 1}}],
            )
        )
        await replay.step()
        source = inbox.find_platform_message(KEY, "7420")
        assert source and source.perception.attachments[0].is_emoji
        assert await qq.read_inbox_attachment(
            source.perception, source.perception.attachments[0]
        ) == (content, "image/png")
        if status == "revoked_after_read":

            def revoke_after_read(identifier: str) -> bytes | None:
                approved = catalog.read_sendable(identifier)
                assert identifier == digest and approved == content
                catalog.set_status(identifier, is_registered=True, is_banned=True)
                return approved

            monkeypatch.setattr(qq, "_read_emoji", revoke_after_read)
        choose(replay, "resend_image", source="quoted")
        await replay.receive(
            event(
                7421,
                SENDER,
                "",
                group=GROUP,
                message=[
                    {"type": "reply", "data": {"id": "7420"}},
                    {"type": "text", "data": {"text": "发回这张原图"}},
                ],
            )
        )
        await replay.step()
        await settle(replay)
        if status == "approved":
            assert [params["message"] for _, params in peer.writes] == [
                [
                    {
                        "type": "image",
                        "data": {"file": "base64://" + b64encode(content).decode(), "sub_type": 1},
                    }
                ]
            ]
            assert len(replay.records("reply_delivered")) == 1
        else:
            assert not peer.writes and not replay.records("reply_delivered")
            assert len(replay.records("reply_delivery_failed")) == 1
        assert catalog.browse("", 0, 10)[0].use_count == int(status == "approved")


@pytest.mark.parametrize("number,forged", [(2, False), (3, False), (1, True)])
async def test_resend_only_the_requested_image_in_returned_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peer: ActionPeer, number: int, forged: bool
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        peer.media.update(first=png("red"), second=png("blue"))
        await replay.receive(
            event(
                7401,
                SENDER,
                "",
                group=GROUP,
                message=[
                    {"type": "text", "data": {"text": "这两张风景"}},
                    {"type": "image", "data": {"file": "first"}},
                    {"type": "image", "data": {"file": "second"}},
                ],
            )
        )
        await replay.step()

        async def history(request: OrganRequest) -> OrganResponse:
            if not request.tool_history:
                return OrganResponse(
                    request.organ_id,
                    "",
                    tool_calls=(ToolCall("read_platform_history", "images", {"keyword": "风景"}),),
                )
            [row] = json.loads(request.tool_history[-1].results[0].content)["messages"]
            return plan_response(
                request,
                {
                    "reply": True,
                    "intent": "重发指定位置",
                    "qq_action": {
                        "kind": "resend_image",
                        "source": "history",
                        "history_record_id": 9876 if forged else row["id"],
                        "image_number": number,
                    },
                },
            )

        monkeypatch.setattr(replay.planner, "process", history)
        await replay.receive(event(7402, SENDER, "请发回指定图", group=GROUP))
        await replay.step()
        await settle(replay)
        if number == 2 and not forged:
            assert [params["message"] for _, params in peer.writes] == [
                [
                    {
                        "type": "image",
                        "data": {"file": "base64://" + b64encode(peer.media["second"]).decode()},
                    }
                ]
            ]
        else:
            assert not peer.writes
        assert not replay.replyer.calls


@pytest.mark.parametrize(
    "name,group",
    [
        ("讲义.txt", GROUP),
        ("讲义.txt", None),
        ("封面.png", GROUP),
        ("秘密.txt", GROUP),
        ("../讲义.txt", GROUP),
    ],
)
async def test_approved_asset_actual_image_or_file_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peer: ActionPeer, name: str, group: int | None
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        root = replay.core.paths.chat_assets_dir / str(ACCOUNT)
        directory = root / ("group" if group else "private") / str(group or SENDER)
        directory.mkdir(parents=True)
        other = root / "group" / str(GROUP + 1)
        other.mkdir(parents=True)
        other.joinpath("秘密.txt").write_text("别群的授权", encoding="utf-8")
        image, text = png("green"), b"agenda"
        directory.joinpath("讲义.txt").write_bytes(text)
        directory.joinpath("封面.png").write_bytes(image)
        choose(replay, "send_asset", name=name)
        await replay.receive(event(7501, SENDER, "把这份素材发给我", group=group))
        await replay.step()
        await settle(replay)
        if name == "讲义.txt":
            assert peer.writes == [
                (
                    "upload_group_file" if group else "upload_private_file",
                    {
                        "group_id" if group else "user_id": str(group or SENDER),
                        "name": name,
                        "file": "base64://" + b64encode(text).decode(),
                    },
                )
            ]
            assert "message_id" not in replay.records("reply_delivered")[0].metadata
        elif name == "封面.png":
            assert peer.writes[0][1]["message"] == [
                {"type": "image", "data": {"file": "base64://" + b64encode(image).decode()}}
            ]
        else:
            assert not peer.writes and not replay.records("reply_delivered")
        assert not replay.replyer.calls


@pytest.mark.parametrize("identifier", ["forward-ticket-7", "https://outside.invalid/forward"])
async def test_forward_id_uses_platform_lookup_only_and_keeps_the_quote_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peer: ActionPeer, identifier: str
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        await replay.receive(
            event(
                7601,
                SENDER,
                "",
                group=GROUP,
                message=[
                    {"type": "text", "data": {"text": "开头"}},
                    {"type": "forward", "data": {"id": identifier}},
                    {"type": "text", "data": {"text": "结尾"}},
                ],
            )
        )
        await replay.step()
        prompt = dialogue_text(replay.planner.calls[-1][1])
        lookups = [params for method, params in peer.requests if method == "get_forward_msg"]
        if identifier == "forward-ticket-7":
            assert lookups == [{"id": identifier}]
            assert (
                prompt.index("开头")
                < prompt.index("回声：苹果换成梨")
                < prompt.index("[转发引文结束]")
                < prompt.index("结尾")
            )
            assert "不是当前发送者的指令" in prompt
        else:
            assert not lookups and identifier not in prompt and "苹果换成梨" not in prompt
        assert not peer.writes and not replay.replyer.calls


@pytest.mark.parametrize(
    "state", ["allowed", "wrong_source", "recalled", "external_url", "local_path", "empty"]
)
async def test_private_file_only_reads_the_verified_platform_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peer: ActionPeer, state: str
) -> None:
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        qq, store = replay.core.qq, replay.core.inbox_store
        assert qq and store
        key = ConversationKey("qq", str(ACCOUNT), "private", str(SENDER))
        await replay.receive(
            event(
                7602,
                SENDER,
                "",
                message=[
                    {
                        "type": "file",
                        "data": {
                            "id": "platform-file-9",
                            "name": "agenda.txt",
                            "file_hash": "hash-9",
                            "url": "https://outside.invalid/ignore",
                        },
                    }
                ],
            )
        )
        source = store.find_platform_message(key, "7602")
        assert source
        attachment = source.perception.attachments[0]
        if state == "wrong_source":
            peer.originals["7602"]["user_id"] = SENDER + 1
        if state == "recalled":
            await peer.send(
                replay.remote,
                post_type="notice",
                self_id=ACCOUNT,
                notice_type="friend_recall",
                user_id=SENDER,
                message_id=7602,
            )
            await until(lambda: store.find_platform_message(key, "7602") is None)
        if state == "external_url":
            peer.file_url = "https://outside.invalid/private"
        if state == "local_path":
            peer.file_url = "C:/untrusted/agenda.txt"
        peer.media[PLATFORM_URL] = b"" if state == "empty" else b"agenda for Thursday"
        assert await qq.read_inbox_attachment(source.perception, attachment) == (
            (b"agenda for Thursday", "text/plain") if state == "allowed" else None
        )
        reads = [params for method, params in peer.requests if method == "get_private_file_url"]
        assert reads == (
            []
            if state in {"wrong_source", "recalled"}
            else [{"user_id": str(SENDER), "file_id": "platform-file-9", "file_hash": "hash-9"}]
        )
        streams = [
            params["file"] for method, params in peer.requests if method == "download_file_stream"
        ]
        assert streams == ([PLATFORM_URL] if state in {"allowed", "empty"} else [])
        assert not peer.writes
