"""使用合成数据库与模型路线检查只读统计，不发出模型请求。"""

import asyncio
import json
import threading
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aureus.channels import WebChannel
from aureus.common import HeartbeatConfig, ManualClock, WebConfig
from aureus.common.sqlite import open_connection
from aureus.core.embedding_progress import EmbeddingProgress
from aureus.core.model_runtime import ModelRuntime
from aureus.heartbeat import MediaReference, MessageRoute, Perception, TrustLevel, VisibilityScope
from aureus.heartbeat.emoji_selection import EmojiSelector
from aureus.heartbeat.inbox_images import InboxImageIndex
from aureus.heartbeat.inbox_store import ConversationKey, InboxStore
from aureus.heartbeat.planning import EmojiIntent
from aureus.memory import (
    EmojiStore,
    ExpressionVectorIndex,
    LanguageContent,
    LanguageKind,
    LanguageStore,
)
from aureus.memory.language import LanguagePackageRow
from aureus.memory.language_matching import ExpressionReviewStatus
from aureus.organs import (
    KeyResolver,
    ModelPreference,
    ModelsConfig,
    OrganBus,
    OrganRequest,
    OrganResponse,
)
from aureus.web import create_app
from tests.heartbeat.test_emoji_selection import synthetic_emoji
from tests.web.config_rig import ConfigRig
from tests.web.test_page_script import run_page_script
from tests.web.test_timeline_api import GOOD, NOW


def _runtime(path: Path) -> ModelRuntime:
    runtime = ModelRuntime(
        OrganBus(), KeyResolver(path / "absent.env", environ={}), fake_organs=False
    )
    _switch(runtime, "first")
    return runtime


def _switch(runtime: ModelRuntime, identifier: str) -> None:
    provider = "test-private-provider"
    config = ModelsConfig.model_validate(
        {
            "providers": [
                {"name": provider, "protocol": "openai", "base_url": "http://localhost:1234/v1"}
            ],
            "models": [
                {"name": name, "identifier": model, "kind": kind, "provider": provider}
                for name, model, kind in (
                    ("local-text", identifier, "embedding"),
                    ("image-primary", identifier, "image_embedding"),
                    ("image-fallback", "fallback", "image_embedding"),
                )
            ],
            "organs": {
                "text_embedding": {"models": ["local-text"]},
                "image_embedding": {"models": ["image-primary", "image-fallback"]},
            },
        }
    )
    runtime.apply(runtime.prepare(config))


async def _never_call(_request: OrganRequest) -> OrganResponse:
    raise AssertionError("统计不应调用器官")


def _save_image(
    inbox: InboxStore, group: str, message: str, visibility: VisibilityScope, *references: str
) -> int:
    record = inbox.add(
        Perception(
            "qq",
            "8",
            "不应出现在统计响应里的消息",
            visibility,
            TrustLevel.VERIFIED_CONTACT,
            route=MessageRoute("9", "group", group, message),
            attachments=tuple(MediaReference("image", reference) for reference in references),
        )
    )
    assert record is not None
    return record.id


def _web_client(rig: ConfigRig, progress: EmbeddingProgress) -> TestClient:
    app = create_app(
        config=WebConfig(),
        auth=rig.auth,
        sessions=rig.sessions,
        channel=WebChannel(rig.heartbeat),
        timeline=rig.timeline,
        embedding_progress=progress,
    )
    return TestClient(app, client=("127.0.0.1", 50000))


def test_picture_progress_uses_saved_attachments_and_read_only_session(tmp_path: Path) -> None:
    rig = ConfigRig(tmp_path)
    try:
        database = rig.paths.database_file
        inbox = InboxStore(database, rig.clock)
        images = InboxImageIndex(database, _never_call, lambda: True)
        runtime = _runtime(tmp_path)
        progress = EmbeddingProgress(
            runtime, images=images, allows_conversation=lambda key: key.recipient_id == "1"
        )
        client = _web_client(rig, progress)
        path = "/api/organs/image_embedding/index-progress"
        assert client.get(path).status_code == 401
        assert client.post("/api/login", json={"password": GOOD}).status_code == 200
        assert client.get(path).json()["items"][0]["state"] == "empty"
        visible = _save_image(
            inbox, "1", "1", VisibilityScope.GROUP, "saved", "saved", "missing", "cleared"
        )
        hidden = _save_image(inbox, "2", "2", VisibilityScope.GROUP, "hidden")
        private = _save_image(inbox, "1", "3", VisibilityScope.PRIVATE, "private")
        _, space = runtime.embedding_spaces.image[0]
        with closing(open_connection(database)) as connection, connection:
            connection.executemany(
                "INSERT INTO inbox_image_vectors (message_id, attachment, space, vector)"
                " VALUES (?, ?, ?, '[1]')",
                (
                    (visible, "saved", space),
                    (visible, "cleared", space),
                    (hidden, "hidden", space),
                    (private, "private", space),
                ),
            )
            remaining = json.dumps(
                [("image", name, False) for name in ("saved", "saved", "missing")]
                + [("record", "cleared", False)]
            )
            connection.execute(
                "UPDATE inbox_messages SET attachments = ? WHERE id = ?", (remaining, visible)
            )
        before = database.read_bytes()
        rig.clock.advance(timedelta(minutes=1))
        response = client.get(path)
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json()["items"] == [
            {"kind": "images", "state": "partial", "route": 1, "established": 1, "eligible": 2},
            {"kind": "images", "state": "missing", "route": 2, "established": 0, "eligible": 2},
        ]
        assert database.read_bytes() == before
        assert all(
            text not in response.text
            for text in (space, "localhost", "不应出现在", "test-private-provider")
        )
        assert client.put(path).status_code == 405
        _switch(runtime, "second")
        assert client.get(path).json()["items"][0]["established"] == 0
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_expression_progress_excludes_stale_and_removed_but_not_old_reviews(
    tmp_path: Path,
) -> None:
    database = tmp_path / "test.db"
    language = LanguageStore(database)
    language.open()
    index = ExpressionVectorIndex(database)
    runtime = _runtime(tmp_path)
    space = runtime.local_expression_space
    assert space
    disabled = await EmbeddingProgress(runtime).read("text_embedding")
    assert disabled is not None and disabled[0].state == "disabled"
    progress = EmbeddingProgress(
        runtime,
        expression_enabled=True,
        language=language,
        expression_index=index,
        memory_rerank_enabled=True,
    )
    empty = await progress.read("text_embedding")
    assert empty is not None and (empty[0].state, empty[-1].state) == ("empty", "on_demand")
    for description in ("unchanged", "changed", "removed", "rejected"):
        language.add_entry(LanguageKind.EXPRESSION, LanguageContent(description, "测试"))
    entries = {entry.text: entry for entry in language.expression_candidates(None)}
    index.publish(space, list(entries.values()), [(1.0, 0.0)] * len(entries))
    language.update_entry(
        LanguageKind.EXPRESSION, entries["changed"].entry_key, LanguageContent("更新", "测试"), True
    )
    language.set_removed(LanguageKind.EXPRESSION, [entries["removed"].entry_key], True)
    language.review_expressions(
        [entries["rejected"].entry_key], ExpressionReviewStatus.REJECTED, "测试", NOW
    )
    language.add_entry(LanguageKind.EXPRESSION, LanguageContent("更新", "测试"))
    result = await progress.read("text_embedding")
    assert result is not None
    assert (result[0].state, result[0].established, result[0].eligible) == ("partial", 2, 3)


def test_expression_counts_only_package_grants_for_allowed_scopes(tmp_path: Path) -> None:
    database = tmp_path / "test.db"
    language = LanguageStore(database)
    language.open()
    index = ExpressionVectorIndex(database)
    package = LanguagePackageRow(LanguageContent("限范围表达", "测试"), 1, "来源", "原件")
    language.save_package(LanguageKind.EXPRESSION, [package])
    scope = ConversationKey("qq", "9", "group", "1").language_scope_key()
    language.package_scope(
        LanguageKind.EXPRESSION, f"package:{package.digest}", [scope], confirm=True
    )
    index.publish("test-space", language.expression_candidates(scope), [(1.0, 0.0)])
    assert index.coverage(language, "test-space", frozenset()) == (0, 0)
    assert index.coverage(language, "test-space", frozenset({scope})) == (1, 1)
    assert index.coverage(language, "old-space", frozenset({scope})) == (0, 1)


@pytest.mark.asyncio
async def test_emoji_progress_only_counts_valid_cached_descriptions_in_the_same_space(
    tmp_path: Path,
) -> None:
    clock = ManualClock(NOW)
    store = EmojiStore(tmp_path / "test.db", tmp_path, clock)
    store.open()
    store.collect(synthetic_emoji("red"), description="开心", register=True)
    calls: list[OrganRequest] = []

    async def embed(request: OrganRequest) -> OrganResponse:
        calls.append(request)
        assert request.organ_id == "text_embedding"
        return OrganResponse(
            "text_embedding",
            "",
            metadata={"embedding_space": "test-space"},
            embeddings=((1.0, 0.0),) * len(request.embedding_inputs),
        )

    # 默认已启用视觉选择（fa88456）；本用例只测文本嵌入的缓存，所以显式关掉。
    config = HeartbeatConfig(qq_emoji_visual_enabled=False)
    selector = EmojiSelector(store, embed, clock, config, None)
    spaces = ((1, "new-space"), (2, "test-space"))
    assert await selector.cache_coverage(spaces) == [(1, 0, 1), (2, 0, 1)]
    assert not calls
    assert (
        await selector.select(
            EmojiIntent("开心", False), (), "用户", ModelPreference.PREFERRED, has_embedding=True
        )
        is not None
    )
    store.collect(synthetic_emoji("blue"), description="难过", register=True)
    assert await selector.cache_coverage(spaces) == [(1, 0, 2), (2, 1, 2)]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_progress_discards_snapshot_if_models_change_mid_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    database = tmp_path / "test.db"
    images = InboxImageIndex(database, _never_call, lambda: True)
    started, release = threading.Event(), threading.Event()

    def read_rows() -> list[tuple[ConversationKey, str | None, int]]:
        started.set()
        assert release.wait(5)
        return []

    monkeypatch.setattr(images, "coverage_by_conversation", read_rows)
    progress = EmbeddingProgress(runtime, images=images, allows_conversation=lambda _key: True)
    task = asyncio.create_task(progress.read("image_embedding"))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        _switch(runtime, "second")
    finally:
        release.set()
    assert await task is None


def test_editor_ignores_responses_after_close_organ_change_and_model_edit() -> None:
    run_page_script(r"""
    const dialog = $('editor-dialog'), body = $('editor-body');
    body.contains = node => node.parentElement === body;
    const panel = el('aside', {});
    body.append(panel);
    dialog.showModal();
    const pending = [];
    api = path => new Promise(resolve => pending.push({path, resolve}));
    const result = {ok:true, data:{items:[{kind:'images', state:'partial', route:1,
      established:2, eligible:3}]}};
    const refresh = mountIndexProgress(panel, 'image_embedding');
    const closing = refresh();
    dialog.close();
    pending.shift().resolve(result);
    await closing;
    assert.equal(panel.children[0].tagName, 'P');
    dialog.showModal();
    const changingOrgan = refresh();
    editorGeneration++;
    pending.shift().resolve(result);
    await changingOrgan;
    assert.equal(panel.children[0].tagName, 'P');
    const activeRefresh = mountIndexProgress(panel, 'image_embedding');
    const changingModel = activeRefresh();
    mdl.revision++;
    const current = activeRefresh(Promise.resolve(true));
    pending.shift().resolve(result);
    await changingModel;
    assert.equal(panel.children[0].tagName, 'P');
    pending.shift().resolve(result);
    await current;
    const track = panel.children[0].children[1];
    assert.equal(track.attributes.role, 'progressbar');
    assert.equal(track.attributes['aria-valuenow'], 2);
    assert.equal(track.attributes['aria-valuemax'], 3);
    assert.equal(track.children.filter(cell => cell.className === 'filled').length, 8);
    assert.equal(panel.children[0].children.at(-1).textContent, '部分已建立');
    """)


def test_disabled_rows_name_the_remaining_requirements() -> None:
    run_page_script(r"""
    const textOf = node => (node.textContent || '') + node.children.map(textOf).join('');
    const row = (kind, state) => textOf(renderIndexProgress(
      {kind, state, route:null, established:0, eligible:null}));
    const name = key => CFG_FIELDS[key][0];
    const group = key => CFG_SECTIONS.find(([sec]) => sec === key.split('.')[0])[1];
    const recall = 'language.recall_enabled';
    const visual = 'heartbeat.qq_emoji_visual_enabled';
    const expression = row('expression', 'disabled');
    assert.equal(name(recall), '回复参考习得语言');
    for (const part of [name(recall), group(recall),
      '配置嵌入模型后自动建立', '保存后重启生效']) {
      assert.ok(expression.includes(part), part);
    }
    const emoji = row('emoji_cache', 'disabled');
    assert.equal(name(visual), 'QQ 表情视觉选择');
    for (const part of [name(visual), group(visual), name('qq.enabled'),
      name('heartbeat.qq_emoji_selection_enabled'), '配置嵌入模型后自动建立', '保存后重启生效']) {
      assert.ok(emoji.includes(part), part);
    }
    const others = ['no_model', 'unavailable', 'on_demand', 'empty', 'missing', 'partial',
      'complete'];
    for (const kind of ['expression', 'emoji_cache']) {
      for (const state of others) {
        const text = row(kind, state);
        assert.ok(!text.includes('保存后重启生效'), kind + state);
      }
    }
    for (const kind of ['images', 'memory_rerank']) {
      assert.ok(!row(kind, 'disabled').includes('保存后重启生效'), kind);
    }
    """)


def test_disabled_hint_link_closes_editor_and_locates_the_setting() -> None:
    run_page_script(r"""
    const findButton = node => node.tagName === 'BUTTON'
      ? node : node.children.map(findButton).find(Boolean);
    const recall = 'language.recall_enabled';
    const shown = [], located = [];
    showPage = (name, onShown) => { shown.push(name); onShown(); };
    locateConfigField = entry => located.push(entry);
    cf.searchEntries = [{key:'heartbeat.qq_emoji_visual_enabled', sec:'heartbeat', id:'other'},
      {key: recall, sec:'language', id:'cf-field-' + recall}];
    $('editor-dialog').showModal();
    const hint = renderIndexProgress({kind:'expression', state:'disabled', route:null,
      established:0, eligible:null});
    findButton(hint).click();
    assert.equal($('editor-dialog').open, false);
    assert.deepEqual(shown, ['config']);
    assert.deepEqual(located.map(entry => entry.id), ['cf-field-' + recall]);
    """)
