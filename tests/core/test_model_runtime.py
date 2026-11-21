"""模型热切换：用假完成函数验证后续调用、在途调用和无模型时的安全行为。"""

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from aureus.common import AureusPaths, ConfigError, ManualClock
from aureus.core.model_runtime import ModelRuntime
from aureus.core.runtime import open_core
from aureus.heartbeat.exploration import ExplorationState
from aureus.organs import (
    KeyResolver,
    LLMOrgan,
    ModelsConfig,
    OrganBus,
    OrganCallError,
    OrganRequest,
)
from tests.core.test_runtime import IDENTITY, START
from tests.organs.test_llm_adapter import FakeCompletion, make_response


def config_for(identifier: str, references: list[str] | None = None) -> ModelsConfig:
    """创建只指向本地假服务商的完整模型配置。"""
    return ModelsConfig.model_validate(
        {
            "providers": [{"name": "local", "protocol": "openai", "max_retry": 0}],
            "models": [{"name": "chosen", "identifier": identifier, "provider": "local"}],
            "organs": {"replyer": {"models": ["chosen"] if references is None else references}},
        }
    )


@pytest.mark.asyncio
async def test_preparing_does_not_apply_and_switching_keeps_inflight_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def complete(**kwargs: Any) -> Any:
        model = kwargs["model"]
        calls.append(model)
        if model == "openai/old":
            started.set()
            await release.wait()
        return make_response(model)

    monkeypatch.setattr("aureus.organs.llm_adapter._load_litellm_completion", lambda: complete)
    monkeypatch.setattr("aureus.organs.llm_adapter.count_prompt_tokens", lambda _: 4)
    bus = OrganBus()
    runtime = ModelRuntime(bus, KeyResolver(tmp_path / "absent.env", environ={}), fake_organs=False)
    original = runtime.prepare(config_for("old"))
    assert bus.list_organs() == []
    runtime.apply(original)
    request = OrganRequest(organ_id="replyer", prompt="测试")
    inflight = asyncio.create_task(bus.send(request))
    await started.wait()
    prepared = runtime.prepare(config_for("new"))
    assert calls == ["openai/old"]
    runtime.apply(prepared)
    assert (await bus.send(request)).suggestion == "openai/new"
    release.set()
    assert (await inflight).suggestion == "openai/old"
    assert runtime.bus is bus
    assert runtime.available_organs == frozenset({"replyer"})


@pytest.mark.asyncio
@pytest.mark.parametrize("references", [[], ["gone"]])
async def test_unconfigured_routes_never_load_or_call_a_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, references: list[str]
) -> None:
    def reject_loading() -> None:
        raise AssertionError("不应加载真实调用函数")

    monkeypatch.setattr("aureus.organs.llm_adapter._load_litellm_completion", reject_loading)
    runtime = ModelRuntime(
        OrganBus(), KeyResolver(tmp_path / "absent.env", environ={}), fake_organs=False
    )
    updates: list[frozenset[str]] = []
    runtime.on_reload = updates.append
    runtime.apply(runtime.prepare(config_for("unused", references)))
    with pytest.raises(OrganCallError) as error:
        await runtime.bus.send(OrganRequest(organ_id="replyer", prompt="测试"))
    assert error.value.attempts == []
    assert updates == [frozenset()]


@pytest.mark.asyncio
async def test_fake_mode_stays_fake_after_reload(tmp_path: Path) -> None:
    runtime = ModelRuntime(
        OrganBus(), KeyResolver(tmp_path / "absent.env", environ={}), fake_organs=True
    )
    runtime.apply(runtime.prepare(config_for("not-a-real-model")))
    response = await runtime.bus.send(OrganRequest(organ_id="replyer", prompt="测试"))
    assert response.metadata["type"] == "fake"


def test_partial_preparation_failure_preserves_organs_and_availability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "aureus.organs.llm_adapter._load_litellm_completion", lambda: FakeCompletion()
    )
    runtime = ModelRuntime(
        OrganBus(), KeyResolver(tmp_path / "absent.env", environ={}), fake_organs=False
    )
    original = runtime.prepare(config_for("old"))
    runtime.apply(original)
    updates: list[frozenset[str]] = []
    runtime.on_reload = updates.append
    constructed: list[str] = []

    def construct(name: str, *args: Any) -> LLMOrgan:
        constructed.append(name)
        if name == "broken":
            raise ConfigError("新器官构造失败")
        return LLMOrgan(name, *args)

    monkeypatch.setattr("aureus.core.model_runtime.LLMOrgan", construct)
    raw = config_for("new").model_dump(mode="json")
    raw["organs"]["broken"] = {"models": ["chosen"]}
    with pytest.raises(ConfigError, match="新器官构造失败"):
        runtime.prepare(ModelsConfig.model_validate(raw))
    assert constructed == ["replyer", "broken"]
    assert runtime.bus.list_organs() == ["replyer"]
    assert runtime.bus._organs["replyer"] is original.organs[0]
    assert runtime.available_organs == original.available
    assert updates == []


@pytest.mark.asyncio
async def test_removing_an_organ_keeps_its_inflight_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def complete(**kwargs: Any) -> Any:
        started.set()
        await release.wait()
        return make_response(kwargs["model"])

    monkeypatch.setattr("aureus.organs.llm_adapter._load_litellm_completion", lambda: complete)
    runtime = ModelRuntime(
        OrganBus(), KeyResolver(tmp_path / "absent.env", environ={}), fake_organs=False
    )
    runtime.apply(runtime.prepare(config_for("old")))
    request = OrganRequest(organ_id="replyer", prompt="测试")
    inflight = asyncio.create_task(runtime.bus.send(request))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        runtime.apply(runtime.prepare(ModelsConfig()))
        with pytest.raises(ValueError, match="未注册"):
            await runtime.bus.send(request)
    finally:
        release.set()
        response = await asyncio.wait_for(inflight, timeout=5)
    assert response.suggestion == "openai/old"
    assert runtime.available_organs == frozenset()


@pytest.mark.asyncio
async def test_inflight_fallback_uses_the_original_routes_after_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def complete(**kwargs: Any) -> Any:
        model = kwargs["model"]
        calls.append(model)
        if model == "openai/old":
            started.set()
            await release.wait()
            raise RuntimeError("仅供测试的模型失败")
        return make_response(model)

    monkeypatch.setattr("aureus.organs.llm_adapter._load_litellm_completion", lambda: complete)
    monkeypatch.setattr("aureus.organs.llm_adapter.count_prompt_tokens", lambda _: 4)
    runtime = ModelRuntime(
        OrganBus(), KeyResolver(tmp_path / "absent.env", environ={}), fake_organs=False
    )
    raw = config_for("old").model_dump(mode="json")
    raw["models"].append({"name": "fallback", "identifier": "old-fallback", "provider": "local"})
    raw["organs"]["replyer"]["models"].append("fallback")
    runtime.apply(runtime.prepare(ModelsConfig.model_validate(raw)))
    request = OrganRequest(organ_id="replyer", prompt="测试")
    inflight = asyncio.create_task(runtime.bus.send(request))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        runtime.apply(runtime.prepare(config_for("new")))
        assert (await runtime.bus.send(request)).suggestion == "openai/new"
    finally:
        release.set()
        response = await asyncio.wait_for(inflight, timeout=5)
    assert response.suggestion == "openai/old-fallback"
    assert calls == ["openai/old", "openai/new", "openai/old-fallback"]
    assert [attempt.model for attempt in response.attempts] == ["chosen", "fallback"]


@pytest.mark.asyncio
@pytest.mark.parametrize("references", [["chosen"], [], ["gone"]])
async def test_fake_reload_never_loads_the_real_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, references: list[str]
) -> None:
    def reject_loading() -> None:
        raise AssertionError("假器官模式不应加载真实模型")

    monkeypatch.setattr("aureus.organs.llm_adapter._load_litellm_completion", reject_loading)
    runtime = ModelRuntime(
        OrganBus(), KeyResolver(tmp_path / "absent.env", environ={}), fake_organs=True
    )
    for identifier in ("old", "new"):
        runtime.apply(runtime.prepare(config_for(identifier, references)))
        response = await runtime.bus.send(OrganRequest(organ_id="replyer", prompt="测试"))
        assert response.metadata["type"] == "fake"
        assert response.attempts == []


@pytest.mark.asyncio
async def test_loading_a_completion_does_not_run_on_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded_threads: list[int] = []
    request_thread = threading.get_ident()

    async def complete(**kwargs: Any) -> Any:
        return make_response(kwargs["model"])

    def load_completion() -> Any:
        loaded_threads.append(threading.get_ident())
        return complete

    monkeypatch.setattr("aureus.organs.llm_adapter._load_litellm_completion", load_completion)
    runtime = ModelRuntime(
        OrganBus(), KeyResolver(tmp_path / "absent.env", environ={}), fake_organs=False
    )
    runtime.apply(await asyncio.to_thread(runtime.prepare, config_for("new")))
    response = await runtime.bus.send(OrganRequest(organ_id="replyer", prompt="测试"))
    assert response.suggestion == "openai/new"
    assert loaded_threads
    assert request_thread not in loaded_threads


def test_availability_reload_keeps_tracker_execution_records_and_core_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = AureusPaths(tmp_path / "home")
    paths.ensure_directories()
    paths.identity_file.write_text(IDENTITY, encoding="utf-8")
    paths.runtime_config_file.write_text(
        '[diary]\norgan = "diary-summary"\n'
        '[persona_growth]\norgan = "persona-summary"\n'
        '[exploration]\norgan = "exploration-summary"\n',
        encoding="utf-8",
    )
    resolver = KeyResolver(paths.env_file, environ={})
    monkeypatch.setattr("aureus.core.runtime.KeyResolver", lambda *args, **kwargs: resolver)
    with open_core(paths, fake_organs=True, clock=ManualClock(START), to_console=False) as core:
        runtime = core.models
        assert runtime is not None
        heartbeat = core.heartbeat
        diary, persona, exploration = (
            heartbeat._diary,
            heartbeat._persona_growth,
            heartbeat._exploration,
        )
        assert diary is not None and persona is not None
        day = diary.calendar.day_of(START)
        diary._attempted_day = day
        persona.attempted_day = day
        persona._material_ids = frozenset({7, 11})
        exploration.state = ExplorationState(explorations_today=2, last_started_at=START)
        exploration.last_explored[7] = START
        exploration._day = day
        exploration.failed = exploration._restored = True
        state = exploration.state
        bus, gateway, ledger, permissions = (
            runtime.bus,
            core.gateway,
            core.ledger,
            core.gateway._permissions,
        )
        names = {diary.config.organ, persona.config.organ, exploration.config.organ}
        for available in (
            {diary.config.organ},
            {persona.config.organ},
            {exploration.config.organ},
            names,
            set(),
            names,
        ):
            raw = config_for("new").model_dump(mode="json")
            for name in names:
                raw["organs"][name] = {"models": ["chosen"] if name in available else []}
            runtime.apply(runtime.prepare(ModelsConfig.model_validate(raw)))
            assert heartbeat._diary is diary and heartbeat._persona_growth is persona
            assert heartbeat._exploration is exploration
            for tracker in (diary, persona, exploration):
                assert tracker.has_organ is (tracker.config.organ in available)
            assert diary.attempted_day == persona.attempted_day == day
            assert persona._material_ids == frozenset({7, 11})
            assert exploration.state is state
            assert exploration.state == ExplorationState(2, START)
            assert exploration.last_explored == {7: START}
            assert exploration._day == day
            assert exploration.failed is exploration._restored is True
            assert runtime.bus is bus and core.gateway is gateway and core.ledger is ledger
            assert core.gateway._permissions is permissions
