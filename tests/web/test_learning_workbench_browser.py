"""用独立浏览器验证发布产物的前端行为；所有工作台 API 由合成响应拦截。"""

import importlib.util
from pathlib import Path

_BROWSER_SPEC = importlib.util.spec_from_file_location(
    "aureus_d111_resource_browser", Path(__file__).with_name("test_resource_browser.py")
)
assert _BROWSER_SPEC is not None and _BROWSER_SPEC.loader is not None
_BROWSER_MODULE = importlib.util.module_from_spec(_BROWSER_SPEC)
_BROWSER_SPEC.loader.exec_module(_BROWSER_MODULE)
Browser = _BROWSER_MODULE.Browser
_open_browser = _BROWSER_MODULE._open_browser

WORKBENCH_RESPONSES = """(() => {
  if (!['/learning-workbench/', '/learning-workbench'].includes(location.pathname)) return;
  const native = window.fetch;
  const requests = [];
  const held = new Map();
  const errors = [];
  const qualityHistory = [];
  window.addEventListener('error', event => errors.push(event.message));
  const control = window.fixture = {
    requests, errors, held, qualityHistory, handoffPage: null, verdict: 'unreviewed',
  };
  const expression = page => ({ id:page, situation:`合成情境 ${page}`, style:`合成表达 ${page}`,
    last_active_time:1, chat_id:'synthetic', create_date:1, checked:false, modified_by:null });
  const jargon = {id:1, content:'合成黑话', meaning:'合成释义', session_id:'synthetic',
    session_ids:['synthetic'], chat_name:'合成会话', chat_names:['合成会话'], count:1,
    is_jargon:true, is_complete:true, is_global:false, created_by:'ai',
    created_timestamp:1, updated_timestamp:1};
  const emoji = () => ({id:1, emoji_hash:'synthetic', full_path:'', format:'png',
    description:'合成表情', usage_count:0, query_count:0, record_time:1, register_time:null,
    last_used_time:null, emotion:null, is_registered:false, is_banned:false, status:'unknown',
    review:control.verdict});
  window.fetch = async (input, options={}) => {
    const url = new URL(input, location.href);
    if (url.origin !== location.origin) throw Error('Unexpected external request');
    if (url.pathname === '/api/resources/learning/quality') {
      const offset = Number(url.searchParams.get('offset') || 0);
      const items = Array.from({length:25}, (_, index) => ({
        id:index + 1, status:'rejected', reason:'mismatch', recorded_at:'2026-11-24T10:00:00+00:00',
        source_label:'合成会话', text:`合成质检说法 ${index + 1}`, detail:'合成情境',
        actions:qualityHistory.filter(item => item.id === index + 1)
          .map(({action, recorded_at}) => [action, recorded_at]),
      }));
      requests.push({method:options.method || 'GET', path:url.pathname, offset,
        query:Object.fromEntries(url.searchParams)});
      return Response.json({enabled:true, total:items.length,
        items:items.slice(offset, offset + 20),
        sources:[{key:'synthetic-scope', label:'合成会话'}]});
    }
    if (url.pathname.startsWith('/api/resources/learning/quality/')) {
      const id = Number(url.pathname.split('/').at(-1));
      const body = JSON.parse(options.body);
      requests.push({method:options.method, path:url.pathname, body});
      if (!body.version) return Response.json({id, scope:'synthetic-scope',
        text:`合成质检说法 ${id}`, detail:'合成情境', version:'a'.repeat(64)});
      if (body.version !== 'a'.repeat(64)) {
        return Response.json({detail:'版本已变化'}, {status:409});
      }
      qualityHistory.push({id, action:body.action, recorded_at:'2026-11-25T10:00:00+00:00'});
      return Response.json({entry_key:`synthetic-${id}`});
    }
    if (!url.pathname.startsWith('/api/webui/')) return native(input, options);
    const method = options.method || 'GET';
    const path = url.pathname.slice('/api/webui'.length);
    const page = Number(url.searchParams.get('page')) || 1;
    const body = options.body && JSON.parse(options.body);
    requests.push({method, path, page, body, search:url.searchParams.get('search')});
    const result = data => Response.json({success:true, data});
    const listing = (data, total) => Response.json({success:true, data, total, page, page_size:20});
    if (path.endsWith('/chats') || path.endsWith('/chat-targets')) return result([{
      chat_id:'synthetic', session_id:'synthetic', chat_name:'合成会话', platform:'web',
      is_group:false, use_expression:true, enable_learning:true,
    }]);
    if (path.endsWith('/groups')) return result([]);
    switch (path) {
      case '/expression/list': {
        const response = listing([expression(page)], 160);
        if (control.handoffPage === page) {
          return new Promise(resolve => held.set(page, () => resolve(response)));
        }
        return response;
      }
      case '/expression/stats/summary':
        return result({total:160,recent_7days:1,chat_count:1,top_chats:{}});
      case '/expression/batch/delete':
        return Response.json({success:true,deleted_count:body.ids.length});
      case '/jargon/list': return listing([jargon], 1);
      case '/jargon/1': return result(jargon);
      case '/jargon/stats/summary':
        return result({total:1,confirmed_jargon:1,confirmed_not_jargon:0,
        manual_jargon:0,pending:0,global_count:0,complete_count:1,chat_count:1,top_chats:{}});
      case '/emoji/list': return listing([emoji()], 1);
      case '/emoji/stats/summary': return result({total:1,registered:0,unregistered:1,banned:0,
        known:0,unknown:1,adopted:0,discarded:0,formats:{png:1},top_used:[]});
      case '/emoji/1/review':
        if (method !== 'PATCH') throw Error('Expected PATCH');
        if (body.verdict === 'rejected') {
          return Response.json({detail:'合成审批冲突'}, {status:409});
        }
        control.verdict = body.verdict;
        return result(emoji());
      default: return Response.json({detail:'未提供测试端点'}, {status:404});
    }
  };
})()"""

INTERACTION = """(async () => {
  const wait = async (test, message) => {
    const deadline = performance.now() + 5000;
    while (performance.now() < deadline) {
      const value = test();
      if (value) return value;
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    throw Error(typeof message === 'function' ? message() : message);
  };
  await wait(() => document.querySelector('#page-learning.active'), '合成登录后未打开学习页');
  const parentButton = await wait(() => {
    const button = document.querySelector('#learning-workbench-open');
    return button?.getClientRects().length && button;
  }, '缺少产品学习入口');
  parentButton.click();
  const frame = document.querySelector('#learning-workbench-frame');
  const doc = () => frame.contentDocument;
  const visible = node => node.getClientRects().length > 0;
  const find = selector => [...doc().querySelectorAll(selector)].find(visible);
  const click = (selector, message) =>
    wait(() => find(selector), message).then(element => element.click());
  const button = label => [...doc().querySelectorAll('button')]
    .find(node => visible(node) && node.textContent.trim() === label);
  const text = value => doc().body?.innerText.includes(value);
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const control = await wait(() => frame.contentWindow.fixture, '未加载工作台构建产物');
  await wait(() => text('合成表达 1'), '表达列表没有加载');
  await wait(() => find('input[aria-label="跳转页码"]'), '页码输入不应藏在额外菜单中');
  await click('[aria-label="选择表达方式：合成情境 1"]', '表达多选不可用');
  control.handoffPage = 2;
  await click('[aria-label="第 2 页"]', '缺少第 2 页');
  await wait(() => control.held.has(2), '未发送第 2 页请求');
  const input = await wait(() => find('input[aria-label="跳转页码"]:enabled'), '翻页中不能跳页');
  input.value = '6';
  input.form.requestSubmit();
  await wait(() => text('合成表达 6'), '未渲染目标页');
  control.held.get(2)();
  await new Promise(resolve => setTimeout(resolve, 150));
  check(text('合成表达 6') && !text('合成表达 2'), '迟到页覆盖当前显示');
  await click('[aria-label="选择表达方式：合成情境 6"]', '目标页无法多选');
  check(text('已选择 2'), '跨页选中状态丢失');
  const batch = await wait(() => button('批量删除'), '缺少批量操作');
  batch.click();
  const confirm = await wait(() => {
    const dialog = find('[role=alertdialog], [role=dialog]');
    return [...(dialog?.querySelectorAll('button') ?? [])]
      .find(node => /删除/.test(node.textContent));
  }, '缺少批量确认');
  confirm.click();
  const bulk = await wait(
    () => control.requests.find(item => item.path === '/expression/batch/delete'),
    '未请求批量操作');
  check(bulk.method === 'POST' && bulk.body.ids.join(',') === '1,6', '批量请求丢失跨页选择');
  const tab = label => [...doc().querySelectorAll('[role=tab]')]
    .find(node => visible(node) && node.textContent.includes(label));
  const activateTab = async (label) => {
    const target = await wait(() => tab(label), `缺少${label}页签`);
    target.dispatchEvent(new frame.contentWindow.MouseEvent('mousedown', {bubbles:true, button:0}));
    await wait(() => target.getAttribute('aria-selected') === 'true', `${label}页签未激活`);
  };
  await activateTab('黑话');
  await wait(() => control.requests.some(item => item.path === '/jargon/list'),
    `黑话列表没有发请求：${JSON.stringify(control.requests)}`);
  await wait(() => text('合成黑话'), '黑话列表没有加载');
  await activateTab('表情');
  await wait(() => control.requests.some(item => item.path === '/emoji/list'),
    '表情列表没有发请求');
  await wait(() => find('[aria-label^="表情审批 · 未审"]'), () => JSON.stringify({
    text:doc().body.innerText.slice(0, 450), errors:control.errors,
    requests:control.requests.slice(-12),
  }));
  const approval = await wait(() => find('[aria-label^="表情审批 · 未审"]'), '表情卡缺少审批');
  const card = (approval.closest('article') || approval.closest('.relative.aspect-square')
    || approval.closest('[data-mobile-emoji-grid] > div')).getBoundingClientRect();
  const badge = approval.getBoundingClientRect();
  check(badge.left >= card.left + card.width / 2 && badge.top >= card.top + card.height / 2,
    '审批不在卡片右下');
  approval.click();
  (await wait(() => button('拒绝'), '缺少拒绝操作')).click();
  await wait(() => text('合成审批冲突'), '应展示审批失败');
  check(control.verdict === 'unreviewed', '失败不应改变审批状态');
  (await wait(() => button('通过'), '缺少通过操作')).click();
  await wait(() => find('[aria-label^="表情审批 · 已通过"]'), '未重新读取审批结果');
  check(control.requests.filter(item => item.path === '/emoji/1/review').length === 2,
    '审批请求数不符');
  check(!control.requests.some(item => item.path === '/emoji/1/register'), '审批不应登记');
  const currentTab = () => new URL(frame.contentWindow.location.href).searchParams.get('tab');
  check(currentTab() === 'emoji', '页签没有写入浏览器 URL');
  frame.contentWindow.history.back();
  await wait(
    () => currentTab() === 'jargon' && tab('黑话')?.getAttribute('aria-selected') === 'true',
    '后退没有恢复黑话页签');
  frame.contentWindow.history.forward();
  await wait(
    () => currentTab() === 'emoji' && tab('表情')?.getAttribute('aria-selected') === 'true',
    '前进没有恢复表情页签');
  await activateTab('表达方式');
  const more = await wait(() => find('[aria-label="更多操作"]'), '表达页缺少更多菜单');
  more.dispatchEvent(new frame.contentWindow.PointerEvent('pointerdown', {bubbles:true, button:0}));
  const qualityEntry = await wait(
    () => [...doc().querySelectorAll('[role=menuitem]')]
      .find(node => visible(node) && node.textContent.includes('表达质检记录')),
    '未提供表达质检记录入口');
  qualityEntry.click();
  await wait(() => text('合成质检说法 1'), '未调用 Aureus 质检记录接口');
  const qualityRequest = control.requests.find(
    item => item.path === '/api/resources/learning/quality');
  check(qualityRequest?.method === 'GET' && qualityRequest.query.limit === '20',
    '质检记录应使用有界 host 查询');
  const scope = await wait(() => find('select[aria-label="质检来源"]'), '质检记录缺少来源筛选');
  scope.value = 'synthetic-scope';
  scope.dispatchEvent(new frame.contentWindow.Event('change', {bubbles:true}));
  await wait(
    () => control.requests.some(item => item.path === '/api/resources/learning/quality'
      && item.query.scope === 'synthetic-scope'),
    '质检来源筛选未请求');
  const qualityPage = await wait(() => find('input[aria-label="质检记录跳页"]'), '质检列表无跳页');
  const setPage = Object.getOwnPropertyDescriptor(
    frame.contentWindow.HTMLInputElement.prototype, 'value').set;
  setPage.call(qualityPage, '2');
  qualityPage.dispatchEvent(new frame.contentWindow.Event('input', {bubbles:true}));
  (await wait(() => button('跳转'), '质检跳页按钮缺失')).click();
  await wait(
    () => control.requests.some(item => item.path === '/api/resources/learning/quality'
      && item.offset === 20),
    '质检记录没有翻页');
  await wait(() => text('合成质检说法 21'), '质检第二页未渲染');
  const firstPage = await wait(
    () => [...doc().querySelectorAll('button')]
      .find(node => visible(node) && node.textContent.includes('上一页')),
    '质检返回首页按钮缺失');
  firstPage.click();
  await wait(() => text('合成质检说法 1') && !text('合成质检说法 21'), '质检列表未回首页');
  (await wait(() => button('挽回预览'), '拒绝记录缺少挽回预览')).click();
  await wait(() => find('[aria-label="确认质检记录维护"]'), '挽回预览未打开');
  const rescuePreview = control.requests.find(
    item => item.path === '/api/resources/learning/quality/1');
  check(rescuePreview?.method === 'POST' && rescuePreview.body.action === 'rescued'
    && !rescuePreview.body.version, '挽回应先取得预览版本');
  (await wait(() => button('确认'), '挽回缺少二次确认')).click();
  await wait(
    () => control.requests.some(item => item.path === '/api/resources/learning/quality/1'
      && item.body?.version),
    '确认挽回没有携带预览版本');
  await wait(() => button('撤回挽回预览'), '挽回后缺少撤回');
  (await wait(() => button('撤回挽回预览'), '挽回后缺少撤回')).click();
  await wait(() => find('[aria-label="确认质检记录维护"]'), '撤回预览未打开');
  const withdrawal = control.requests
    .filter(item => item.path === '/api/resources/learning/quality/1').at(-1);
  check(withdrawal.body.action === 'withdrawn' && !withdrawal.body.version, '撤回应先预览');
  (await wait(() => button('确认'), '撤回缺少二次确认')).click();
  await wait(
    () => control.requests.some(item => item.path === '/api/resources/learning/quality/1'
      && item.body?.action === 'withdrawn' && item.body?.version),
    '撤回确认没有版本校验');
  return true;
})()"""


def test_workbench_product_entry_paging_selection_and_emoji_review(tmp_path: Path) -> None:
    with _open_browser(tmp_path) as browser:
        _run_workbench(browser)


def _run_workbench(browser: Browser) -> None:
    browser.command("Page.addScriptToEvaluateOnNewDocument", source=WORKBENCH_RESPONSES)
    browser.open("learning", width=1440, height=1000)
    assert browser.evaluate(INTERACTION) is True
