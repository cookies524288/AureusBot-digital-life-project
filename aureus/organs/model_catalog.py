"""按已保存的服务商协议读取模型目录。只请求模型元数据。"""

import asyncio

import httpx

from aureus.common.config import ConfigError
from aureus.organs.api_keys import KeyResolver
from aureus.organs.model_config import ProviderConfig

DEFAULT_BASE = {
    "openai": "https://api.openai.com/v1",
    "openai_responses": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
}


def _provider_base(provider: ProviderConfig) -> httpx.URL:
    base = httpx.URL(provider.base_url or DEFAULT_BASE[provider.protocol])
    if (
        base.scheme not in {"http", "https"}
        or not base.host
        or base.userinfo
        or base.query
        or base.fragment
    ):
        raise ConfigError("API供应商请求地址必须是 HTTP(S) 接口根地址。")
    return base


async def probe_provider(
    provider: ProviderConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, int]:
    """用不带凭据的 HEAD 请求测到响应头的往返耗时，任何 HTTP 状态都表示可达。"""
    try:
        base = _provider_base(provider)
        async with httpx.AsyncClient(
            transport=transport, timeout=provider.timeout_seconds
        ) as client:
            loop = asyncio.get_running_loop()
            started = loop.time()
            response = await client.head(base)
            return {
                "latency_ms": round((loop.time() - started) * 1000),
                "status": response.status_code,
            }
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        raise ConfigError("未收到供应商响应，请检查地址和网络连接。") from None


async def list_provider_models(
    provider: ProviderConfig,
    resolver: KeyResolver | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    """读取目录并保留服务商顺序；服务商错误只报告状态，不返回原始响应。"""
    protocol = provider.protocol
    headers = {"anthropic-version": "2023-06-01"} if protocol == "anthropic" else {}
    if provider.api_key_ref or provider.api_key_env:
        key = None
        if resolver is not None:
            key = (
                resolver.get_vault(provider.api_key_ref)
                if provider.api_key_ref
                else resolver.get(provider.api_key_env or "")
            )
        if not key:
            raise ConfigError("该API供应商的 API key 尚未配置或已删除。")
        value = key.get_secret_value()
        if protocol == "anthropic":
            headers["x-api-key"] = value
        elif protocol == "gemini":
            headers["x-goog-api-key"] = value
        else:
            headers["Authorization"] = f"Bearer {value}"

    identifiers: dict[str, None] = {}
    params: dict[str, str] = {}
    seen: set[str] = set()
    try:
        base = _provider_base(provider)
        path = base.path.rstrip("/")
        if protocol in {"anthropic", "gemini"} and not path.rsplit("/", 1)[-1].startswith("v1"):
            path += "/v1" if protocol == "anthropic" else "/v1beta"
        url = base.copy_with(path=path + "/models")
        async with httpx.AsyncClient(
            transport=transport, timeout=provider.timeout_seconds
        ) as client:
            while True:
                response = await client.get(url, headers=headers, params=params)
                if not response.is_success:
                    raise ConfigError(f"API供应商模型目录请求失败（HTTP {response.status_code}）。")
                data = response.json()
                rows = (
                    data.get("models" if protocol == "gemini" else "data")
                    if isinstance(data, dict)
                    else None
                )
                if not isinstance(rows, list):
                    raise ConfigError("API供应商返回了无法识别的模型目录。")
                for row in rows:
                    identifier = (
                        row.get("name" if protocol == "gemini" else "id")
                        if isinstance(row, dict)
                        else None
                    )
                    if not isinstance(identifier, str) or not identifier.strip():
                        raise ConfigError("API供应商返回了无效的模型标识符。")
                    identifiers[
                        identifier.removeprefix("models/") if protocol == "gemini" else identifier
                    ] = None
                if protocol == "gemini" and data.get("nextPageToken"):
                    cursor_name, cursor = "pageToken", data["nextPageToken"]
                elif protocol == "anthropic" and data.get("has_more"):
                    cursor_name, cursor = "after_id", data.get("last_id")
                else:
                    return list(identifiers)
                if not isinstance(cursor, str) or not cursor or cursor in seen or len(seen) >= 100:
                    raise ConfigError("API供应商目录分页异常。")
                seen.add(cursor)
                params = {cursor_name: cursor}
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, UnicodeError):
        raise ConfigError("无法读取API供应商模型目录，请检查请求地址、网络和凭据。") from None
