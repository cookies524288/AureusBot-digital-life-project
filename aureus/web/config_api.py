"""网页配置接口：中文错误、口令确认留在主线程，文件读写交给线程（D84）。"""

import asyncio
import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from tomlkit.exceptions import TOMLKitError

from aureus.common import ConfigError
from aureus.organs import validate_models_config

from .auth import LoginLockedError, LoginRejectedError, WebAuth
from .config_files import ConfigService, models_require_password


@contextmanager
def _translate_errors() -> Iterator[None]:
    try:
        yield
    except LoginLockedError as error:
        seconds = max(1, math.ceil(error.retry_after.total_seconds()))
        raise HTTPException(429, detail=str(error), headers={"Retry-After": str(seconds)}) from None
    except LoginRejectedError:
        raise HTTPException(403, detail="口令不对。") from None
    except ConfigError as error:
        raise HTTPException(422, detail=str(error)) from None
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


def register_config_routes(
    app: FastAPI, service: ConfigService, auth: WebAuth, require_ready: Callable[..., str]
) -> None:
    """注册配置接口；统一要求已登录且已修改默认口令。"""

    @app.get("/api/models")
    async def view_models(_token: Annotated[str, Depends(require_ready)]) -> dict[str, Any]:
        with _translate_errors():
            return await asyncio.to_thread(service.view_models)

    @app.put("/api/models")
    async def edit_models(
        request: Request, _token: Annotated[str, Depends(require_ready)]
    ) -> dict[str, bool]:
        with _translate_errors():
            body = await _body(request)
            raw = body.get("config")
            if not isinstance(raw, dict):
                raise HTTPException(422, detail="提交的模型配置必须是对象。")
            submitted = validate_models_config(raw, "提交的模型配置")
            async with service.write_lock:
                _, current = await asyncio.to_thread(service.read_models)
                submitted = submitted.model_copy(update={"config_version": current.config_version})
                if models_require_password(current, submitted):
                    _confirm(auth, body, request)
                if current != submitted or list(current.organs) != list(submitted.organs):
                    await asyncio.to_thread(service.write_models, submitted)
            return {"ok": True, "restart_required": service.restart_required}
