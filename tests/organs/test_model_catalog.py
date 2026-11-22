"""厂商目录只读请求、鉴权、分页与脱敏。"""

from pathlib import Path

import httpx
import pytest

from aureus.common import ConfigError
from aureus.organs.api_keys import KeyResolver
from aureus.organs.model_catalog import list_provider_models
from aureus.organs.model_config import ProviderConfig, ProviderProtocol


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("protocol", "base", "url", "header", "auth"),
    [
        ("openai", None, "https://api.openai.com/v1/models", "Authorization", "Bearer fake"),
        (
            "openai_responses",
            "https://proxy.test/api/v2/",
            "https://proxy.test/api/v2/models",
            "Authorization",
            "Bearer fake",
        ),
        ("anthropic", "https://proxy.test", "https://proxy.test/v1/models", "x-api-key", "fake"),
        (
            "gemini",
            "https://proxy.test/v1beta/",
            "https://proxy.test/v1beta/models",
            "x-goog-api-key",
            "fake",
        ),
    ],
)
async def test_catalog_reads_authenticated_pages(
    protocol: ProviderProtocol, base: str | None, url: str, header: str, auth: str, tmp_path: Path
) -> None:
    keys = KeyResolver(tmp_path / "absent", {"AUREUS_KEY_TEST": "fake"})
    provider = ProviderConfig(
        name="p", protocol=protocol, base_url=base, api_key_env="AUREUS_KEY_TEST"
    )
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and str(request.url).split("?")[0] == url
        assert request.headers[header] == auth
        calls.append(dict(request.url.params))
        if protocol == "gemini":
            if len(calls) == 1:
                return httpx.Response(
                    200, json={"models": [{"name": "models/one"}], "nextPageToken": "2"}
                )
            assert calls[-1] == {"pageToken": "2"}
            return httpx.Response(200, json={"models": [{"name": "models/two"}]})
        if protocol == "anthropic":
            assert request.headers["anthropic-version"]
            if len(calls) == 1:
                return httpx.Response(
                    200, json={"data": [{"id": "one"}], "has_more": True, "last_id": "one"}
                )
            assert calls[-1] == {"after_id": "one"}
        return httpx.Response(200, json={"data": [{"id": "one"}, {"id": "two"}]})

    assert await list_provider_models(provider, keys, transport=httpx.MockTransport(respond)) == [
        "one",
        "two",
    ]
    assert calls[0] == {}


@pytest.mark.asyncio
async def test_catalog_rejects_missing_credentials_before_request() -> None:
    provider = ProviderConfig(name="p", protocol="openai", api_key_env="AUREUS_KEY_MISSING")
    with pytest.raises(ConfigError, match="API key"):
        await list_provider_models(
            provider,
            None,
            transport=httpx.MockTransport(lambda _: pytest.fail("unexpected request")),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 401, 500, 200])
async def test_catalog_errors_hide_response_body(status: int) -> None:
    provider = ProviderConfig(name="p", protocol="openai")
    transport = httpx.MockTransport(lambda _: httpx.Response(status, text="private response"))
    with pytest.raises(ConfigError) as error:
        await list_provider_models(provider, None, transport=transport)
    assert "private response" not in str(error.value)
