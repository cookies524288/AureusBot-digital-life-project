"""设置总重置网页交互：执行真实网页脚本，使用合成数据和请求。"""

from tests.web.test_page_script import run_page_script


def test_cancel_reset_keeps_drafts_and_browser_preferences_without_sending_request() -> None:
    run_page_script(
        r"""
        cf.data = {log_level:'DEBUG'};
        cf.changes = {log_level:'ERROR'};
        document.documentElement.dataset.font = 'system';
        const calls = [];
        api = async (...args) => { calls.push(args); };
        localStorage.removeItem = key => calls.push(key);
        const dialog = $('cf-restore-all-dialog');
        const open = $('cf-restore-all').listeners.click[0];
        const cancel = $('cf-restore-all-cancel').listeners.click[0];
        open();
        assert.equal(dialog.open, true);
        $('cf-restore-all-password').value = 'synthetic password';
        cancel();
        dialog.listeners.close[0]();
        assert.equal(dialog.open, false);
        assert.equal($('cf-restore-all-password').value, '');
        assert.equal(cf.changes.log_level, 'ERROR');
        assert.equal(cf.data.log_level, 'DEBUG');
        assert.equal(document.documentElement.dataset.font, 'system');
        assert.equal(calls.length, 0);
        """
    )


def test_reset_updates_settings_and_browser_font_only_after_server_accepts() -> None:
    run_page_script(
        r"""
        cf.data = {log_level:'DEBUG'};
        cf.changes = {log_level:'ERROR'};
        cf.invalid.add('web.session_days');
        document.documentElement.dataset.font = 'system';
        const preferences = new Map([['aureus-font','system'], ['aureus-theme','dark']]);
        localStorage.removeItem = key => preferences.delete(key);
        let respond;
        const calls = [];
        api = (path, method, body) => {
          calls.push([path, method, body.password]);
          return new Promise(resolve => { respond = resolve; });
        };
        let renders = 0;
        renderConfigSection = () => { renders++; };
        const dialog = $('cf-restore-all-dialog');
        const submit = $('cf-restore-all-form').listeners.submit[0];
        const password = $('cf-restore-all-password');
        const event = {preventDefault() {}};
        $('cf-restore-all').listeners.click[0]();
        password.value = 'wrong';
        const rejected = submit(event);
        assert.equal($('cf-restore-all-submit').disabled, true);
        assert.equal(preferences.get('aureus-font'), 'system');
        respond({ok:false, status:403, data:{detail:'密码不对。'}});
        await rejected;
        assert.equal(dialog.open, true);
        assert.equal(password.value, '');
        assert.equal($('cf-restore-all-error').textContent, '密码不对。');
        assert.equal(cf.data.log_level, 'DEBUG');
        assert.equal(cf.changes.log_level, 'ERROR');
        assert.equal(cf.invalid.size, 1);
        assert.equal(document.documentElement.dataset.font, 'system');
        assert.equal(renders, 0);

        password.value = 'synthetic password';
        const accepted = submit(event);
        assert.equal(preferences.get('aureus-font'), 'system');
        respond({ok:true, data:{config:{log_level:'INFO'}, changed:['log_level'], restart_required:true}});
        await accepted;
        assert.equal(dialog.open, false);
        assert.equal(cf.data.log_level, 'INFO');
        assert.equal(Object.keys(cf.changes).length, 0);
        assert.equal(cf.invalid.size, 0);
        assert.equal(document.documentElement.dataset.font, DEFAULT_UI_FONT);
        assert.equal(preferences.has('aureus-font'), false);
        assert.equal(preferences.get('aureus-theme'), 'dark');
        assert.equal(renders, 1);
        assert.deepEqual(calls, [
          ['/api/config/restore-defaults','POST','wrong'],
          ['/api/config/restore-defaults','POST','synthetic password'],
        ]);
        """
    )
