"""D111 学习工作台的认证静态入口与离线资源。"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

WORKBENCH_ROOT = Path(__file__).parent / "static" / "learning-workbench"
_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'self'; base-uri 'self'; object-src 'none'"
    ),
}


def register_learning_assets(
    app: FastAPI, require_ready: Callable[..., Awaitable[str]]
) -> None:
    """沿用 Aureus 登录，只提供构建目录内的同源文件。"""

    @app.get("/learning-workbench/{asset:path}", include_in_schema=False)
    async def learning_asset(
        asset: str, _token: Annotated[str, Depends(require_ready)]
    ) -> FileResponse:
        root = WORKBENCH_ROOT.resolve()
        target = (root / (asset or "index.html")).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(404, "未找到学习工作台资源。")
        return FileResponse(target, headers=_HEADERS)
