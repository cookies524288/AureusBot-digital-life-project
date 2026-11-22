"""网页配置接口：中文错误、密码确认留在主线程，文件读写交给线程（D84）。"""

import asyncio
import math
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from tomlkit.exceptions import TOMLKitError

from aureus.common import ConfigError
from aureus.common.vault import ENTRY_ID_PATTERN, MAX_SECRET_CHARS, VaultError
from aureus.core.model_runtime import ModelRuntime
from aureus.organs import API_KEY_ENV_PATTERN, BUILTIN_ORGANS, validate_models_config
from aureus.organs.model_catalog import list_provider_models

from .auth import LoginLockedError, LoginRejectedError, WebAuth, is_loopback
from .config_files import (
    PASSWORD_SECTIONS,
    READONLY_CONFIG,
    ConfigService,
    models_require_password,
    runtime_field_names,
)

# 无实测依据：远超常见密钥长度，只为挡住超大的录入请求（D84）。
MAX_KEY_CHARS = MAX_SECRET_CHARS
LOCAL_KEYS_ONLY = "密钥只能在本机的网页上录入。其他设备请到本机的网页，或用 aureus keys set。"


@contextmanager
def _translate_errors(service: ConfigService) -> Iterator[None]:
    try:
        yield
    except LoginLockedError as error:
        seconds = max(1, math.ceil(error.retry_after.total_seconds()))
        raise HTTPException(429, detail=str(error), headers={"Retry-After": str(seconds)}) from None
    except LoginRejectedError:
        raise HTTPException(403, detail="密码不对。") from None
    except ConfigError as error:
        raise HTTPException(422, detail=str(error)) from None
    except VaultError as error:
        raise HTTPException(422, detail=service.describe_vault_error(error)) from None
    except (UnicodeDecodeError, TOMLKitError):
        raise HTTPException(422, detail="配置文件不是合法的 TOML。") from None
    except OSError:
        raise HTTPException(500, detail="写不进配置文件。") from None


async def _body(request: Request) -> dict[str, Any]:
    # 不用会把输入写进 422 的请求模型，特别是新录入的密钥还不在日志过滤器里。
    try:
        raw = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(422, detail="请求内容不是合法的 JSON。") from None
    if not isinstance(raw, dict):
        raise HTTPException(422, detail="请求内容必须是对象。")
    return raw


def _confirm(auth: WebAuth, body: dict[str, Any], request: Request) -> None:
    password = body.get("password")
    auth.confirm_password(
        password if isinstance(password, str) else "", request.client.host if request.client else ""
    )


def _require_local(request: Request) -> None:
    if not is_loopback(request.client.host if request.client else ""):
        # 必须在确认密码之前，局域网来源不能借密钥接口尝试密码或耗尽限流。
        raise HTTPException(403, detail=LOCAL_KEYS_ONLY)


def _key_name(body: dict[str, Any]) -> str:
    name = body.get("name")
    if not isinstance(name, str) or not re.fullmatch(API_KEY_ENV_PATTERN, name):
        raise HTTPException(
            422, detail="密钥名要以 AUREUS_KEY_ 开头，只能用大写字母、数字和下划线。"
        )
    return name


def _entry_id(body: dict[str, Any]) -> str:
    value = body.get("id")
    if not isinstance(value, str) or not re.fullmatch(ENTRY_ID_PATTERN, value):
        raise HTTPException(422, detail="密钥引用格式不对。")
    return value


def _runtime_changes(body: dict[str, Any]) -> dict[str, Any]:
    changes = body.get("changes")
    if not isinstance(changes, dict):
        raise HTTPException(422, detail="提交的配置改动必须是对象。")
    for value in changes.values():
        fields = value.values() if isinstance(value, dict) else [value]
        if any(not isinstance(item, (bool, int, float, str)) for item in fields):
            raise HTTPException(422, detail="配置的值只能是开关、数字或文字。")
    return changes


def register_config_routes(
    app: FastAPI,
    service: ConfigService,
    auth: WebAuth,
    require_ready: Callable[..., Awaitable[str]],
    model_runtime: ModelRuntime | None = None,
) -> None:
    """注册配置接口；统一要求已登录且已修改默认密码。"""
    if model_runtime is not None:
        service.key_resolver = model_runtime.key_resolver

    @app.get("/api/models")
    async def view_models(_token: Annotated[str, Depends(require_ready)]) -> dict[str, Any]:
        with _translate_errors(service):
            return await asyncio.to_thread(service.view_models)

    @app.post("/api/models/catalog")
    async def catalog(
        request: Request, _token: Annotated[str, Depends(require_ready)]
    ) -> dict[str, list[str]]:
        with _translate_errors(service):
            name = (await _body(request)).get("provider")
            _, current = await asyncio.to_thread(service.read_models)
            provider = next((item for item in current.providers if item.name == name), None)
            if provider is None:
                raise HTTPException(404, detail="请先保存厂商配置。")
            return {"models": await list_provider_models(provider, service.key_resolver)}

    @app.put("/api/models")
    async def edit_models(
        request: Request, _token: Annotated[str, Depends(require_ready)]
    ) -> dict[str, Any]:
        with _translate_errors(service):
            body = await _body(request)
            raw = body.get("config")
            if not isinstance(raw, dict):
                raise HTTPException(422, detail="提交的模型配置必须是对象。")
            submitted = validate_models_config(raw, "提交的模型配置")
            async with service.write_lock:
                _, current = await asyncio.to_thread(service.read_models)
                submitted = submitted.model_copy(update={"config_version": current.config_version})
                removed = set(current.organs) - set(submitted.organs)
                if removed.intersection(BUILTIN_ORGANS):
                    raise HTTPException(422, detail="默认器官不可删除，可以清空它使用的模型。")
                if models_require_password(current, submitted):
                    _confirm(auth, body, request)
                if current != submitted or list(current.organs) != list(submitted.organs):
                    prepared = (
                        await asyncio.to_thread(model_runtime.prepare, submitted)
                        if model_runtime is not None
                        else None
                    )
                    await asyncio.to_thread(service.write_models, submitted)
                    if model_runtime is not None and prepared is not None:
                        model_runtime.apply(prepared)
            return {
                "ok": True,
                "restart_required": service.restart_required,
                "warnings": submitted.warnings(),
            }

    @app.get("/api/keys")
    async def view_keys(_token: Annotated[str, Depends(require_ready)]) -> dict[str, Any]:
        with _translate_errors(service):
            return await asyncio.to_thread(service.view_keys)

    @app.put("/api/keys")
    async def edit_key(
        request: Request, _token: Annotated[str, Depends(require_ready)]
    ) -> dict[str, bool]:
        _require_local(request)
        with _translate_errors(service):
            body = await _body(request)
            _confirm(auth, body, request)
            name = _key_name(body)
            value = body.get("value")
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value.strip()) > MAX_KEY_CHARS
            ):
                raise HTTPException(422, detail=f"密钥不能为空，且最多 {MAX_KEY_CHARS} 个字。")
            async with service.write_lock:
                await asyncio.to_thread(service.write_key, name, value.strip())
            return {"ok": True, "restart_required": service.restart_required}

    @app.post("/api/keys")
    async def create_key(
        request: Request, _token: Annotated[str, Depends(require_ready)]
    ) -> dict[str, Any]:
        _require_local(request)
        with _translate_errors(service):
            body = await _body(request)
            _confirm(auth, body, request)
            name, kind, value = body.get("name"), body.get("kind"), body.get("value")
            if not isinstance(name, str) or not isinstance(kind, str):
                raise HTTPException(422, detail="请填写密钥名称并选择类型。")
            if not isinstance(value, str) or not value or len(value) > MAX_KEY_CHARS:
                raise HTTPException(422, detail=f"密钥不能为空，且最多 {MAX_KEY_CHARS} 个字。")
            async with service.write_lock:
                entry_id = await asyncio.to_thread(service.create_key, name, kind, value)
            return {"ok": True, "id": entry_id, "restart_required": service.restart_required}

    @app.post("/api/keys/rename")
    async def rename_key(
        request: Request, _token: Annotated[str, Depends(require_ready)]
    ) -> dict[str, bool]:
        _require_local(request)
        with _translate_errors(service):
            body = await _body(request)
            _confirm(auth, body, request)
            entry_id, name = _entry_id(body), body.get("name")
            if not isinstance(name, str):
                raise HTTPException(422, detail="请填写密钥名称。")
            async with service.write_lock:
                renamed = await asyncio.to_thread(service.rename_key, entry_id, name)
            if not renamed:
                raise HTTPException(404, detail="没有这个密钥。")
            return {"ok": True, "restart_required": service.restart_required}

    @app.get("/api/config")
    async def view_config(_token: Annotated[str, Depends(require_ready)]) -> dict[str, Any]:
        with _translate_errors(service):
            return await asyncio.to_thread(service.view_runtime)

    @app.put("/api/config")
    async def edit_config(
        request: Request, _token: Annotated[str, Depends(require_ready)]
    ) -> dict[str, Any]:
        with _translate_errors(service):
            body = await _body(request)
            changes = _runtime_changes(body)
            async with service.write_lock:
                changed = await asyncio.to_thread(service.changed_runtime, changes)
                names = runtime_field_names(changed)
                if any(name in READONLY_CONFIG for name in names):
                    raise HTTPException(
                        403,
                        detail="这一项只能在本机直接编辑 aureus.toml：改错了会让你从网页上进不来。",
                    )
                if any(name.split(".")[0] in PASSWORD_SECTIONS for name in names):
                    _confirm(auth, body, request)
                if changed:
                    await asyncio.to_thread(service.write_runtime, changed)
            return {"ok": True, "changed": names, "restart_required": service.restart_required}

    @app.post("/api/keys/remove")
    async def remove_key(
        request: Request, _token: Annotated[str, Depends(require_ready)]
    ) -> dict[str, bool]:
        _require_local(request)
        with _translate_errors(service):
            body = await _body(request)
            _confirm(auth, body, request)
            by_id = "id" in body
            name = _entry_id(body) if by_id else _key_name(body)
            async with service.write_lock:
                removed = await asyncio.to_thread(service.remove_key, name, by_id=by_id)
            if not removed:
                raise HTTPException(404, detail="没有这个密钥。")
            return {"ok": True, "restart_required": service.restart_required}
