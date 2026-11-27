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
          for (const field of Object.keys(definition.properties)) {
            expectedFields.push(`${sec}.${field}`);
          }
        }
        const sections = cfSectionList();
        const entries = configSearchEntries(sections);
        assert.deepEqual(sections.map(([sec]) => sec).sort(), expectedSections.sort());
        assert.deepEqual(entries.map(({key}) => key).sort(),
          [...expectedFields.filter(key => !excluded.has(key)), 'aureus-font'].sort());
        assert.deepEqual(Object.keys(CFG_FIELDS).sort(),
          entries.map(({key}) => key).filter(key => key !== 'aureus-font').sort());
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


def test_biochemical_rows_show_values_defaults_sources_and_toml_recovery(
    config_response: dict[str, Any],
) -> None:
    run_page_script(
        "const response = " + json.dumps(config_response) + ";"
        r"""
        response.config.hormones.anger_half_life_seconds = 731;
        cf.schema = response.schema;
        cf.data = response.config;
        cf.readonly = new Set(response.readonly);
        cf.biochemicalSources = response.biochemical_sources;
        for (const section of cfSectionList()) {
          const card = buildConfigSection(...section);
          const [sec] = section;
          const protectedFields = Object.keys(cf.biochemicalSources)
            .filter(key => key.startsWith(sec + '.'));
          if (protectedFields.length) {
            assert.ok(card.children.some(node =>
              node.textContent.includes(`[${sec}] 节只删除对应的键值行`)));
          }
          for (const key of protectedFields) {
            const field = key.split('.')[1];
            const row = $('cf-field-' + key).closest('.cfg-field');
            const control = row.children.at(-1);
            assert.equal(control.children.length, 1);
            const value = control.children[0];
            assert.equal(value.tagName, 'DIV');
            assert.equal(value.children[0].textContent, String(response.config[sec][field]));
            assert.equal(value.children[1].textContent,
              `开发默认值 ${cfSectionProperties(sec)[field].default}` +
              ` · 来源：${cf.biochemicalSources[key]}`);
          }
        }
        assert.equal($('cf-field-hormones.anger_enabled').tagName, 'INPUT');
        assert.equal($('cf-field-mood.enabled').tagName, 'INPUT');
        """
    )


def test_chat_settings_render_typed_controls_and_a_glossary_count(
    config_response: dict[str, Any],
) -> None:
    run_page_script(
        "const response = " + json.dumps(config_response) + ";"
        r"""
        response.chat_meme_glossary_count = 7;
        applyConfigData(state.loaded, response);
        cf.tabs.get('heartbeat').button.click();
        const split = $('cf-field-heartbeat.reply_split_mode');
        assert.equal(split.tagName, 'SELECT');
        const choices = Array.from(split.children, opt => [opt.attributes.value, opt.textContent]);
        assert.deepEqual(choices,
          [['natural', '自然分段'], ['explicit', '仅按显式标记分段']]);
        split.value = 'explicit'; split.listeners.change[0]();
        assert.equal(cf.changes['heartbeat.reply_split_mode'], 'explicit');
        const term = $('cf-field-heartbeat.chat_meme_glossary');
        const value = term.closest('.cfg-field').children.at(-1);
        assert.equal(value.children.length, 1);
        assert.equal(value.children[0].tagName, 'DIV');
        assert.match(value.children[0].textContent, /自定义 7 条.*config\/aureus\.toml/);
        const flags = {heartbeat:['qq_emoji_followup_enabled', 'qq_emoji_play_enabled',
          'chat_reminders_enabled', 'chat_nudges_enabled', 'chat_rechecks_enabled',
          'chat_group_lookup_enabled', 'chat_meme_lookup_enabled', 'chat_assets_enabled'],
          qq:['chat_actions_enabled', 'poke_response_enabled', 'follow_recall_enabled']};
        for (const [section, fields] of Object.entries(flags)) {
          cf.tabs.get(section).button.click();
          for (const field of fields) {
            const key = `${section}.${field}`;
            const target = $('cf-field-' + key);
            assert.equal(target.attributes.type, 'checkbox', key);
            target.checked = !response.config[section][field]; target.listeners.change[0]();
            assert.equal(cf.changes[key], target.checked, key);
          }
          const key = section === 'qq' ? 'qq.poke_cooldown_seconds' :
            'heartbeat.qq_emoji_followup_seconds';
          const seconds = $('cf-field-' + key);
          assert.equal(seconds.attributes.type, 'number');
          seconds.value = '4.5'; seconds.listeners.input[0]();
          assert.equal(cf.changes[key], 4.5);
        }
        assert.equal(Object.keys(cf.changes).length, 14);
        assert.equal($('cf-field-qq.endpoint').className, 'setting-term');
        """,
        include_resource_pages=False,
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
