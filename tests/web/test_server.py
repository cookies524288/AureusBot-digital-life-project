"""网页服务端的测试：登录、默认密码限制、历史对话（路线图 1.10，D63）。

用 Starlette 的测试客户端，不开真实端口。测试客户端的来源地址是 testclient，
不是回环地址，所以要测「本机」的路径时，把客户端的来源地址指定成 127.0.0.1。
"""

import asyncio
import base64
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import get_ident
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from PIL import Image

from aureus.channels import WebChannel
from aureus.common import ManualClock, WebConfig
from aureus.heartbeat import EVENT_MESSAGE, Perception
from aureus.memory.emoji import MAX_EMOJI_BYTES, EmojiStore, ImportedEmoji
from aureus.memory.history import MemorySource
from aureus.timeline import ActorType, TimelineDatabase, VisibilityScope
from aureus.web import LoginLimiter, PasswordStore, SessionStore, WebAuth, create_app
from aureus.web.server import SESSION_COOKIE

START = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
GOOD = "correct horse"
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


class FakeHeartbeat:
    def submit(self, perception: Perception) -> bool:
        return True


class Rig:
    def __init__(
        self,
        tmp_path: Path,
        config: WebConfig | None = None,
        *,
        open_terminal: Callable[[], Awaitable[None]] | None = None,
        request_shutdown: Callable[[], None] | None = None,
        resource_emojis: EmojiStore | None = None,
    ) -> None:
        self.clock = ManualClock(START)
        self.config = config or WebConfig(login_max_failures=3)
        self.passwords = PasswordStore(tmp_path / "web_password.argon2", FAST_HASHER)
        self.sessions = SessionStore(tmp_path / "aureus.db", self.clock, self.config.session_days)
        self.sessions.open()
        self.timeline = TimelineDatabase(tmp_path / "aureus.db", self.clock)
        self.timeline.open()
        self.auth = WebAuth(self.passwords, self.sessions, LoginLimiter(self.config, self.clock))
        self.app = create_app(
            config=self.config,
            auth=self.auth,
            sessions=self.sessions,
            channel=WebChannel(FakeHeartbeat()),
            timeline=self.timeline,
            open_terminal=open_terminal,
            request_shutdown=request_shutdown,
            resource_emojis=resource_emojis,
        )

    def client(self, host: str = "127.0.0.1") -> TestClient:
        return TestClient(self.app, client=(host, 50000))

    def close(self) -> None:
        self.sessions.close()
        self.timeline.close()


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path)
    yield made
    made.close()


def login(client: TestClient, password: str = "admin") -> Any:
    return client.post("/api/login", json={"password": password})


def set_password(rig: Rig) -> None:
    rig.passwords.set_password(GOOD)


def test_emoji_catalog_filters_actual_format_before_paging_and_keeps_global_counts(
    tmp_path: Path,
) -> None:
    store = EmojiStore(tmp_path / "emoji.db", tmp_path, ManualClock(START))
    store.open()

    def collect(image_format: str, color: str, description: str) -> str:
        with BytesIO() as stream:
            Image.new("RGB", (8, 8), color).save(stream, format=image_format)
            entry = store.collect(stream.getvalue(), description=description, register=True)
        assert entry.digest is not None
        return entry.digest

    first = collect("PNG", "blue", "示例.jpg")
    second = collect("PNG", "red", "示例第二张")
    jpeg = collect("JPEG", "green", "示例.png")
    damaged = collect("PNG", "purple", "示例损坏")
    removed = collect("PNG", "yellow", "示例移除")
    (tmp_path / "attachments" / damaged).write_bytes(b"\x89PNG\r\n\x1a\n")
    store.set_status(jpeg, is_registered=True, is_banned=True)
    store.set_status(damaged, is_registered=False, is_banned=False)
    assert store.set_removed([f"asset:{removed}"], removed=True)
    rig = Rig(tmp_path, resource_emojis=store)
    try:
        with rig.client() as client:
            set_password(rig)
            assert login(client, GOOD).status_code == 200
            endpoint = "/api/resources/media"
            params = {"q": "示例", "status": "registered", "format": "png", "limit": 1}
            page_one = client.get(endpoint, params=params).json()
            page_two = client.get(endpoint, params={**params, "offset": 1}).json()
            assert [page["items"][0]["digest"] for page in (page_one, page_two)] == sorted(
                [first, second]
            )
            assert page_one["items"][0]["media_type"] == "image/png"
            assert (page_one["total"], page_one["next_offset"]) == (2, 1)
            assert (page_two["total"], page_two["next_offset"]) == (2, None)
            most_used = max(first, second)
            store.record_use(most_used)
            sorted_page = client.get(endpoint, params={**params, "sort": "most_used"}).json()
            assert sorted_page["items"][0]["digest"] == most_used
            excluded = client.get(endpoint, params={"format": "png", "q": "损坏"}).json()
            assert excluded["items"] == [] and excluded["total"] == 0
            removed_page = client.get(endpoint, params={"format": "png", "removed": True}).json()
            assert [item["digest"] for item in removed_page["items"]] == [removed]
            counts = {"total": 5, "registered": 2, "unregistered": 1, "banned": 1, "removed": 1}
            for page in (page_one, page_two, sorted_page, excluded, removed_page):
                assert page["counts"] == counts
            assert client.get(endpoint, params={"sort": "bad"}).status_code == 422
            assert client.get(endpoint, params={"format": "svg"}).status_code == 422
    finally:
        rig.close()


def test_emoji_sequential_upload_keeps_existing_metadata_after_bad_file(tmp_path: Path) -> None:
    store = EmojiStore(tmp_path / "emoji.db", tmp_path, ManualClock(START))
    store.open()
    with BytesIO() as stream:
        Image.new("RGB", (8, 8), "blue").save(stream, format="PNG")
        first = stream.getvalue()
    with BytesIO() as stream:
        Image.new("RGB", (8, 8), "green").save(stream, format="JPEG")
        second = stream.getvalue()
    digest = sha256(first).hexdigest()
    source = MemorySource("fixture", "main", "images", (7,))
    store.import_records([ImportedEmoji(source, digest, "原说明", True, True, 4, 5, 0, None, "{}")])
    rig = Rig(tmp_path, resource_emojis=store)
    try:
        with rig.client() as client:
            set_password(rig)
            assert login(client, GOOD).status_code == 200
            endpoint = "/api/resources/emoji"
            headers = {"Origin": "http://testserver", "Content-Type": "application/octet-stream"}
            responses = [
                client.post(endpoint, content=data, headers=headers)
                for data in (first, b"invalid", second)
            ]
            assert [
                (response.status_code, response.json().get("created")) for response in responses
            ] == [(200, False), (422, None), (200, True)]
            assert store.read_catalog_media(digest) == first
            catalog = {row.digest: row for row in store.browse("", 0, 10)}
            original = catalog[digest]
            assert (original.description, original.is_registered, original.is_banned) == (
                "原说明",
                True,
                True,
            )
            assert (original.collection_count, original.use_count, original.source) == (
                4,
                5,
                source.encode(),
            )
            new = catalog[sha256(second).hexdigest()]
            assert not new.is_registered and not new.is_banned and new.has_file
            assert client.get("/api/resources/media").json()["counts"] == {
                "total": 2,
                "registered": 0,
                "unregistered": 1,
                "banned": 1,
                "removed": 0,
            }
    finally:
        rig.close()


def test_emoji_catalog_upload_and_maintenance_preserve_originals(tmp_path: Path) -> None:
    store = EmojiStore(tmp_path / "emoji.db", tmp_path, ManualClock(START))
    store.open()
    with BytesIO() as stream:
        Image.new("RGB", (8, 8), "blue").save(stream, format="PNG")
        image = stream.getvalue()
    digest = sha256(image).hexdigest()
    source = MemorySource("fixture", "main", "images", (7,))
    assert store.import_records(
        [
            ImportedEmoji(source, digest, "旧说明", False, True, 2, 3, 0, None, "{}"),
        ]
    ) == (1, 1)
    rig = Rig(tmp_path, resource_emojis=store)
    endpoint = "/api/resources/emoji"
    edit = f"{endpoint}/{digest}"
    media = "/api/resources/media"
    origin = {"Origin": "http://testserver"}
    headers = {**origin, "Content-Type": "application/octet-stream"}
    try:
        with rig.client() as client:
            assert client.post(endpoint, content=image, headers=headers).status_code == 401
            assert client.patch(edit, json={"is_banned": False}, headers=origin).status_code == 401
            assert login(client).json()["must_change_password"]
            denied = client.post(endpoint, content=image, headers=headers)
            assert denied.status_code == 403 and "默认密码" in denied.json()["detail"]
            set_password(rig)
            assert login(client, GOOD).status_code == 200
            foreign = {**headers, "Origin": "http://example.test"}
            assert client.post(endpoint, content=image, headers=foreign).status_code == 403
            assert client.patch(edit, json={"is_banned": False}).status_code == 403
            assert client.post(endpoint, content=image, headers=origin).status_code == 415
            assert client.post(endpoint, content=image[:16], headers=headers).status_code == 422
            assert client.post(endpoint, content=b"<svg/>", headers=headers).status_code == 422
            oversized = b"0" * (MAX_EMOJI_BYTES + 1)
            assert client.post(endpoint, content=oversized, headers=headers).status_code == 413
            assert (
                client.patch(edit, json={"is_registered": True}, headers=origin).status_code == 422
            )
            assert store.read_catalog_media(digest) is None

            upload = client.post(endpoint, content=image, headers=headers)
            assert upload.status_code == 200
            assert upload.json() == {"digest": digest, "created": False}
            [item] = client.get(media, params={"status": "banned"}).json()["items"]
            assert (item["text"], item["collection_count"], item["use_count"]) == ("旧说明", 2, 3)
            assert item["source"] == source.encode()

            payload = {
                "description": " 握手 ",
                "tags": [" 友好 ", "", "友好"],
                "is_registered": True,
            }
            assert client.patch(edit, json=payload, headers=origin).status_code == 200
            assert store.read_sendable(digest) is None
            assert client.patch(edit, json={"is_banned": False}, headers=origin).status_code == 200
            assert store.read_sendable(digest) == image
            assert client.patch(edit, json={"is_banned": True}, headers=origin).status_code == 200
            assert store.read_sendable(digest) is None
            assert client.post(endpoint, content=image, headers=headers).json() == upload.json()
            [item] = client.get(media, params={"q": "友好", "status": "banned"}).json()["items"]
            assert item["text"] == "握手" and item["tags"] == ["友好"] and item["is_registered"]
            assert (item["source"], item["collection_count"], item["use_count"]) == (
                source.encode(),
                2,
                3,
            )
            assert client.get(f"{media}/{digest}?kind=emoji").content == image
            assert client.patch(edit, json={"is_banned": False}, headers=origin).status_code == 200
            assert store.read_sendable(digest) == image
            assert (
                client.patch(edit, json={"is_registered": False}, headers=origin).status_code == 200
            )
            assert store.read_sendable(digest) is None
    finally:
        rig.close()


# ---------- 没登录什么都看不到 ----------


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/api/status"), ("get", "/api/history"), ("post", "/api/logout")],
)
def test_nothing_is_visible_without_logging_in(rig: Rig, method: str, path: str) -> None:
    response = getattr(rig.client(), method)(path)
    assert response.status_code == 401


def test_a_forged_cookie_is_not_a_login(rig: Rig) -> None:
    client = rig.client()
    client.cookies.set(SESSION_COOKIE, "x" * 43)
    assert client.get("/api/status").status_code == 401


def test_terminal_open_requires_local_same_origin_and_ready_login(tmp_path: Path) -> None:
    opened = 0

    async def open_terminal() -> None:
        nonlocal opened
        opened += 1

    rig = Rig(tmp_path, open_terminal=open_terminal)
    path = "/api/terminal/open"
    origin = {"Origin": "http://127.0.0.1"}
    try:
        client = TestClient(rig.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
        assert client.post(path, headers=origin).status_code == 401
        assert login(client).status_code == 200
        assert client.post(path, headers=origin).status_code == 403
        assert "can_open_terminal" not in client.get("/api/status").json()

        set_password(rig)
        assert login(client, GOOD).status_code == 200
        assert client.get("/api/status").json()["can_open_terminal"]
        assert client.post(path).status_code == 403
        assert client.post(path, headers={"Origin": "http://localhost"}).status_code == 403

        remote = TestClient(rig.app, base_url="http://127.0.0.1", client=("192.168.1.20", 50000))
        assert login(remote, GOOD).status_code == 200
        assert not remote.get("/api/status").json()["can_open_terminal"]
        assert remote.post(path, headers=origin).status_code == 403
        assert opened == 0

        assert client.post(path, headers=origin).status_code == 200
        assert opened == 1
    finally:
        rig.close()


def test_the_api_docs_are_not_exposed(rig: Rig) -> None:
    client = rig.client()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_shutdown_requires_ready_login_and_same_origin(tmp_path: Path) -> None:
    stopped = []
    rig = Rig(tmp_path, WebConfig(allow_remote=True), request_shutdown=lambda: stopped.append(True))
    path = "/api/shutdown"
    origin = {"Origin": "http://testserver"}
    try:
        client = rig.client()
        assert client.post(path, headers=origin).status_code == 401
        assert login(client).status_code == 200
        assert client.post(path, headers=origin).status_code == 403

        set_password(rig)
        client = rig.client(host="192.168.1.20")
        assert login(client, GOOD).status_code == 200
        assert client.post(path).status_code == 403
        assert client.post(path, headers={"Origin": "http://other.test"}).status_code == 403
        assert not stopped

        assert client.post(path, headers=origin).status_code == 200
        assert stopped == [True]
    finally:
        rig.close()


def test_running_app_keeps_its_own_page_when_the_html_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = tmp_path / "index.html"
    page.write_text("<p>first</p>", encoding="utf-8")
    graph = tmp_path / "organ-graph.js"
    resources = tmp_path / "resource-management.js"
    memory_flow = tmp_path / "memory-flow-transfer.js"
    tile = tmp_path / "organ-tile.png"
    graph.write_text("first()", encoding="utf-8")
    resources.write_text("loadFirst()", encoding="utf-8")
    memory_flow.write_text("transferFirst()", encoding="utf-8")
    tile.write_bytes(b"first tile")
    monkeypatch.setattr("aureus.web.server.INDEX_PAGE", page)
    running = Rig(tmp_path)
    try:
        page.write_text("<p>second</p>", encoding="utf-8")
        graph.write_text("second()", encoding="utf-8")
        resources.write_text("loadSecond()", encoding="utf-8")
        memory_flow.write_text("transferSecond()", encoding="utf-8")
        tile.write_bytes(b"second tile")
        client = running.client()
        assert client.get("/").text == "<p>first</p>"
        assert client.get("/assets/organ-graph.js").text == "first()"
        assert client.get("/assets/resource-management.js").text == "loadFirst()"
        assert client.get("/assets/memory-flow-transfer.js").text == "transferFirst()"
        assert client.get("/assets/organ-tile.png").content == b"first tile"
    finally:
        running.close()

    restarted = Rig(tmp_path)
    try:
        client = restarted.client()
        response = client.get("/")
        assert response.text == "<p>second</p>"
        assert response.headers["Cache-Control"] == "no-store"
        assert client.get("/assets/organ-graph.js").text == "second()"
        assert client.get("/assets/resource-management.js").text == "loadSecond()"
        assert client.get("/assets/memory-flow-transfer.js").text == "transferSecond()"
        image = client.get("/assets/organ-tile.png")
        assert image.headers["Content-Type"] == "image/png"
        assert image.content == b"second tile"
    finally:
        restarted.close()


def test_the_project_logo_is_embedded_and_used_without_a_new_route(rig: Rig) -> None:
    client = rig.client()
    source = (
        Path(__file__).resolve().parents[2]
        / "assets/logo/logo-no-ring-48x48-black-on-transparent.png"
    )
    html = client.get("/").text
    logos = {}
    for name in ("login-logo", "aureus-logo"):
        match = re.search(rf'<symbol id="{name}"[^>]*>.*?</symbol>', html, re.DOTALL)
        assert match is not None
        logo = ET.fromstring(match.group())
        assert logo.attrib["viewBox"] == "0 0 64 64"
        circle = logo.find("circle")
        assert circle is not None
        assert circle.attrib == {"class": "brand-mark-bg", "cx": "32", "cy": "32", "r": "30"}
        logos[name] = logo
    assert ".brand-mark-bg{fill:var(--brand-mark-bg,var(--bg))}" in html
    assert "--bg:#fdf6e3" in html
    assert ":root[data-theme='dark'] .brand-mark{--brand-mark-bg:transparent;" in html
    image = logos["aureus-logo"].find("image")
    assert image is not None
    assert image.attrib["href"] == "data:image/png;base64," + base64.b64encode(
        source.read_bytes()
    ).decode("ascii")
    assert html.count('<use href="#aureus-logo"/>') == 2
    assert '<use href="#login-logo"/></svg>AureusBot</h1>' in html
    assert 'src="/logo.svg"' not in html
    assert ">A</span>" not in html


# ---------- 登录 ----------


def test_default_password_logs_in_from_this_machine_and_says_to_change_it(rig: Rig) -> None:
    client = rig.client()
    response = login(client)
    assert response.status_code == 200
    assert response.json() == {"must_change_password": True}
    assert client.get("/api/status").json() == {"must_change_password": True}


def test_the_cookie_is_http_only_and_same_site_strict(rig: Rig) -> None:
    header = login(rig.client()).headers["set-cookie"].lower()
    assert header.startswith(f"{SESSION_COOKIE}=")
    assert "httponly" in header
    assert "samesite=strict" in header
    assert f"max-age={7 * 86400}" in header


def test_default_password_is_refused_from_the_network(rig: Rig) -> None:
    response = login(rig.client("192.168.1.20"))
    assert response.status_code == 401
    assert SESSION_COOKIE not in response.cookies


def test_a_wrong_password_is_401_without_echoing_it(rig: Rig) -> None:
    set_password(rig)
    response = login(rig.client(), "SECRET-guess")
    assert response.status_code == 401
    assert "SECRET" not in response.text


def test_too_many_failures_give_429_with_retry_after(rig: Rig) -> None:
    set_password(rig)
    client = rig.client("192.168.1.20")
    for _ in range(3):
        assert login(client, "wrong password").status_code == 401
    locked = login(client, GOOD)
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) >= 1


def test_default_login_limit_uses_seven_failures_and_a_thirty_second_retry(tmp_path: Path) -> None:
    rig = Rig(tmp_path, WebConfig())
    try:
        set_password(rig)
        client = rig.client()
        for _ in range(6):
            assert login(client, "wrong").status_code == 401
        assert login(client, GOOD).status_code == 200  # 六次失败仍能用正确密码登录。
        for _ in range(7):
            assert login(client, "wrong").status_code == 401
        locked = login(client, GOOD)
        assert locked.status_code == 429
        assert locked.headers["retry-after"] == "30"
        rig.clock.advance(timedelta(seconds=29))
        assert login(client, GOOD).headers["retry-after"] == "1"
        rig.clock.advance(timedelta(seconds=1))
        assert login(client, GOOD).status_code == 200
    finally:
        rig.close()


def test_a_long_password_is_refused_before_hashing(rig: Rig) -> None:
    assert login(rig.client(), "x" * 1000).status_code == 422


def test_unknown_fields_in_the_login_body_are_refused(rig: Rig) -> None:
    response = rig.client().post("/api/login", json={"password": "admin", "extra": 1})
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/status", "/api/history"])
async def test_login_dependencies_stay_on_the_request_thread(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    set_password(rig)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://testserver"
    ) as client:
        assert (await client.post("/api/login", json={"password": GOOD})).status_code == 200
        threads: list[int] = []
        original_touch = rig.sessions.touch
        original_is_default = rig.passwords.is_default

        def touch(token: str) -> bool:
            threads.append(get_ident())
            return original_touch(token)

        def is_default() -> bool:
            threads.append(get_ident())
            return original_is_default()

        monkeypatch.setattr(rig.sessions, "touch", touch)
        monkeypatch.setattr(rig.passwords, "is_default", is_default)
        assert (await client.get(path)).status_code == 200
        assert threads == [get_ident(), get_ident()]


@pytest.mark.asyncio
async def test_parallel_requests_keep_the_session_valid(rig: Rig) -> None:
    set_password(rig)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://testserver"
    ) as client:
        assert (await client.post("/api/login", json={"password": GOOD})).status_code == 200
        responses = await asyncio.gather(
            *(client.get(path) for path in ["/api/status", "/api/history"] * 50)
        )
        assert all(response.status_code == 200 for response in responses)
        assert (await client.get("/api/status")).status_code == 200


# ---------- 默认密码状态：只能改密码 ----------


def test_history_is_blocked_while_the_password_is_still_the_default(rig: Rig) -> None:
    client = rig.client()
    login(client)
    response = client.get("/api/history")
    assert response.status_code == 403
    assert "默认密码" in response.json()["detail"]


def test_changing_the_password_works_in_the_default_state(rig: Rig) -> None:
    client = rig.client()
    login(client)
    response = client.post("/api/password", json={"old_password": "admin", "new_password": GOOD})
    assert response.status_code == 200
    assert rig.auth.password_is_default is False


def test_changing_the_password_logs_this_device_out_too(rig: Rig) -> None:
    client = rig.client()
    login(client)
    client.post("/api/password", json={"old_password": "admin", "new_password": GOOD})
    assert client.get("/api/status").status_code == 401
    assert login(client, GOOD).json() == {"must_change_password": False}


@pytest.mark.parametrize("new", ["admin", "short", "x" * 300])
def test_a_bad_new_password_is_refused_and_nothing_changes(rig: Rig, new: str) -> None:
    client = rig.client()
    login(client)
    response = client.post("/api/password", json={"old_password": "admin", "new_password": new})
    assert response.status_code == 422
    assert rig.auth.password_is_default is True
    assert client.get("/api/status").status_code == 200  # 登录状态还在


def test_a_wrong_old_password_is_refused(rig: Rig) -> None:
    client = rig.client()
    login(client)
    response = client.post(
        "/api/password", json={"old_password": "not the old one", "new_password": GOOD}
    )
    assert response.status_code == 401
    assert rig.auth.password_is_default is True


# ---------- 退出 ----------


def test_logout_ends_only_this_device(rig: Rig) -> None:
    set_password(rig)
    phone, laptop = rig.client(), rig.client()
    login(phone, GOOD)
    login(laptop, GOOD)
    assert phone.post("/api/logout").status_code == 200
    assert phone.get("/api/status").status_code == 401
    assert laptop.get("/api/status").status_code == 200


# ---------- 历史对话 ----------


def add_message(rig: Rig, content: str, actor: ActorType, channel: str) -> None:
    rig.timeline.append(
        event_type=EVENT_MESSAGE,
        content=content,
        actor_type=actor,
        visibility=VisibilityScope.USER_ONLY,
        metadata={"channel": channel},
    )


def test_history_shows_every_channel_and_marks_where_each_came_from(rig: Rig) -> None:
    set_password(rig)
    add_message(rig, "终端上说的", ActorType.USER, "terminal")
    add_message(rig, "回给终端", ActorType.ME, "terminal")
    add_message(rig, "网页上说的", ActorType.USER, "web")
    client = rig.client()
    login(client, GOOD)

    items = client.get("/api/history").json()

    assert [(i["who"], i["channel"], i["content"]) for i in items] == [
        ("user", "terminal", "终端上说的"),
        ("me", "terminal", "回给终端"),
        ("user", "web", "网页上说的"),
    ]


def test_history_only_contains_messages(rig: Rig) -> None:
    set_password(rig)
    rig.timeline.append(
        event_type="cognitive_state",
        content="内部状态",
        actor_type=ActorType.ME,
        visibility=VisibilityScope.PRIVATE,
    )
    client = rig.client()
    login(client, GOOD)
    assert client.get("/api/history").json() == []


def test_history_limit_is_bounded_and_keeps_the_latest(rig: Rig) -> None:
    set_password(rig)
    for number in range(5):
        add_message(rig, f"第{number}条", ActorType.USER, "web")
    client = rig.client()
    login(client, GOOD)

    assert [i["content"] for i in client.get("/api/history?limit=2").json()] == [
        "第3条",
        "第4条",
    ]
    assert len(client.get("/api/history?limit=0").json()) == 1  # 下限 1
    assert len(client.get("/api/history?limit=100000").json()) == 5  # 上限 200，不报错
