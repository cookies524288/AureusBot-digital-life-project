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
    get lastChild() { return this.children.at(-1) ?? null; },
    get elements() {
      if (this.tagName !== 'FORM') return undefined;
      const controls = {};
      function collect(parent) {
        for (const child of parent.children) {
          const name = child.attributes.name;
          if (name && ['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON'].includes(child.tagName)) {
            controls[name] = child;
          }
          collect(child);
        }
      }
      collect(this);
      return controls;
    },
    classList: {
      add: name => classes.add(name), remove: name => classes.delete(name),
      contains: name => classes.has(name),
      toggle(name, force) {
        const enabled = force === undefined ? !classes.has(name) : force;
        if (enabled) classes.add(name); else classes.delete(name);
        return enabled;
      },
    },
    append(...children) { for (const child of children) this.insertBefore(child, null); },
    insertBefore(child, next) {
      if (child.parentElement) child.remove();
      const index = next === null ? this.children.length : this.children.indexOf(next);
      this.children.splice(index, 0, child);
      child.parentElement = this;
    },
    replaceChildren(...children) {
      for (const child of this.children.slice()) child.remove?.();
      this.children = [];
      this.append(...children);
    },
    replaceWith(node) {
      if (!this.parentElement) return;
      this.parentElement.insertBefore(node, this);
      this.remove();
    },
    setAttribute(key, value) {
      this.attributes[key] = value;
      if (key === 'id') nodes.set(value, this);
      if (key === 'disabled') this.disabled = true;
    },
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); },
    removeEventListener() {}, focus() {},
    remove() {
      if (this.parentElement) {
        const siblings = this.parentElement.children;
        siblings.splice(siblings.indexOf(this), 1);
        this.parentElement = null;
      }
      this.removed = true;
    },
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
    createTextNode: text => Object.assign(element(), { nodeType: 3, textContent: text }),
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
  for (const script of input.scripts) vm.runInContext(script, context);
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
    static = ROOT / "aureus/web/static"
    html = (static / "index.html").read_text(encoding="utf-8")
    scripts = [
        (static / name).read_text(encoding="utf-8")
        for name in ("organ-graph.js", "resource-management.js", "memory-flow-transfer.js")
    ]
    result = subprocess.run(
        [node, "-e", HARNESS],
        input=json.dumps({"html": html, "scripts": scripts, "test": code}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_initial_chat_history_keeps_a_live_notice() -> None:
    run_page_script(
        r"""
        const log = $('chat-log');
        Object.defineProperty(log, 'textContent', {set() { this.children = []; }});
        let finishHistory;
        api = () => new Promise(resolve => { finishHistory = resolve; });
        state.loggedIn = true;
        openSocket();
        const loading = loadChatHistory();
        sockets.at(-1).onmessage({data:JSON.stringify({
          type:'notice', sender:'系统', channel:'web', content:'实时通知',
        })});
        const notice = log.children.at(-1);
        assert.equal(notice.className, 'msg notice');
        assert.equal(notice.children[0].textContent, '实时通知');
        finishHistory({ok:true, data:[]});
        await loading;
        assert.ok(log.children.includes(notice), '首次历史响应覆盖了实时通知');
        assert.equal(log.children.length, 2);
        """
    )


def test_initial_chat_history_merges_live_reply_by_record_id() -> None:
    run_page_script(
        r"""
        const log = $('chat-log');
        Object.defineProperty(log, 'textContent', {set() { this.children = []; }});
        let finishHistory;
        api = () => new Promise(resolve => { finishHistory = resolve; });
        state.loggedIn = true;
        openSocket();
        const loading = loadChatHistory();
        const push = message => sockets.at(-1).onmessage({data:JSON.stringify(message)});
        const time = '2026-11-22T12:00:00Z';
        push({type:'reply', id:12, time, sender:'小绿', channel:'web', content:'收到'});
        const liveReply = log.children.at(-1);
        finishHistory({ok:true, data:[
          {id:10, time:'2026-11-21T12:00:00Z', who:'user', channel:'terminal', content:'你好'},
          {id:11, time:'2026-11-21T12:01:00Z', who:'me', channel:'web', content:'收到'},
          {id:12, time, who:'me', channel:'web', content:'收到'},
          {id:13, time:'2026-11-22T12:01:00Z', who:'me', channel:'web', content:'晚些收到'},
        ]});
        await loading;
        push({type:'reply', id:13, time:'2026-11-22T12:01:00Z',
          sender:'小绿', channel:'web', content:'晚些收到'});
        const messages = Array.from(log.children).filter(node => node.className.startsWith('msg '));
        assert.deepEqual(messages.map(node => node.children[0].textContent),
          ['你好', '收到', '收到', '晚些收到']);
        assert.equal(messages[2], liveReply);
        assert.match(messages[0].children[1].textContent, /terminal/);
        assert.match(messages[2].children[1].textContent, /小绿 · web/);
        assert.equal(log.children.filter(node => node.className === 'chat-day').length, 2);
        """
    )


@pytest.mark.parametrize("send_count", [1, 2])
def test_chat_sends_only_after_initial_history_is_merged(send_count: int) -> None:
    run_page_script(
        rf"""
        let finishHistory;
        const sent = [];
        api = () => new Promise(resolve => {{ finishHistory = resolve; }});
        state.loggedIn = true;
        openSocket();
        Object.assign(sockets.at(-1), {{
          readyState:1, send:message => sent.push(JSON.parse(message).text),
        }});
        const loading = loadChatHistory();
        const box = $('chat-text');
        const send = $('chat-send').listeners.click[0];
        box.value = '你好';
        send();
        finishHistory({{ok:true, data:sent.map((content, id) => ({{
          id, time:'2026-11-22T12:00:00Z', who:'user', channel:'web', content,
        }}))}});
        await loading;
        const bubbles = () => $('chat-log').children.filter(node => node.className === 'msg user');
        assert.equal(bubbles().length, 0);
        assert.equal(box.value, '你好');
        send();
        for (let i = 1; i < {send_count}; i++) {{
          box.value = '你好';
          send();
        }}
        assert.deepEqual(sent, Array({send_count}).fill('你好'));
        assert.deepEqual(Array.from(bubbles(), node => node.children[0].textContent), sent);
        assert.equal(box.value, '');
        """
    )


def test_chat_history_failure_preserves_the_draft_for_retry() -> None:
    run_page_script(
        r"""
        let finishHistory;
        let requests = 0;
        const sent = [];
        api = () => {
          requests++;
          return new Promise(resolve => { finishHistory = resolve; });
        };
        state.loggedIn = true;
        openSocket();
        Object.assign(sockets.at(-1), {
          readyState:1, send:message => sent.push(JSON.parse(message).text),
        });
        const loading = loadChatHistory();
        finishHistory({ok:false, status:0, data:{detail:'断网'}});
        await loading;
        const box = $('chat-text');
        box.value = ' 草稿 ';
        const enter = $('chat-text').listeners.keydown[0];
        enter({key:'Enter', preventDefault() {}});
        $('chat-send').listeners.click[0]();
        assert.equal(requests, 2);
        assert.deepEqual(sent, []);
        assert.equal(box.value, ' 草稿 ');
        box.value = '修改后的草稿';
        const retry = loadChatHistory();
        finishHistory({ok:true, data:[]});
        await retry;
        assert.deepEqual(sent, []);
        enter({key:'Enter', preventDefault() {}});
        assert.deepEqual(sent, ['修改后的草稿']);
        assert.equal(box.value, '');
        """
    )


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


def test_launcher_exit_requires_confirmation_and_keeps_a_closed_page() -> None:
    run_page_script(
        """
        const token = '8123/00000000000000000000000000000001';
        let connections = 0, closes = 0;
        window.close = () => { closes++; };
        fetch = async url => {
          assert.equal(url, 'http://127.0.0.1:' + token);
          if (++connections === 1) throw new Error('连接中断');
          return {ok: true, text: async () => 'exited'};
        };
        await watchLauncherExit();
        assert.equal(connections, 0);
        location.hash = '#aureus-launcher=' + token;
        const waiting = watchLauncherExit();
        await Promise.resolve();
        assert.equal(window.name, 'aureus-launcher=' + token);
        assert.equal(closes, 0);
        timers.pop()();
        await waiting;
        assert.equal(closes, 1);
        assert.equal(window.name, '');
        assert.equal($('shutdown').classList.contains('hidden'), false);
        assert.match($('shutdown-title').textContent, /Aureus 已退出/);
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
