"""逐跳地址检查、正文提取和资源限制；不连接真实网络（D79）。"""

import asyncio

import httpx
import pytest

from aureus.actions.web import FetchFailure, WebPage, WebReader
from aureus.common import ExplorationConfig
from aureus.safety.permissions import (
    ACTION_READ_WEB,
    ACTION_WEB_SEARCH,
    DEFAULT_ACTION_LEVELS,
    PermissionLevel,
)

pytestmark = pytest.mark.asyncio
PUBLIC = "93.184.216.34"


async def test_read_only_web_actions_are_l1() -> None:
    assert DEFAULT_ACTION_LEVELS[ACTION_READ_WEB] is PermissionLevel.ROUTINE
    assert DEFAULT_ACTION_LEVELS[ACTION_WEB_SEARCH] is PermissionLevel.ROUTINE


async def public_resolve(host: str) -> list[str]:
    return [PUBLIC]


def reader(handler, *, resolve=public_resolve, **options) -> WebReader:
    return WebReader(
        ExplorationConfig(**options), transport=httpx.MockTransport(handler), resolve=resolve
    )


async def test_html_title_blocks_and_ignored_tags() -> None:
    html = "<title> 测试 &amp; 标题 </title><h1>开头</h1><p> 正文  内容 </p>"
    html += "".join(
        f"<{tag}>不能出现</{tag}>" for tag in ("script", "style", "template", "noscript", "svg")
    )
    page = await reader(
        lambda _: httpx.Response(200, text=html, headers={"content-type": "text/html"})
    ).read("https://example.com")
    assert isinstance(page, WebPage)
    assert page.title == "测试 & 标题" and page.text == "开头\n正文 内容"
    assert not page.truncated


async def test_plain_text_encoding_and_character_limit() -> None:
    page = await reader(
        lambda _: httpx.Response(
            200, content=b"\xe9" * 300, headers={"content-type": "text/plain; charset=windows-1252"}
        ),
        max_page_chars=200,
    ).read("https://example.com")
    assert isinstance(page, WebPage) and page.text == "é" * 200 and page.truncated


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "http://10.1.2.3",
        "http://192.168.1.1",
        "http://169.254.1.1",
        "http://[::1]",
        "http://[::ffff:127.0.0.1]",
        "http://224.0.0.1",
        "http://[ff02::1]",
    ],
)
async def test_local_private_link_local_and_mapped_addresses_are_blocked(url: str) -> None:
    calls = []
    result = await reader(lambda request: calls.append(request)).read(url)
    assert result is FetchFailure.BLOCKED_ADDRESS and calls == []


@pytest.mark.parametrize("addresses", [["127.0.0.1"], [PUBLIC, "10.0.0.1"], []])
async def test_localhost_and_mixed_dns_answers_are_blocked(addresses: list[str]) -> None:
    async def resolve(host: str) -> list[str]:
        assert host == "localhost"
        return addresses

    assert (
        await reader(lambda _: pytest.fail("不应连接"), resolve=resolve).read("http://localhost")
        is FetchFailure.BLOCKED_ADDRESS
    )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com",
        "https://example.com:80",
        "http://example.com:443",
        "http://example.com:0",
        "https://user:pass@example.com",
        "https://example.com:bad",
        "https:///missing",
        "https://[bad",
    ],
)
async def test_bad_scheme_port_credentials_and_shape_are_rejected(url: str) -> None:
    assert await reader(lambda _: pytest.fail("不应连接")).read(url) is FetchFailure.BAD_URL


async def test_redirect_to_internal_address_is_checked_before_request() -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://10.0.0.1/private"})

    assert await reader(handle).read("https://example.com") is FetchFailure.BLOCKED_ADDRESS
    assert len(calls) == 1


async def test_redirect_limit_is_three() -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"location": "/again"})

    assert await reader(handle).read("https://example.com") is FetchFailure.TOO_MANY_REDIRECTS
    assert len(calls) == 4


async def test_get_has_no_cookie_credentials_or_environment_proxy(monkeypatch) -> None:
    original = httpx.AsyncClient

    def build(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["cookies"] is None
        assert kwargs["follow_redirects"] is False
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", build)
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        assert "cookie" not in request.headers and "authorization" not in request.headers
        assert request.headers["user-agent"].startswith("AureusBot/")
        assert set(request.headers) == {"host", "user-agent", "accept"}
        if len(calls) == 1:
            return httpx.Response(
                302, headers={"location": "/done", "set-cookie": "secret=x; Path=/"}
            )
        return httpx.Response(200, text="资料", headers={"content-type": "text/plain"})

    page = await reader(handle).read("https://example.com")
    assert isinstance(page, WebPage) and page.url == "https://example.com/done"


async def test_total_timeout_includes_dns() -> None:
    async def resolve(host: str) -> list[str]:
        await asyncio.sleep(1)
        return [PUBLIC]

    assert (
        await reader(
            lambda _: pytest.fail("不应连接"), resolve=resolve, fetch_timeout_seconds=0.01
        ).read("https://example.com")
        is FetchFailure.TIMEOUT
    )


async def test_oversize_page_stops_at_byte_limit() -> None:
    page = await reader(
        lambda _: httpx.Response(200, content=b"a" * 3000, headers={"content-type": "text/plain"}),
        max_page_bytes=1024,
    ).read("https://example.com")
    assert isinstance(page, WebPage) and page.text == "a" * 1024 and page.truncated


@pytest.mark.parametrize(
    ("status", "kind", "expected"),
    [
        (200, "image/png", FetchFailure.NOT_TEXT),
        (404, "text/plain", FetchFailure.HTTP_ERROR),
    ],
)
async def test_non_text_and_http_errors(status: int, kind: str, expected: FetchFailure) -> None:
    assert (
        await reader(lambda _: httpx.Response(status, headers={"content-type": kind})).read(
            "https://example.com"
        )
        is expected
    )


@pytest.mark.parametrize("error", [RuntimeError("unexpected"), httpx.ConnectError("offline")])
async def test_unexpected_and_network_errors_never_escape(error: Exception) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise error

    assert await reader(handle).read("https://example.com") is FetchFailure.NETWORK
