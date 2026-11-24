"""模型页交互与回退确认：直接运行页面脚本，不使用真实接口。"""

from tests.web.test_page_script import ROOT, run_page_script


def test_late_close_event_preserves_reopened_model_editor() -> None:
    run_page_script(
        r"""
        const dialog = $('editor-dialog');
        openEditor('厂商', el('div'), false);
        dialog.close();
        const newForm = el('div');
        openEditor('模型', newForm, false);
        dialog.listeners.close[0]();
        assert.equal($('editor-body').children[0], newForm);
        dialog.close();
        dialog.listeners.close[0]();
        assert.equal($('editor-body').children.length, 0);
        """
    )


def test_explanatory_copy_is_removed_from_the_page_and_cards() -> None:
    html = (ROOT / "aureus/web/static/index.html").read_text(encoding="utf-8")
    for text in (
        "它此刻的状态。",
        "和它直接说话，这里说的话也会记进时间线。每条都标明来自哪个通道。",
        "它想成为的样子，随真实经历慢慢变化。",
    ):
        assert text not in html


def test_toast_fades_out_before_removing_the_node() -> None:
    run_page_script(
        r"""
        timers.length = 0;
        toast('动画验收');
        const node = $('toasts').children.at(-1);
        let finish;
        const finished = new Promise(resolve => { finish = resolve; });
        node.getAnimations = () => [{finished}];
        assert.equal(node.removed, undefined);
        timers.shift()();
        assert.equal(node.classList.contains('leaving'), true);
        assert.equal(node.removed, undefined);
        finish();
        await finished;
        await Promise.resolve();
        assert.equal(node.removed, true);
        """
    )


def test_repeated_model_warnings_share_one_visible_toast() -> None:
    run_page_script(
        r"""
        toast('replyer未配置模型', 'error');
        toast('replyer未配置模型', 'error');
        assert.equal($('toasts').children.length, 1);
        """
    )


def test_model_results_and_toasts_expire_independently_after_five_seconds() -> None:
    run_page_script(
        r"""
        let now = 0;
        const pending = [];
        setTimeout = (callback, delay) => pending.push({callback, at:now + delay});
        const advance = async time => {
          now = time;
          while (pending[0]?.at <= now) pending.shift().callback();
          await Promise.resolve();
        };
        const model = {name:'合成模型', identifier:'test', provider:'合成厂商', kind:'llm'};
        mdl.baseline = {models:[model]};
        mdl.provider = model.provider;
        api = async path => path === '/api/models/test'
          ? {ok:true, data:{model:model.name, provider:model.provider, kind:'llm', checks:[]}}
          : {ok:true, data:{latency_ms:1}};
        const probes = $('model-test-status');
        const toasts = $('toasts');
        const showError = detail => {
          fail({status:400, data:{detail}});
          toasts.lastChild.getAnimations = () => [];
        };
        await testModel(model);
        showError('第一条合成提示');
        const firstProbe = probes.lastChild, firstToast = toasts.lastChild;
        await advance(1000);
        await $('md-test-provider').listeners.click[0]();
        showError('第二条合成提示');
        const secondProbe = probes.lastChild, secondToast = toasts.lastChild;
        const assertVisible = (host, ...expected) => {
          assert.equal(host.children.length, expected.length);
          assert.ok(expected.every((notice, index) => host.children[index] === notice));
        };
        await advance(4999);
        assertVisible(probes, firstProbe, secondProbe);
        assertVisible(toasts, firstToast, secondToast);
        await advance(5000);
        assertVisible(probes, secondProbe);
        assertVisible(toasts, secondToast);
        await advance(5999);
        assertVisible(probes, secondProbe);
        assertVisible(toasts, secondToast);
        await advance(6000);
        assertVisible(probes);
        assertVisible(toasts);
        """
    )


def test_loading_models_does_not_replace_newer_edits() -> None:
    run_page_script(
        r"""
        mdl.cfg = {providers: [], models: [], organs: {}};
        mdl.defaults = {organs: {models: []}};
        const oldConfig = clone(mdl.cfg);
        let release;
        api = async (path, method) => method === 'PUT'
          ? {ok:true, data:{}} : new Promise(resolve => { release = resolve; });
        const loading = loadModelsConfig();
        mdl.cfg.organs.replyer = emptyOrgan();
        await markDirty();
        release({ok:true, data:{config:oldConfig}});
        await loading;
        assert.deepEqual(mdl.cfg.organs.replyer.models, []);
        """
    )


def test_model_delete_survives_a_config_refresh_during_confirmation() -> None:
    run_page_script(
        r"""
        mdl.cfg = {providers: [], models: [{name:'first'}], organs: {}};
        let accept;
        confirmAction = () => new Promise(resolve => { accept = resolve; });
        const removing = removeModel(mdl.cfg.models[0]);
        mdl.cfg = clone(mdl.cfg);
        accept(true);
        await removing;
        assert.equal(mdl.cfg.models.length, 0);
        """
    )


def test_builtin_organ_has_no_delete_action() -> None:
    run_page_script(
        r"""
        mdl.cfg = { providers: [], models: [], organs: { replyer: { models: [] } } };
        mdl.builtin = ['replyer', 'planner', 'judge', 'summarizer'];
        const card = organCard('replyer', mdl.cfg.organs.replyer);
        const walk = node => [node, ...(node.children || []).flatMap(walk)];
        assert.equal(walk(card).some(node => node.textContent === '删除器官'), false);
        """
    )


def test_deleting_a_used_model_keeps_the_reference_and_warns() -> None:
    run_page_script(
        r"""
        const model = {name: 'first', provider: 'local'};
        mdl.cfg = {providers: [{name:'local'}], models:[model],
          organs:{replyer:{models:['first']}}};
        const messages = [];
        toast = message => messages.push(message);
        globalThis.confirmAction = async () => true;
        await removeModel(model);
        assert.equal(mdl.cfg.models.length, 0);
        assert.deepEqual(mdl.cfg.organs.replyer.models, ['first']);
        assert.equal(messages.some(message => message.includes('replyer引用了不存在的模型')), true);
        """
    )


def test_declining_a_persona_rollback_makes_no_request() -> None:
    run_page_script(
        r"""
        let requests = 0;
        let question = '';
        $('page-persona').querySelector = () => ({});
        globalThis.confirmAction = async text => { question = text; return false; };
        fetch = async () => { requests++; return reply(403, {detail:'测试拒绝'}); };
        const card = personaLayerCard('mutable', '可变人格', '', [{id:1, content:'旧版本'}]);
        const walk = node => [node, ...(node.children || []).flatMap(walk)];
        const button = walk(card).find(node => node.textContent === '用这个版本');
        await button.listeners.click[0]();
        assert.equal(question, '确定要使用「可变人格」的这个历史版本吗？');
        assert.equal(requests, 0);
        """
    )


def test_persona_save_requires_changes_and_confirmation() -> None:
    run_page_script(
        r"""
        const requests = [];
        api = async (...request) => { requests.push(request); return {ok:true}; };
        loadPersona = async () => {};
        const body = {};
        $('page-persona').querySelector = () => body;
        const card = personaLayerCard('mutable', '可变人格', '原文', []);
        const walk = node => [node, ...(node.children || []).flatMap(walk)];
        const save = walk(card).find(node => node.textContent === '保存可变人格');
        const area = $('ps-mutable');
        const edit = value => { area.value = value; area.listeners.input[0](); };
        const dialog = $('confirm-dialog');
        const click = save.listeners.click[0];

        assert.equal(save.disabled, true);
        edit('草稿');
        assert.equal(save.disabled, false);
        edit('原文');
        assert.equal(save.disabled, true);
        edit('草稿');

        const cancelled = click();
        assert.equal(dialog.open, true);
        dialog.returnValue = 'no';
        dialog.onclose();
        await cancelled;
        assert.equal(area.value, '草稿');
        assert.equal(save.disabled, false);
        assert.deepEqual(requests, []);

        const confirmed = click();
        await click();
        assert.deepEqual(requests, []);
        dialog.returnValue = 'yes';
        dialog.onclose();
        await confirmed;
        assert.deepEqual(requests, [['/api/settings', 'PUT', {text:{mutable:'草稿'}}]]);
        """
    )
