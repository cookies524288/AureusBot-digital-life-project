"""验证本地 QQ 原件的媒体内容、具体秒位与失败时的事实边界。"""

import asyncio
import json
import shutil
import subprocess
import wave
from base64 import b64encode
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from websockets.asyncio.server import ServerConnection

from aureus.common import ManualClock
from aureus.heartbeat.inbox_store import ConversationKey
from aureus.organs import OrganRequest, OrganResponse
from aureus.timeline import VisibilityScope
from tests.channels.test_qq_behavior_replay import START, ReplayOrgan, open_replay
from tests.channels.test_qq_runtime import ACCOUNT, GROUP, FakeOneBot, event
from tests.prompt_assertions import dialogue_text

pytestmark = pytest.mark.asyncio


def ffmpeg(args: list[str], data: bytes = b"") -> bytes:
    command = shutil.which("ffmpeg")
    if command is None:
        pytest.skip("此项验证真实的可选本地 FFmpeg 解码")
    return subprocess.run(
        [command, "-nostdin", "-loglevel", "error", *args, "pipe:1"],
        input=data,
        capture_output=True,
        check=True,
        timeout=8,
    ).stdout


def sample(name: str) -> bytes:
    if name.endswith(".gif"):
        with BytesIO() as buffer:
            Image.new("RGB", (4, 4), "red").save(
                buffer,
                "GIF",
                save_all=True,
                append_images=[Image.new("RGB", (4, 4), "blue")],
                duration=200,
                loop=0,
            )
            return buffer.getvalue()
    if name.endswith(".mp4"):
        return ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                "color=red:s=16x16:r=2:d=1",
                "-f",
                "lavfi",
                "-i",
                "color=blue:s=16x16:r=2:d=2",
                "-filter_complex",
                "[0:v][1:v]concat=n=2:v=1:a=0",
                "-c:v",
                "mpeg4",
                "-movflags",
                "frag_keyframe+empty_moov",
                "-f",
                "mp4",
            ]
        )
    if name.endswith(".wav"):
        with BytesIO() as buffer:
            with wave.open(buffer, "wb") as stream:
                stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                stream.writeframes(b"\x00\x00" * 16000)
                stream.writeframes((b"\x00\x30" * 20 + b"\x00\xd0" * 20) * 800)
            return buffer.getvalue()
    return "标题：合成附件\n园内集合处是风车下面。".encode()


def install_media_server(
    patch: pytest.MonkeyPatch,
    group_message: dict[str, Any],
    contents: bytes,
) -> list[str]:
    actions: list[str] = []
    platform_url = "https://fixture.qq.com/only-from-adapter"
    media = group_message["message"][0]
    media_id = media["data"]["file_id"]

    async def serve_media(bot: FakeOneBot, remote: ServerConnection) -> None:
        async for frame in remote:
            request = json.loads(frame)
            action, params = request["action"], request["params"]
            envelope = {"status": "ok", "retcode": 0, "echo": request["echo"]}
            actions.append(action)
            if action == "get_login_info":
                await bot.send(remote, **envelope, data={"user_id": ACCOUNT})
                bot.connections.put_nowait(remote)
            elif action == "get_group_info":
                await bot.send(remote, **envelope, data={"group_name": "合成群"})
            elif action == "get_msg":
                assert params == {"message_id": str(group_message["message_id"])}
                verified = {**media, "data": {**media["data"], "url": platform_url}}
                await bot.send(remote, **envelope, data={**group_message, "message": [verified]})
            elif action == "get_group_file_url":
                assert params == {"group_id": str(GROUP), "file_id": media_id}
                await bot.send(remote, **envelope, data={"url": platform_url})
            elif action in ("download_file_image_stream", "download_file_stream"):
                assert params.get("file", params.get("file_id")) == (
                    media_id if action == "download_file_image_stream" else platform_url
                )
                for packet in (
                    {"type": "stream", "data_type": "file_info", "file_size": len(contents)},
                    {
                        "type": "stream",
                        "data_type": "file_chunk",
                        "index": 0,
                        "data": b64encode(contents).decode(),
                        "size": len(contents),
                    },
                    {
                        "type": "response",
                        "data_type": "file_complete",
                        "total_bytes": len(contents),
                        "total_chunks": 1,
                    },
                ):
                    await bot.send(remote, **envelope, stream="stream-action", data=packet)
            else:
                pytest.fail(f"不需要其他平台请求：{action}")

    patch.setattr(FakeOneBot, "handle", serve_media)
    return actions


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        ("image", "两色.gif"),
        ("file", "解析.txt"),
        ("video", "换色.mp4"),
        ("file", "片段.mp4"),
        ("file", "声音.wav"),
    ],
)
async def test_scoped_media_reaches_real_decoder_and_planner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    name: str,
) -> None:
    clock = ManualClock(START)
    identifier = "fixture-media-67"
    group_message = event(
        5701,
        2570,
        "",
        group=GROUP,
        message=[
            {
                "type": kind,
                "data": {
                    "file_id": identifier,
                    "name": name,
                    "sub_type": 4,
                    "url": "https://untrusted.invalid/not-fetched",
                },
            }
        ],
    )
    contents = await asyncio.to_thread(sample, name)
    actions = install_media_server(monkeypatch, group_message, contents)
    audio = name.endswith(".wav")
    timed = audio or name.endswith(".mp4")
    interpreter = ReplayOrgan("voice" if audio else "vlm", clock)
    observed: list[bytes] = []

    async def analyze(request: OrganRequest) -> OrganResponse:
        content = request.media_bytes
        assert content
        observed.append(content)
        if audio:
            assert request.media_type == "audio/mpeg"
            return OrganResponse(request.organ_id, "所选音频片段")
        assert request.media_type == "image/jpeg"
        with Image.open(BytesIO(content)) as frame:
            pixels = frame.convert("RGB").tobytes()

            def color_at(horizontal: int) -> bytes:
                start = 3 * (frame.width * (frame.height // 2) + horizontal)
                return pixels[start : start + 3]

            if name.endswith(".gif"):
                assert "普通动图" in request.prompt and frame.width == frame.height * 2
                assert (
                    color_at(frame.width // 4)[0] > 230 and color_at(3 * frame.width // 4)[2] > 230
                )
                return OrganResponse(request.organ_id, "画面先红后蓝")
            return OrganResponse(
                request.organ_id, "蓝色画面" if color_at(frame.width // 2)[2] > 230 else "红色画面"
            )

    monkeypatch.setattr(interpreter, "process", analyze)
    async with open_replay(tmp_path, monkeypatch, clock, emoji_vlm=interpreter) as replay:
        await replay.receive(group_message)
        await replay.step()
        if timed:
            await replay.receive(
                event(
                    5702,
                    2571,
                    "",
                    group=GROUP,
                    message=[
                        {"type": "reply", "data": {"id": "5701"}},
                        {"type": "text", "data": {"text": "看看这个附件在 0:02 的内容？"}},
                    ],
                )
            )
            await replay.step()
            assert len(observed) == 2
            expected = "2 秒附近：" + ("所选音频片段" if audio else "蓝色画面")
            if audio:
                sound = await asyncio.to_thread(
                    ffmpeg, ["-f", "mp3", "-i", "pipe:0", "-f", "s16le"], observed[-1]
                )
                assert len(sound) < 2 * 16000 * 2 and max(sound[8192:12288]) > 64
            else:
                assert "红色画面" in dialogue_text(replay.planner.calls[0][1])
        else:
            expected = "画面先红后蓝" if name.endswith(".gif") else "园内集合处是风车下面。"
        assert expected in dialogue_text(replay.planner.calls[-1][1])
        store = replay.core.inbox_store
        assert store is not None
        key = ConversationKey("qq", str(ACCOUNT), "group", str(GROUP))
        [saved] = store.list_read_history(
            key, VisibilityScope.GROUP, before_id=None, keyword=expected, limit=3
        )
        assert any(expected in text for text in saved.media_notes)
        assert (
            "get_group_file_url" in actions
            if kind == "file"
            else "get_group_file_url" not in actions
        )
        if timed:
            await replay.receive(
                event(
                    5703,
                    2571,
                    "",
                    group=GROUP,
                    message=[
                        {"type": "reply", "data": {"id": "5701"}},
                        {"type": "text", "data": {"text": "再看这个附件的第 99 秒。"}},
                    ],
                )
            )
            await replay.step()
            assert len(observed) == 2
            assert "99 秒附近：" not in dialogue_text(replay.planner.calls[-1][1])
        assert replay.fake.messages.empty() and not replay.replyer.calls
