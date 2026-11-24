"""用默认配置的离线 schema 检查设置查找、定位和草稿。"""

import json
from typing import Any
from unittest.mock import patch

import pytest

from aureus.common import RuntimeConfig
from aureus.web.config_files import HIDDEN_CONFIG, ConfigService
from tests.web.test_page_script import run_page_script


@pytest.fixture
def config_response() -> dict[str, Any]:
    """执行实际接口使用的序列化方法，不读取任何配置文件。"""
    service = ConfigService.__new__(ConfigService)
    with patch.object(service, "read_runtime", return_value=(False, RuntimeConfig())):
        return service.view_runtime()


def test_settings_catalog_covers_the_received_schema(config_response: dict[str, Any]) -> None:
    """逐节验证 schema 字段都有中文、帮助和真正的定位目标。"""
    run_page_script(
        "cf.schema = " + json.dumps(config_response["schema"]) + ";"
        "cf.data = " + json.dumps(config_response["config"]) + ";"
        "cf.readonly = new Set(" + json.dumps(config_response["readonly"]) + ");"
        "const excluded = new Set(" + json.dumps(sorted(HIDDEN_CONFIG | {"qq.token_key"})) + ");"
        r"""
        const expectedFields = [];
        const expectedSections = [''];
        for (const [sec, node] of Object.entries(cf.schema.properties)) {
          if (!node.$ref) { expectedFields.push(sec); continue; }
          const definition = cf.schema.$defs[node.$ref.split('/').pop()];
          expectedSections.push(sec);
          for (const field of Object.keys(definition.properties)) expectedFields.push(`${sec}.${field}`);
        }
        const sections = cfSectionList();
        const entries = configSearchEntries(sections);
        assert.deepEqual(sections.map(([sec]) => sec).sort(), expectedSections.sort());
        assert.deepEqual(entries.map(({key}) => key).sort(),
          [...expectedFields.filter(key => !excluded.has(key)), 'aureus-font'].sort());
        assert.deepEqual(Object.keys(CFG_FIELDS).sort(), entries.map(({key}) => key).filter(key => key !== 'aureus-font').sort());
        function checkHelp(term, name) {
          assert.equal(term.className, 'setting-term');
          assert.equal(term.attributes.tabindex, 0);
          assert.match(term.attributes['aria-label'], /\p{Script=Han}/u);
          assert.equal(term.attributes['aria-label'], name);
          const help = $(term.attributes['aria-describedby']);
          assert.equal(help.attributes.role, 'tooltip');
          assert.match(help.children[1].textContent, /\p{Script=Han}{3,}/u);
          return help.children[1].textContent;
        }
        for (const section of sections) {
          const [sec, title] = section;
          const card = buildConfigSection(...section);
          checkHelp(card.children[0].children[0].children[0].children[0], title);
          for (const {id, key, name} of entries.filter(entry => entry.sec === sec)) {
            const target = $(id);
            const row = target.closest('.cfg-field');
            const help = checkHelp(row.children[0].children[0], name);
            if (cf.readonly.has(key)) {
              assert.equal(target.className, 'setting-term');
              assert.match(help, /只读/);
            } else {
              assert.ok(['INPUT', 'SELECT'].includes(target.tagName));
              assert.match(help, key === 'aureus-font' ? /立即生效/ : /重启后生效/);
            }
          }
        }
        """
    )


def test_settings_search_uses_real_controls_and_preserves_drafts(
    config_response: dict[str, Any],
) -> None:
    """按名称、说明、节和路径查找，验证焦点、原控件和草稿。"""
    run_page_script(
        "const response = " + json.dumps(config_response) + ";"
        r"""
        response.config.qq.token_key = 'PRIVATE_TEST_KEY_REFERENCE';
        response.config.exploration.search_endpoint = 'https://private.example.invalid/value';
        const requests = [];
        api = async path => { requests.push(path); return {ok:true, data:response}; };
        await loadConfigPage();
        const search = $('cf-search');
        const sidebar = $('cf-sidebar');
        const clear = $('cf-search-clear');
        search.focus();
        const query = value => {search.value = value; search.listeners.input[0]();};
        const key = (name, target = search, isComposing = false) =>
          sidebar.listeners.keydown[0]({key:name, target, isComposing, preventDefault() {}});
        query('界面字体');
        assert.equal(cf.results.length, 1);
        assert.equal(document.activeElement, search);
        assert.equal(cf.tabs.get('').button.attributes['aria-disabled'], 'true');
        cf.tabs.get('').button.click();
        assert.equal(search.value, '界面字体');
        assert.equal(document.activeElement, search);
        key('Enter', search, true);
        assert.equal(document.activeElement, search);
        key('ArrowDown');
        assert.equal(document.activeElement, cf.results[0]);
        key('ArrowUp', cf.results[0]);
        assert.equal(document.activeElement, search);
        key('Enter');
        const font = $('cf-font');
        assert.equal(document.activeElement, font);
        assert.equal(font.closest('.cfg-field').scrolled.block, 'center');
        const preferences = [];
        localStorage.setItem = (...preference) => preferences.push(preference);
        font.value = 'system';
        font.listeners.change[0]();
        assert.deepEqual(preferences, [['aureus-font', 'system']]);
        assert.equal(Object.keys(cf.changes).length, 0);

        query('搜索服务地址 探索');
        assert.equal(cf.results.length, 1);
        cf.results[0].click();
        const address = $('cf-field-exploration.search_endpoint');
        assert.equal(document.activeElement, address);
        assert.equal(cf.tab, 'exploration');
        assert.equal(cf.tabs.get('exploration').button.attributes['aria-current'], 'true');
        assert.equal(cf.tabs.get('exploration').button.attributes['aria-disabled'], 'true');
        assert.equal(cf.tabs.get('').button.attributes['aria-disabled'], 'false');
        address.value = 'https://search.example.invalid/search';
        address.listeners.input[0]();
        query('WEB.SESSION_DAYS');
        key('Enter');
        const days = $('cf-field-web.session_days');
        assert.equal(document.activeElement, days);
        days.value = '';
        days.validity = {valid:false, badInput:true};
        days.listeners.input[0]();
        assert.equal(cf.invalid.has('web.session_days'), true);
        assert.equal($('cf-save').disabled, false);

        query('找不到的合成设置');
        assert.equal(cf.results.length, 0);
        assert.match($('cf-search-state').textContent, /没有匹配/);
        assert.equal($('cf-search-results').classList.contains('hidden'), true);
        assert.equal(clear.classList.contains('hidden'), false);
        clear.click();
        assert.equal(document.activeElement, search);
        assert.equal(search.value, '');
        assert.equal(cf.tab, 'web');
        assert.equal(cf.sections.get('web').classList.contains('hidden'), false);
        assert.equal(days.validity.badInput, true);

        query('从谷值到峰值');
        assert.equal(cf.results.length, 1);
        key('Enter');
        assert.equal(document.activeElement, $('cf-field-cortisol.baseline_span'));
        assert.equal(document.activeElement.className, 'setting-term');
        cf.tabs.get('exploration').button.click();
        assert.equal(search.value, '');
        assert.equal(document.activeElement, cf.tabs.get('exploration').button);
        assert.equal(address.value, 'https://search.example.invalid/search');
        assert.equal(cf.changes['exploration.search_endpoint'], address.value);
        cf.tabs.get('web').button.click();
        assert.equal($('cf-field-web.session_days'), days);
        assert.equal(days.validity.badInput, true);
        assert.equal(days.attributes['aria-invalid'], 'true');
        await $('cf-save').listeners.click[0]();
        assert.deepEqual(requests, ['/api/config']);

        for (const forbidden of ['qq.token_key', 'PRIVATE_TEST_KEY_REFERENCE',
          'private.example.invalid', 'heartbeat.advance_biomodel', 'cf-password']) {
          query(forbidden);
          assert.equal(cf.results.length, 0, forbidden);
        }
        key('Escape');
        assert.equal(search.value, '');
        assert.equal(document.activeElement, search);
        assert.equal(cf.tab, 'web');
        assert.equal(cf.invalid.has('web.session_days'), true);
        assert.equal(cf.changes['exploration.search_endpoint'], address.value);
        assert.deepEqual(requests, ['/api/config']);
        """
    )


def test_platform_config_shortcut_selects_readonly_section_with_existing_drafts(
    config_response: dict[str, Any],
) -> None:
    """从加了草稿的设置页返回平台，用右键跳进原有的 QQ 设置节。"""
    run_page_script(
        "const response = " + json.dumps(config_response) + ";"
        r"""
        const requests = [];
        api = async request => { requests.push(request); return {ok:true, data:response}; };
        await loadConfigPage();
        cf.tabs.get('web').button.click();
        const days = $('cf-field-web.session_days');
        days.value = '3.5';
        days.validity = {valid:false, badInput:false};
        days.listeners.input[0]();
        const search = $('cf-search');
        search.value = '界面字体';
        search.listeners.input[0]();

        state.page = 'inbox';
        let opened, prevented = false;
        const navigate = showPage;
        showPage = (...args) => opened = navigate(...args);
        $('inbox-open-qq').listeners.contextmenu[0]({preventDefault() { prevented = true; }});
        await opened;
        const tab = cf.tabs.get('qq').button;
        assert.equal(prevented, true);
        assert.equal(state.page, 'config');
        assert.equal(cf.tab, 'qq');
        assert.equal(search.value, '');
        assert.equal(document.activeElement, tab);
        assert.equal(tab.attributes['aria-current'], 'true');
        assert.equal(tab.attributes['aria-disabled'], 'true');
        assert.equal($('cf-field-qq.enabled').classList.contains('setting-term'), true);
        cf.tabs.get('web').button.click();
        assert.equal(tab.attributes['aria-disabled'], 'false');
        assert.equal($('cf-field-web.session_days'), days);
        assert.equal(days.value, '3.5');
        assert.equal(cf.invalid.has('web.session_days'), true);
        assert.deepEqual(requests, ['/api/config']);
        """
    )
