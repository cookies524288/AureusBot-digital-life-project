"""运行真实页面脚本：独立密钥页、原文清理与按名称选择。"""

from tests.web.test_page_script import run_page_script


def test_key_cards_render_metadata_as_text_and_have_no_reveal_action() -> None:
    run_page_script(
        r"""
        const name = '<img src=x onerror=alert(1)>';
        const entry = {id:'a'.repeat(32), name, kind:'password'};
        api = async () => ({ok:true, data:{vault:'ok', entries:[entry]}});
        await loadKeysPage();
        const walk = node => [node, ...(node.children || []).flatMap(walk)];
        const nodes = walk($('ky-list'));
        assert.equal(nodes.some(node => node.tagName === 'IMG'), false);
        assert.equal(nodes.some(node => node.textContent === name), true);
        const buttons = nodes.filter(node => node.tagName === 'BUTTON');
        assert.deepEqual(buttons.map(node => node.textContent), ['改名','删除']);
        assert.equal(PAGES.keys.load, loadKeysPage);
        """
    )


def test_create_clears_secret_inputs_before_request_and_never_retains_response_values() -> None:
    run_page_script(
        r"""
        const body = openKeyEditor(null);
        const inputs = body.children.map(field => field.children.at(-1));
        for (const input of inputs) input.reportValidity = () => true;
        const [name, kind, secret, password] = inputs;
        name.value = '我的接口'; kind.value = 'api_key';
        secret.value = 'fake-private-value'; password.value = 'fake-login';
        let requests = 0;
        api = async (path, method, data) => {
          if (method === 'POST') {
            requests++; assert.equal(path, '/api/keys');
            assert.equal(data.value, 'fake-private-value');
            assert.equal(data.password, 'fake-login');
            assert.equal(secret.value, ''); assert.equal(password.value, '');
            return {ok:true, data:{id:'b'.repeat(32)}};
          }
          return {ok:true, data:{vault:'ok', entries:[]}};
        };
        await $('editor-footer').children[0].listeners.click[0]();
        assert.equal(requests, 1);
        assert.equal(secret.value, ''); assert.equal(password.value, '');
        """
    )


def test_other_type_can_enter_multiline_values() -> None:
    run_page_script(
        r"""
        const body = openKeyEditor(null);
        const kind = body.children[1].children.at(-1);
        kind.value = 'other';
        kind.listeners.change[0]();
        const value = body.children[2].children.at(-1);
        assert.equal(value.tagName, 'TEXTAREA');
        value.value = 'fake-line-one\n fake-line-two ';
        assert.equal(value.value.endsWith(' '), true);
        assert.equal(value.value.includes(String.fromCharCode(10)), true);
        """
    )


def test_provider_select_lists_only_api_keys_and_preserves_missing_reference() -> None:
    run_page_script(
        r"""
        mdl.cfg = {providers:[],models:[],organs:{}}; mdl.choices = {};
        mdl.entries = [{id:'a'.repeat(32),name:'我的 API',kind:'api_key'},
          {id:'b'.repeat(32),name:'网站密码',kind:'password'}];
        openProviderEditor({name:'p',protocol:'openai',api_key_ref:'c'.repeat(32),
          api_key_env:null,timeout_seconds:60,max_retry:2});
        const walk = node => [node, ...(node.children || []).flatMap(walk)];
        const options = walk($('editor-body')).filter(node => node.tagName === 'OPTION');
        assert.equal(options.some(node => node.textContent === '我的 API'), true);
        assert.equal(options.some(node => node.textContent === '网站密码'), false);
        assert.equal(options.some(node => node.textContent === '引用的 API key 已删除'), true);
        """
    )


def test_provider_key_creation_retries_and_selects_key_in_the_same_draft() -> None:
    run_page_script(
        r"""
        mdl.cfg = {providers:[],models:[],organs:{}};
        mdl.defaults = {providers:{timeout_seconds:60,max_retry:2}};
        const existing = {id:'a'.repeat(32),name:'原密钥',kind:'api_key'};
        const created = {id:'b'.repeat(32),name:'新增密钥',kind:'api_key'};
        mdl.entries = [existing];
        openProviderEditor(null);
        const parent = $('editor-body').children[0];
        const control = (label, body = parent) => body.children.flatMap(node => node.children)
          .find(node => node.attributes['aria-label'] === label);
        const trigger = parent.children.at(-1).children[1];
        const source = control('API key');
        const name = control('API供应商名称');
        assert.deepEqual(Array.from(source.children, node => node.textContent),
          ['不使用密钥','原密钥']);
        assert.equal(control('环境变量名'), undefined);
        name.value = '供应商草稿';
        source.value = 'vault:' + existing.id;
        trigger.listeners.click[0]({currentTarget:trigger});
        assert.equal($('editor-dialog').open, true);
        assert.equal($('provider-key-dialog').open, true);
        const footer = $('provider-key-footer');
        footer.children[1].listeners.click[0]();
        assert.equal($('editor-dialog').open, true);
        assert.equal($('provider-key-dialog').open, false);
        assert.equal(source.value, 'vault:' + existing.id);

        trigger.listeners.click[0]({currentTarget:trigger});
        const child = $('provider-key-body').children[0];
        const save = footer.children[0];
        for (const field of child.children) field.children.at(-1).reportValidity = () => true;
        const secret = control('密钥原文', child), password = control('当前密码', child);
        control('名称', child).value = created.name;
        assert.equal(control('类型', child).children.length, 1);
        const requests = [];
        api = async (path, method, data) => {
          assert.equal(path, '/api/keys');
          if (method === 'POST') {
            requests.push({...data});
            if (requests.length === 1) return {ok:false, status:403, data:{detail:'请重试'}};
            return {ok:true, data:{id:created.id}};
          }
          return {ok:true, data:{entries:[existing, created]}};
        };
        secret.value = 'synthetic-key'; password.value = 'wrong-password';
        await save.listeners.click[0]();
        assert.equal($('provider-key-dialog').open, true);
        assert.equal(save.disabled, false);
        assert.equal(secret.value, ''); assert.equal(password.value, '');
        assert.equal(source.value, 'vault:' + existing.id);
        secret.value = 'synthetic-key'; password.value = 'synthetic-password';
        await save.listeners.click[0]();
        assert.deepEqual(requests.map(({kind,password}) => [kind,password]),
          [['api_key','wrong-password'],['api_key','synthetic-password']]);
        assert.equal($('provider-key-dialog').open, false);
        assert.equal($('editor-dialog').open, true);
        assert.equal(source.value, 'vault:' + created.id);
        assert.equal(name.value, '供应商草稿');
        assert.equal(mdl.cfg.providers.length, 0);
        """
    )


def test_editing_other_provider_fields_keeps_its_existing_environment_reference() -> None:
    run_page_script(
        r"""
        const provider = {name:'旧供应商',protocol:'openai',base_url:null,
          api_key_env:'AUREUS_KEY_FIXTURE',timeout_seconds:60,max_retry:2};
        mdl.cfg = {providers:[provider],models:[],organs:{}};
        mdl.entries = [];
        openProviderEditor(provider);
        const fields = $('editor-body').children[0].children.flatMap(node => node.children)
          .filter(node => ['INPUT','SELECT'].includes(node.tagName));
        const source = fields.find(node => node.attributes['aria-label'] === 'API key');
        const timeout = fields.find(node => node.attributes['aria-label'] === '超时（秒）');
        assert.deepEqual(Array.from(source.children, node => node.textContent),
          ['不使用密钥','环境变量 · AUREUS_KEY_FIXTURE']);
        for (const field of fields) field.reportValidity = () => true;
        markDirty = async () => false;
        timeout.value = '75';
        timeout.listeners.change[0]();
        assert.equal(provider.api_key_env, 'AUREUS_KEY_FIXTURE');
        assert.equal(provider.timeout_seconds, 75);
        source.value = '';
        source.listeners.change[0]();
        assert.equal(provider.api_key_env, null);
        openProviderEditor(provider);
        const reopened = $('editor-body').children[0].children.flatMap(node => node.children)
          .find(node => node.attributes['aria-label'] === 'API key');
        assert.deepEqual(Array.from(reopened.children, node => node.textContent), ['不使用密钥']);
        """
    )
