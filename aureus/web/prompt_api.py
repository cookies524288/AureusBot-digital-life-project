"""提示词目录、逐项覆盖及无模型调用的预览（D90）。"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from aureus.common import Clock, ConfigError, RuntimeConfig
from aureus.heartbeat import build_prompt_scenes, catalog_template_keys
from aureus.identity import Identity
from aureus.organs import (
    BUILTIN_ORGANS,
    PromptStore,
    PromptTemplateError,
    assemble_messages,
    organ_tasks,
)

from .config_files import ConfigService


class PromptUpdate(BaseModel):
    """一份模板的替换原文。"""

    source: str


def register_prompt_routes(
    app: FastAPI,
    *,
    store: PromptStore,
    files: ConfigService,
    runtime: RuntimeConfig,
    identity: Callable[[], Identity],
    clock: Clock,
    require_ready: Callable[..., Awaitable[str]],
) -> None:
    """提示词接口继承高级配置的会话及密码状态检查。"""
    guard = [Depends(require_ready)]

    async def organs() -> dict[str, set[str]]:
        try:
            _, config = await asyncio.to_thread(files.read_models)
        except ConfigError as error:
            raise HTTPException(422, detail=str(error)) from None
        routes = organ_tasks(runtime, config.configured_text_organs())
        return {
            name: {task for task, assigned in routes.items() if assigned == name}
            for name in dict.fromkeys((*BUILTIN_ORGANS, *config.organs, *routes.values()))
        }

    def view(key: str) -> dict[str, Any]:
        if key not in store.templates:
            raise HTTPException(404, detail="找不到该提示词。")
        template = store.templates[key]
        override = store.snapshot().get(key)
        return {
            "key": key,
            "label": template.label,
            "source": (override or template.default).template,
            "variables": template.variables,
            "customized": override is not None,
            "editable": template.editable,
        }

    @app.get("/api/prompts", dependencies=guard)
    async def list_prompts() -> dict[str, Any]:
        return {
            "organs": {
                name: sorted(catalog_template_keys(name, tasks))
                for name, tasks in (await organs()).items()
            },
            "templates": [view(key) for key in store.templates],
        }

    async def update(key: str, source: str | None) -> dict[str, Any]:
        try:
            await asyncio.to_thread(store.write, key, source)
        except KeyError:
            raise HTTPException(404, detail="找不到该提示词。") from None
        except PromptTemplateError as error:
            raise HTTPException(422, detail=str(error)) from None
        return view(key)

    @app.put("/api/prompts/{key}", dependencies=guard)
    async def save_prompt(key: str, body: PromptUpdate) -> dict[str, Any]:
        return await update(key, body.source)

    @app.delete("/api/prompts/{key}", dependencies=guard)
    async def reset_prompt(key: str) -> dict[str, Any]:
        return await update(key, None)

    @app.get("/api/organs/{organ}/prompt-preview", dependencies=guard)
    async def preview_prompt(organ: str) -> dict[str, Any]:
        registered = await organs()
        if organ not in registered:
            raise HTTPException(404, detail="找不到该器官。")
        scenes = build_prompt_scenes(
            organ,
            registered[organ],
            identity(),
            clock.now(),
            roleplay=runtime.heartbeat.roleplay_mode,
        )
        return {
            "scenes": [
                {
                    "id": scene.key,
                    "label": scene.label,
                    "note": scene.note,
                    "messages": assemble_messages(scene.request, store),
                }
                for scene in scenes
            ]
        }
