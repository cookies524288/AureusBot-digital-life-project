"""直接执行网页脚本，核对登录判断和异步重连；Node 只用于测试，不参与构建。"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const nodes = new Map();
function element(tag = 'div') {
  const classes = new Set();
  return {
    nodeType: 1, tagName: tag.toUpperCase(), dataset: {}, style: {}, value: '', textContent: '',
    children: [], listeners: {}, attributes: {},
    classList: {
      add: name => classes.add(name), remove: name => classes.delete(name),
      contains: name => classes.has(name),
      toggle(name, force) {
        const enabled = force === undefined ? !classes.has(name) : force;
        if (enabled) classes.add(name); else classes.delete(name);
        return enabled;
      },
    },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    setAttribute(key, value) {
      this.attributes[key] = value;
      if (key === 'id') nodes.set(value, this);
    },
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); },
    removeEventListener() {}, focus() {}, remove() { this.removed = true; },
    querySelectorAll() { return []; },
    showModal() { this.open = true; }, close() { this.open = false; },
  };
}
const timers = [], sockets = [];
const reply = (status, data) => ({
  ok: status >= 200 && status < 300, status,
  json: async () => data, headers: { get: () => null },
});
const context = vm.createContext({
  assert, timers, sockets, reply, URLSearchParams,
  document: {
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, element());
      return nodes.get(id);
    },
    querySelectorAll: () => [], createElement: element,
    createElementNS: (_, tag) => element(tag),
    createTextNode: text => ({ nodeType: 3, textContent: text }),
    documentElement: { dataset: {} },
  },
  window: { listeners: {}, addEventListener(name, callback) { this.listeners[name] = callback; } },
  localStorage: { getItem: () => null },
  location: { protocol: 'http:', host: 'localhost', hash: '' },
  history: { replaceState() {} },
  matchMedia: () => ({ matches: true, addEventListener() {} }),
  setTimeout: callback => { timers.push(callback); return callback; },
  clearTimeout: callback => {
    const i = timers.indexOf(callback); if (i >= 0) timers.splice(i, 1);
  },
  setInterval() {},
  fetch: async () => reply(401, { detail: '请先登录。' }),
  WebSocket: class { constructor() { sockets.push(this); } close() {} },
});
async function main() {
  vm.runInContext(input.html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
  await new Promise(resolve => setImmediate(resolve));
  await vm.runInContext('(async () => {' + input.test + '})()', context);
}
main().catch(error => { console.error(error); process.exitCode = 1; });
"""


def run_page_script(code: str) -> None:
    """用模拟 DOM、请求和定时器执行真实页面；没有 Node 的环境明确跳过。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("网页脚本行为测试需要 Node，不影响运行或打包")
    html = (ROOT / "aureus/web/static/index.html").read_text(encoding="utf-8")
    result = subprocess.run(
        [node, "-e", HARNESS],
        input=json.dumps({"html": html, "test": code}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_selected_task_keeps_its_text_until_the_selection_is_released() -> None:
    run_page_script(
        r"""
        const task = $('ov-task');
        showOverviewSignal({thinking:false, current_task:'原任务'});
        let selected = true;
        window.getSelection = () => ({isCollapsed:!selected, rangeCount:1,
          getRangeAt:() => ({intersectsNode:node => node === task})});
        showOverviewSignal({thinking:true, current_task:'中间任务'});
        showOverviewSignal({thinking:true, current_task:'最新任务'});
        assert.equal(task.textContent, '原任务');
        assert.equal($('ov-thinking').textContent, '正在想');
        selected = false;
        window.listeners.selectionchange();
        assert.equal(task.textContent, '最新任务');
        """
    )


def test_shutdown_request_is_only_sent_after_confirming() -> None:
    run_page_script(
        r"""
        const shutdown = $('shutdown-btn').listeners.click[0];
        const dialog = $('confirm-dialog');
        const requests = [];
        api = async (...request) => { requests.push(request); return {ok:true}; };

        const cancelled = shutdown();
        assert.equal($('confirm-text').textContent, '确定要关闭程序吗');
        await shutdown();
        assert.deepEqual(requests, []);
        dialog.returnValue = 'no';
        dialog.onclose();
        await cancelled;
        assert.deepEqual(requests, []);

        const confirmed = shutdown();
        dialog.returnValue = 'yes';
        dialog.onclose();
        await confirmed;
        assert.deepEqual(requests, [['/api/shutdown', 'POST']]);
        assert.equal($('shutdown').classList.contains('hidden'), false);
        """
    )


@pytest.mark.parametrize("probe", ["valid", "offline"])
def test_wrong_password_does_not_log_out_a_valid_session(probe: str) -> None:
    run_page_script(
        "const probe = " + json.dumps(probe) + ";"
        r"""
        state.loggedIn = true;
        $('app').classList.remove('hidden');
        const requests = [];
        fetch = async path => {
          requests.push(path);
          if (path === '/api/password') return reply(401, { detail: '密码不对。' });
          if (probe === 'offline') throw new TypeError('offline');
          return reply(200, { must_change_password: false });
        };
        const res = await api('/api/password', 'POST', {});
        assert.equal(res.status, 401);
        assert.equal(errorText(res), '密码不对。');
        assert.equal(state.loggedIn, true);
        assert.equal($('app').classList.contains('hidden'), false);
        assert.deepEqual(requests, ['/api/password', '/api/status']);
        """
    )


def test_an_expired_session_returns_to_login_and_closes_the_socket() -> None:
    run_page_script(
        r"""
        state.loggedIn = true;
        openSocket();
        fetch = async () => reply(401, { detail: '请先登录。' });
        await api('/api/history');
        assert.equal(state.loggedIn, false);
        assert.equal(socket, null);
        assert.equal(socketWanted, false);
        assert.equal($('login').classList.contains('hidden'), false);
        assert.equal($('login-error').textContent, '登录已失效，请重新登录。');
        """
    )


def test_logging_out_during_reconnect_does_not_open_a_socket() -> None:
    run_page_script(
        r"""
        state.loggedIn = true;
        socketWanted = true;
        let resolveStatus;
        fetch = () => new Promise(resolve => { resolveStatus = resolve; });
        reconnectLater();
        const pending = timers.pop()();
        showAuth('login');
        resolveStatus(reply(200, { must_change_password: false }));
        await pending;
        assert.equal(sockets.length, 0);
        assert.equal(socketWanted, false);
        """
    )


def test_a_valid_session_can_reconnect() -> None:
    run_page_script(
        r"""
        state.loggedIn = true;
        socketWanted = true;
        fetch = async () => reply(200, { must_change_password: false });
        reconnectLater();
        await timers.pop()();
        assert.equal(sockets.length, 1);
        assert.equal(socketWanted, true);
        """
    )


def test_the_default_password_only_opens_the_password_change_view() -> None:
    run_page_script(
        r"""
        fetch = async () => reply(200, { must_change_password: true });
        await enter();
        assert.equal(state.loggedIn, false);
        assert.equal($('app').classList.contains('hidden'), true);
        assert.equal($('change').classList.contains('hidden'), false);
        assert.equal(sockets.length, 0);
        """
    )
