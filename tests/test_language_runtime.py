"""用假器官沿入站、规划、回复和重启路径验证 D89 的习得语言。"""

import json
from pathlib import Path
from typing import TypedDict

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from aureus.common import ManualClock
from aureus.core import runtime
from aureus.core.model_runtime import ModelRuntime
from aureus.heartbeat import MessageRoute, Perception, TrustLevel
from aureus.heartbeat.inbox_store import ConversationKey, InboxStore
from aureus.heartbeat.language_runner import learn_language
from aureus.memory import LanguageKind, LanguageProposal, LanguageSample, LanguageStore
from aureus.memory.language import LanguageContent, LanguagePackageRow
from aureus.organs import CallPurpose, KeyResolver, OrganBus, OrganRequest, OrganResponse
from aureus.timeline import ActorType, TimelineQuery, VisibilityScope
from aureus.web.language_package_api import register_language_package_routes
from aureus.web.resource_api import register_resource_routes
from tests.heartbeat.test_judging_flow import NOW
from tests.prompt_assertions import dialogue_text
from tests.test_exploration_runtime import _paths


class LanguageOrgan:
    """按任务提供合成提议，并保留实际收到的提示。"""

    def __init__(self, organ_id: str, requests: list[OrganRequest]) -> None:
        self.organ_id = organ_id
        self.requests = requests

    async def process(self, request: OrganRequest) -> OrganResponse:
        self.requests.append(request)
        task = request.context["task"]
        if task == "learn_jargon":
            samples = learning_samples(request)
            text = json.dumps(
                [
                    {"content": "GG", "id": sample["id"]}
                    for sample in samples
                    if "gg" in sample["text"].lower()
                ]
            )
        elif task == "learn_style":
            text = json.dumps(
                [
                    {"style": "月兔冲线", "situation": "庆祝跑步成绩", "id": sample["id"]}
                    for sample in learning_samples(request)
                    if "月兔冲线" in sample["text"]
                ],
                ensure_ascii=False,
            )
        elif task == "jargon_inference_with_context":
            meaning = (
                "抵达终点"
                if any("跑步第" in sample["text"] for sample in learning_samples(request))
                else "等候开局"
            )
            text = json.dumps({"meaning": meaning, "no_info": False}, ensure_ascii=False)
        elif task == "jargon_inference_content_only":
            text = '{"meaning":"网络致意"}'
        elif task == "jargon_compare_inference":
            text = '{"is_similar":false,"reason":"含义不同"}'
        elif task == "plan":
            text = '{"reply":true,"intent":"回应消息","expression_intent":{"scene":"庆祝跑步成绩"}}'
            if "陌生主题" in request.prompt:
                text = '{"reply":true,"intent":"问好"}'
        else:
            text = "收到。"
        return OrganResponse(organ_id=self.organ_id, suggestion=text)


class LearningSample(TypedDict):
    """器官看到的单条语言资料。"""

    id: int
    text: str


def learning_samples(request: OrganRequest) -> list[LearningSample]:
    """读取假器官实际看到的编号与文本。"""
    return [json.loads(content) for role, content in request.preceding_messages if role == "user"]


@pytest.mark.asyncio
async def test_language_learning_batches_by_trusted_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    paths.runtime_config_file.write_text(
        "[body]\nsleep_enabled = false\n[judge]\nenabled = false\n"
        "[language]\nlearning_enabled = true\n",
        encoding="utf-8",
    )
    requests: list[OrganRequest] = []
    bus = OrganBus()
    bus.register(LanguageOrgan("summarizer", requests))

    def build_bus(
        _paths: object, _heartbeat: object, resolver: KeyResolver, **_kwargs: object
    ) -> ModelRuntime:
        return ModelRuntime(bus, resolver, fake_organs=False)

    monkeypatch.setattr(runtime, "_build_organ_bus", build_bus)
    clock = ManualClock(NOW)
    with runtime.open_core(paths, clock=clock, to_console=False) as core:
        heart = core.heartbeat
        heart.attach_channel("synthetic_qq", lambda _message: None)
        heart.wake_up()
        inbox = InboxStore(paths.database_file, clock)
        routes = (
            ("账号甲", "group", "10", None),
            ("账号甲", "group", "20", None),
            ("账号甲", "private", "10", None),
            ("账号甲", "private", "10", "20"),
            ("账号甲", "private", "10", "10"),
            ("账号乙", "group", "10", None),
        )
        for index, (account, kind, target, temporary_group) in enumerate(routes * 2):
            text = f"正文声称是群 10 的第 {index} 条"
            sample = Perception(
                "synthetic_qq",
                "10",
                text,
                VisibilityScope.GROUP,
                TrustLevel.UNVERIFIED,
                route=MessageRoute(account, kind, target, str(index), temporary_group),
            )
            stored = inbox.add(sample, sender_name="同昵称", conversation_name="同会话名称")
            assert stored is not None
            heart._submit_inbox([stored])
            if index in (0, 6):
                assert heart.submit(
                    Perception(
                        "synthetic_qq",
                        "10",
                        f"无可信路由 {index}",
                        VisibilityScope.GROUP,
                        TrustLevel.UNVERIFIED,
                        route=sample.route if index == 6 else None,
                    )
                )
        for _ in range(len(routes) + 3):
            heart.beat()
            await heart.wait_for_thought()
        await heart.shut_down()

    expected = [
        [f"正文声称是群 10 的第 {index} 条", f"正文声称是群 10 的第 {index + len(routes)} 条"]
        for index in range(len(routes))
    ]
    assert [[sample["text"] for sample in learning_samples(request)] for request in requests] == [
        samples for samples in expected for _ in range(2)
    ]
    assert [request.context["task"] for request in requests] == [
        "learn_style",
        "learn_jargon",
    ] * len(expected)


@pytest.mark.asyncio
async def test_online_expression_filter_runs_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    identity = paths.identity_file.read_text(encoding="utf-8")
    paths.identity_file.write_text(
        identity.replace("[facts]\n", '[facts]\nalias_names = ["  MoonBot  "]\n'), encoding="utf-8"
    )
    paths.runtime_config_file.write_text(
        "[body]\nsleep_enabled = false\n[judge]\nenabled = false\n"
        "[language]\nlearning_enabled = true\n",
        encoding="utf-8",
    )
    requests: list[OrganRequest] = []

    class FilterOrgan(LanguageOrgan):
        async def process(self, request: OrganRequest) -> OrganResponse:
            if request.context["task"] != "learn_style":
                return await super().process(request)
            requests.append(request)
            proposals = [
                {"style": style, "situation": "庆祝跑步成绩", "id": source_id}
                for style, source_id in (
                    ("测试豆", 1),
                    ("使用MoonBOT", 1),
                    ("使用", 1),
                    ("使用：MoonBot冲线", 1),
                    (" 使用月兔冲线 ", 1),
                    ("月兔冲线", 1),
                    ("不应学入的表达", 2),
                )
            ]
            return OrganResponse(self.organ_id, json.dumps(proposals, ensure_ascii=False))

    bus = OrganBus()
    bus.register(FilterOrgan("summarizer", requests))

    def build_bus(
        _paths: object, _heartbeat: object, resolver: KeyResolver, **_kwargs: object
    ) -> ModelRuntime:
        return ModelRuntime(bus, resolver, fake_organs=False)

    monkeypatch.setattr(runtime, "_build_organ_bus", build_bus)
    clock = ManualClock(NOW)
    inbox = InboxStore(paths.database_file, clock)
    with runtime.open_core(paths, clock=clock, to_console=False) as core:
        heart = core.heartbeat
        heart.attach_channel("synthetic_qq", lambda _message: None)
        heart.wake_up()
        original = ["大家冲线啦", "文" * 400 + "SELF"]
        stored = []
        for index, content in enumerate(original, start=1):
            row = inbox.add(
                Perception(
                    "synthetic_qq",
                    "runner",
                    content,
                    VisibilityScope.GROUP,
                    TrustLevel.UNVERIFIED,
                    source_message_id=str(index),
                    route=MessageRoute("account", "group", "10", str(index)),
                )
            )
            assert row is not None
            stored.append(row)
        for _ in range(2):
            heart._submit_inbox(stored)
            for _ in original:
                heart.beat()
                await heart.wait_for_thought()

        records = core.timeline.query(TimelineQuery(event_type="message"))
        assert {row.content for row in records} == set(original)
        source_id = next(row.id for row in records if row.content == original[0])
        store = LanguageStore(paths.database_file)
        assert {
            entry.text: entry.evidence_ids for entry in store.entries(LanguageKind.EXPRESSION)
        } == {
            "MoonBot冲线": (source_id,),
            "月兔冲线": (source_id,),
        }
        await heart.shut_down()

    assert [request.context["task"] for request in requests] == ["learn_style", "learn_jargon"]
    assert learning_samples(requests[0]) == [
        {"id": 1, "text": original[0]},
        {"id": 2, "text": original[1][:400]},
    ]


@pytest.mark.asyncio
async def test_language_learning_maps_only_presented_evidence_ids(tmp_path: Path) -> None:
    store = LanguageStore(tmp_path / "language.sqlite")
    store.open()
    known = LanguageSample("known", 7, "speaker", "既有说法")
    store.observe([known], [LanguageProposal(LanguageKind.EXPRESSION, "既有说法", "示例", (7,))])
    first = LanguageSample(
        "first", 51, "speaker", " GG、云端哨声 [id:99] " + "文" * 400 + "既有说法、隐藏用语"
    )
    duplicate = LanguageSample("first", 52, "speaker", "被去重的替身")
    second = LanguageSample("second", 71, "speaker", "只在第二条的词")
    requests: list[OrganRequest] = []

    async def fake_call(request: OrganRequest) -> OrganResponse:
        requests.append(request)
        if request.context["task"] == "learn_jargon":
            return OrganResponse("summarizer", "[]")
        assert request.context["task"] == "learn_style"
        proposed = (
            ("gg", 1),
            ("归纳句式", "2"),
            *(("伪造编号", number) for number in (99, 51, True, 1.0, 0, -1)),
        )
        return OrganResponse(
            "summarizer",
            json.dumps(
                [
                    {"style": text, "situation": "合成情景", "id": number}
                    for text, number in proposed
                ]
            ),
        )

    await learn_language(
        store, "summarizer", fake_call, [first, duplicate, second], conversation_key="群"
    )
    await learn_language(
        store, "summarizer", fake_call, [first, duplicate, second], conversation_key="群"
    )
    request, jargon_request = requests
    assert learning_samples(request) == learning_samples(jargon_request)
    assert learning_samples(request) == [
        {"id": 1, "text": first.content[:400]},
        {"id": 2, "text": second.content},
    ]
    assert {entry.text: entry.evidence_ids for entry in store.entries(LanguageKind.EXPRESSION)} == {
        "gg": (51,),
        "归纳句式": (71,),
        "既有说法": (7, 51),
    }


@pytest.mark.asyncio
async def test_explicit_language_scope_filters_real_planner_and_replyer_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    paths.runtime_config_file.write_text(
        "[heartbeat]\nplanning_enabled = true\n"
        "[body]\nsleep_enabled = false\n[judge]\nenabled = false\n"
        "[language]\nlearning_enabled = false\nreference_chars = 1000\n",
        encoding="utf-8",
    )
    requests: list[OrganRequest] = []
    bus = OrganBus()
    for organ in ("planner", "replyer"):
        bus.register(LanguageOrgan(organ, requests))

    def build_bus(
        _paths: object, _heartbeat: object, resolver: KeyResolver, **_kwargs: object
    ) -> ModelRuntime:
        return ModelRuntime(bus, resolver, fake_organs=False)

    def message(kind: str, recipient: str, number: int = 1) -> Perception:
        return Perception(
            "scope_test",
            "local",
            "GG，跑步冲线，打招呼。",
            VisibilityScope.GROUP if kind == "group" else VisibilityScope.USER_ONLY,
            TrustLevel.VERIFIED_USER,
            route=MessageRoute("42", kind, recipient, str(number)),
            mentioned=True,
        )

    async def require_ready() -> str:
        return "synthetic"

    def same_origin(request: Request) -> bool:
        return request.headers.get("Origin") == "http://testserver"

    monkeypatch.setattr(runtime, "_build_organ_bus", build_bus)
    clock = ManualClock(NOW)
    inbox = InboxStore(paths.database_file, clock)
    conversations = message("group", "10"), message("group", "20"), message("private", "10")
    first, second, private = conversations
    keys = [ConversationKey.from_perception(item).language_scope_key() for item in conversations]
    for item, name in zip(conversations, ("合成甲群", "合成乙群", "合成私聊"), strict=True):
        assert inbox.add(item, conversation_name=name) is not None
    store = LanguageStore(paths.database_file)
    store.open()
    store.add_entry(LanguageKind.JARGON, LanguageContent("GG", "旧有释义"))
    entries = [
        (LanguageKind.JARGON, LanguageContent("GG", "甲群异义"), [keys[0]]),
        (LanguageKind.JARGON, LanguageContent("GG", "乙群异义"), [keys[1]]),
        (LanguageKind.EXPRESSION, LanguageContent("甲群招呼", "庆祝跑步成绩"), [keys[0], keys[2]]),
        (LanguageKind.EXPRESSION, LanguageContent("乙群招呼", "庆祝跑步成绩"), [keys[1]]),
        (LanguageKind.JARGON, LanguageContent("GG", "永不映射"), []),
    ]
    app = FastAPI()
    register_resource_routes(app, require_ready, store, None, same_origin=same_origin, inbox=inbox)
    endpoint = "/api/resources/learning/scope"
    headers = {"Origin": "http://testserver"}
    express_key = ""
    with TestClient(app) as client:
        assert {item["key"] for item in client.get(endpoint).json()["items"]} == set(keys)
        for index, (kind, content, targets) in enumerate(entries):
            package = LanguagePackageRow(content, 2, "不可信来源会话", json.dumps({"id": index}))
            assert store.save_package(kind, [package]) == 1
            entry_key = f"package:{package.digest}"
            payload = {"kind": kind, "entry_key": entry_key, "conversation_keys": targets}
            assert client.post(endpoint, json=payload).status_code == 403
            unknown = {**payload, "conversation_keys": [*targets, "未发现会话"]}
            assert client.post(endpoint, headers=headers, json=unknown).status_code == 422
            preview = client.post(endpoint, headers=headers, json=payload).json()
            assert preview["before"] == [] and len(preview["after"]) == len(targets)
            assert not preview["confirmed"]
            assert store.package_scope(kind, entry_key, [], confirm=False) == ()
            if targets:
                confirmed = client.post(
                    endpoint, headers=headers, json={**payload, "confirm": True}
                )
                assert confirmed.json()["confirmed"]
                assert store.package_scope(kind, entry_key, [], confirm=False) == tuple(
                    sorted(targets)
                )
            if index == 2:
                express_key = entry_key

    with runtime.open_core(paths, clock=clock, to_console=False) as core:
        heart = core.heartbeat
        heart.attach_channel("scope_test", lambda _message: None)
        heart.wake_up()
        for target, jargon, expression in (
            (first, "甲群异义", "甲群招呼"),
            (second, "乙群异义", "乙群招呼"),
            (private, None, "甲群招呼"),
        ):
            requests.clear()
            assert target.route is not None
            assert heart.submit(message(target.route.kind, target.route.recipient_id, 2))
            for _ in range(3):
                heart.beat()
                await heart.wait_for_thought()
            plan = next(request for request in requests if request.context["task"] == "plan")
            reply = next(request for request in requests if request.context["task"] == "reply")
            assert "旧有释义" in plan.prompt and "永不映射" not in plan.prompt
            for word in ("甲群异义", "乙群异义"):
                assert (word in plan.prompt) == (word == jargon)
            for word in ("甲群招呼", "乙群招呼"):
                assert (word in dialogue_text(reply)) == (word == expression)
        await heart.shut_down()

    with TestClient(app) as client:
        removed = client.post(
            endpoint,
            headers=headers,
            json={
                "kind": "expression",
                "entry_key": express_key,
                "conversation_keys": [],
                "confirm": True,
            },
        )
        assert removed.json()["after"] == []
    assert not store.expression_reference(
        "跑步", "庆祝跑步成绩", (), "", 1000, conversation_key=keys[0]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["online", "pending", "granted", "revoked"])
async def test_same_language_in_reply_depends_on_explicit_package_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    paths = _paths(tmp_path)
    paths.runtime_config_file.write_text(
        "[heartbeat]\nplanning_enabled = true\n"
        "[body]\nsleep_enabled = false\n[judge]\nenabled = false\n"
        "[language]\nlearning_enabled = false\nreference_chars = 1000\n",
        encoding="utf-8",
    )
    clock = ManualClock(NOW)
    requests: list[OrganRequest] = []
    bus = OrganBus()
    for organ in ("planner", "replyer"):
        bus.register(LanguageOrgan(organ, requests))

    def build_bus(
        _paths: object, _heartbeat: object, resolver: KeyResolver, **_kwargs: object
    ) -> ModelRuntime:
        return ModelRuntime(bus, resolver, fake_organs=False)

    async def require_ready() -> str:
        return "synthetic"

    def same_origin(request: Request) -> bool:
        return request.headers.get("Origin") == "http://testserver"

    monkeypatch.setattr(runtime, "_build_organ_bus", build_bus)
    private = Perception(
        "synthetic_qq",
        "runner",
        "私聊来源",
        VisibilityScope.USER_ONLY,
        TrustLevel.VERIFIED_USER,
        route=MessageRoute("42", "private", "10", "1"),
    )
    group = Perception(
        "synthetic_qq",
        "local",
        "GG，来聊跑步。",
        VisibilityScope.GROUP,
        TrustLevel.VERIFIED_USER,
        route=MessageRoute("99", "group", "20", "2"),
        mentioned=True,
    )
    private_key, group_key = (
        ConversationKey.from_perception(item).language_scope_key() for item in (private, group)
    )
    inbox = InboxStore(paths.database_file, clock)
    assert inbox.add(private, conversation_name="合成私聊") is not None
    assert inbox.add(group, conversation_name="合成群") is not None
    store = LanguageStore(paths.database_file)
    store.open()
    if source == "online":
        samples = [
            LanguageSample(f"private:{number}", number, "runner", "跑步第 1 场：GG，月兔冲线。")
            for number in range(1, 5)
        ]
        await learn_language(
            store,
            "summarizer",
            LanguageOrgan("summarizer", []).process,
            samples,
            conversation_key=private_key,
        )
    else:
        app = FastAPI()
        register_resource_routes(
            app, require_ready, store, None, same_origin=same_origin, inbox=inbox
        )
        register_language_package_routes(app, require_ready, same_origin, store, clock)
        headers = {"Origin": "http://testserver"}
        with TestClient(app) as client:
            for kind, spelling, fields, item in (
                (
                    LanguageKind.JARGON,
                    "jargons",
                    {"include_chat_info": True},
                    {
                        "content": "GG",
                        "meaning": "抵达终点",
                        "count": 4,
                        "is_jargon": True,
                        "is_complete": True,
                        "is_global": True,
                        "targets": [{"platform": "qq", "id": "20", "type": "group", "count": 4}],
                    },
                ),
                (
                    LanguageKind.EXPRESSION,
                    "expressions",
                    {"source_chat_name": "合成私聊"},
                    {
                        "style": "月兔冲线",
                        "situation": "庆祝跑步成绩",
                        "count": 4,
                        "checked": False,
                        "content_list": "[]",
                    },
                ),
            ):
                package = {
                    "type": f"maibot.{kind}.export",
                    "version": 1,
                    "exported_at": NOW.isoformat(),
                    "count": 1,
                    spelling: [item],
                    **fields,
                }
                endpoint = f"/api/resources/learning/package/{kind}"
                preview = client.post(endpoint, headers=headers, json=package)
                assert preview.status_code == 200
                confirmed = client.post(
                    endpoint,
                    headers=headers,
                    json=package,
                    params={"confirmed": preview.json()["digest"]},
                )
                assert confirmed.json() == {"added": 1, "skipped": 0, "can_recall": 0}
                [entry] = store.entries(kind, include_disabled=True)
                scope = "/api/resources/learning/scope"
                payload = {
                    "kind": kind,
                    "entry_key": entry.entry_key,
                    "conversation_keys": [group_key],
                }
                assert client.post(scope, headers=headers, json=payload).json()["before"] == []
                if source in {"granted", "revoked"}:
                    grant = client.post(scope, headers=headers, json={**payload, "confirm": True})
                    assert grant.status_code == 200 and grant.json()["confirmed"]
                    assert store.package_scope(kind, entry.entry_key, [], confirm=False) == (
                        group_key,
                    )
                if source == "revoked":
                    revoked = client.post(
                        scope,
                        headers=headers,
                        json={**payload, "confirm": True, "conversation_keys": []},
                    )
                    assert revoked.status_code == 200 and revoked.json()["confirmed"]
                    assert len(revoked.json()["before"]) == 1
                    assert revoked.json()["after"] == []
                    assert store.package_scope(kind, entry.entry_key, [], confirm=False) == ()

    with runtime.open_core(paths, clock=clock, to_console=False) as core:
        heart = core.heartbeat
        heart.attach_channel("synthetic_qq", lambda _message: None)
        heart.wake_up()
        assert heart.submit(group)
        for _ in range(3):
            heart.beat()
            await heart.wait_for_thought()
        await heart.shut_down()
    plan = next(request for request in requests if request.context["task"] == "plan")
    reply = next(request for request in requests if request.context["task"] == "reply")
    expected = source in {"online", "granted"}
    assert ("GG：抵达终点" in plan.prompt) is expected
    assert ("当「庆祝跑步成绩」时，可用「月兔冲线」" in dialogue_text(reply)) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("learning", "budget"), [(True, 700), (False, 700), (True, 0)])
async def test_language_normal_inbound_across_group_and_private_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, learning: bool, budget: int
) -> None:
    paths = _paths(tmp_path)
    paths.runtime_config_file.write_text(
        "[heartbeat]\nplanning_enabled = true\ninbox_debounce_enabled = false\n"
        "[body]\nsleep_enabled = false\n[judge]\nenabled = false\n"
        "[language]\n"
        f"learning_enabled = {str(learning).lower()}\nreference_chars = {budget}\n",
        encoding="utf-8",
    )
    requests: list[OrganRequest] = []
    bus = OrganBus()
    for organ in ("planner", "replyer", "summarizer"):
        bus.register(LanguageOrgan(organ, requests))

    def build_bus(
        _paths: object, _heartbeat: object, resolver: KeyResolver, **_kwargs: object
    ) -> ModelRuntime:
        return ModelRuntime(bus, resolver, fake_organs=False)

    monkeypatch.setattr(runtime, "_build_organ_bus", build_bus)
    store = LanguageStore(paths.database_file)
    samples = [
        Perception(
            channel="mock_platform",
            sender_id="runner",
            content=(
                f"跑步第 {index} 场：GG，月兔冲线。"
                if index < 4
                else f"集合第 {index} 场：gg，等开局。"
            ),
            visibility=VisibilityScope.GROUP,
            trust=TrustLevel.UNVERIFIED,
            source_message_id=f"sample-{index}",
            route=MessageRoute("account", "group", "10" if index < 4 else "20", str(index)),
        )
        for index in range(8)
    ]

    clock = ManualClock(NOW)
    inbox = InboxStore(paths.database_file, clock)
    stored_samples = [inbox.add(sample) for sample in samples]
    assert all(stored_samples)
    with runtime.open_core(paths, clock=clock, to_console=False) as core:
        heart = core.heartbeat
        heart.attach_channel("mock_platform", lambda _message: None)
        heart.wake_up()
        core.timeline.append("message", "历史回放，无入站", ActorType.USER)
        heart._submit_inbox(
            [sample for sample in [*stored_samples, stored_samples[0]] if sample is not None]
        )
        for _ in range(len(samples) + 1):
            heart.beat()
            await heart.wait_for_thought()
        entries = store.entries(LanguageKind.EXPRESSION)
        assert [(entry.text, entry.count) for entry in entries] == (
            [("月兔冲线", 4)] if learning else []
        )
        jargon = store.entries(LanguageKind.JARGON)
        assert [(entry.detail, entry.count) for entry in jargon] == (
            [("抵达终点", 4), ("等候开局", 4)] if learning else []
        )
        await heart.shut_down()

    if learning:
        assert [
            learning_samples(request)
            for request in requests
            if request.context["task"] == "learn_style"
        ] == [
            [{"id": number, "text": sample.content} for number, sample in enumerate(batch, start=1)]
            for batch in (samples[:4], samples[4:])
        ]
        assert all(request.purpose is CallPurpose.OPTIONAL for request in requests)
    else:
        assert not requests
    requests.clear()
    with runtime.open_core(paths, clock=clock, to_console=False) as core:
        heart = core.heartbeat
        heart.attach_channel("mock_platform", lambda _message: None)
        heart.wake_up()
        heart._submit_inbox([sample for sample in stored_samples if sample is not None])
        for _ in samples:
            heart.beat()
            await heart.wait_for_thought()
        assert not requests
        assert store.entries(LanguageKind.EXPRESSION) == entries
        assert store.entries(LanguageKind.JARGON) == jargon
        if learning:
            mentioned = set(jargon[0].evidence_ids + jargon[1].evidence_ids)
            evidence = core.timeline.query(TimelineQuery(event_type="message"))
            assert {
                row.metadata["source_message_id"] for row in evidence if row.id in mentioned
            } == {sample.source_message_id for sample in samples}

        def submit(kind: str, recipient: str, text: str, number: str) -> None:
            message = Perception(
                "mock_platform",
                "local",
                text,
                VisibilityScope.USER_ONLY if kind == "private" else VisibilityScope.GROUP,
                TrustLevel.VERIFIED_USER,
                route=MessageRoute("account", kind, recipient, number),
                mentioned=True,
            )
            stored = inbox.add(message)
            assert stored is not None
            heart._submit_inbox([stored])

        for kind, recipient, number in (
            ("group", "10", "101"),
            ("group", "20", "102"),
            ("private", "10", "103"),
        ):
            requests.clear()
            submit(kind, recipient, "gg，来聊跑步。", number)
            for _ in range(3):
                heart.beat()
                await heart.wait_for_thought()
            plan = next(request for request in requests if request.context["task"] == "plan")
            reply = next(request for request in requests if request.context["task"] == "reply")
            injected = learning and budget > 0
            assert ("GG：抵达终点" in plan.prompt) is injected
            assert ("GG：等候开局" in plan.prompt) is injected
            assert ("当「庆祝跑步成绩」时，可用「月兔冲线」" in dialogue_text(reply)) is (injected)
        submit("group", "10", "陌生主题。", "104")
        requests.clear()
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        await heart.wait_for_thought()
        heart.beat()
        assert not any("习得表达（" in dialogue_text(request) for request in requests)
        await heart.shut_down()
