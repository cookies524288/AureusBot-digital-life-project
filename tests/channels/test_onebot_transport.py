"""使用回环服务验证 OneBot 动作与连接清理。"""

import asyncio
import json
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from aureus.channels.onebot_transport import OneBotError, OneBotTransport
from aureus.common import QQConfig


@pytest.mark.asyncio
async def test_onebot_echo_failure_timeout_disconnect_and_shutdown() -> None:
    requests = asyncio.Queue[tuple[ServerConnection, dict[str, Any]]]()
    login_seen = asyncio.Event()

    async def handle(remote: ServerConnection) -> None:
        async for frame in remote:
            request = json.loads(frame)
            if request["action"] == "get_login_info":
                await remote.send(
                    json.dumps(
                        {
                            "echo": request["echo"],
                            "status": "ok",
                            "retcode": 0,
                            "data": {"user_id": "9000"},
                        }
                    )
                )
                await remote.send('{"post_type":"meta_event"}')
            else:
                requests.put_nowait((remote, request))

    async with asyncio.timeout(10), serve(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = QQConfig(
            enabled=True,
            endpoint=f"ws://127.0.0.1:{port}",
            account_id="9000",
            action_timeout_seconds=0.5,
            reconnect_seconds=0.01,
        )
        transport = OneBotTransport(config, None, lambda _: login_seen.set())
        connection = asyncio.create_task(transport.run())
        try:
            await login_seen.wait()
            socket = transport.socket
            assert socket is not None
            first = asyncio.create_task(transport.call(socket, "first", {}))
            second = asyncio.create_task(transport.call(socket, "second", {}))
            remote, first_request = await requests.get()
            _, second_request = await requests.get()
            await remote.send('{"echo":"unknown","status":"ok","retcode":0}')
            await remote.send(
                json.dumps({"echo": second_request["echo"], "status": "failed", "retcode": 100})
            )
            with pytest.raises(OneBotError, match="拒绝"):
                await second
            assert not first.done()
            await remote.send(
                json.dumps(
                    {"echo": first_request["echo"], "status": "ok", "retcode": 0, "data": {"ok": 1}}
                )
            )
            assert await first == {"ok": 1}
            with pytest.raises(TimeoutError):
                await transport.call(socket, "timeout", {})
            assert (await requests.get())[1]["action"] == "timeout"
            pending = asyncio.create_task(transport.call(socket, "pending", {}))
            await requests.get()
            login_seen.clear()
            await remote.close()
            with pytest.raises(OneBotError, match="断开"):
                await pending

            await login_seen.wait()
            socket = transport.socket
            assert socket is not None
            closing = asyncio.create_task(transport.call(socket, "closing", {}))
            assert (await requests.get())[1]["action"] == "closing"
        finally:
            connection.cancel()
            with pytest.raises(asyncio.CancelledError):
                await connection
        with pytest.raises(OneBotError, match="断开"):
            await closing
        assert transport.socket is None
        assert not transport._pending
        assert not any(task.get_name() == "aureus-onebot-reader" for task in asyncio.all_tasks())
