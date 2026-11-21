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
