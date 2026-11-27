function check(condition, description) {
  if (!condition) throw Error(description);
}

function shown(selector) {
  const node = document.querySelector(selector);
  return node?.getClientRects().length ? node : null;
}

async function waitFor(predicate, description) {
  const deadline = performance.now() + 9000;
  while (performance.now() < deadline) {
    const value = await predicate();
    if (value) return value;
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  throw Error(description);
}

function paintOpportunity() {
  return new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
}

function resources(path) {
  return performance.getEntriesByType('resource').filter(entry => new URL(entry.name).pathname === path);
}

async function fixtureAction(path, action) {
  const response = await fetch('/__fixture/' + path, {method:'POST', headers:{'X-UI364-Action':action}});
  check(response.ok, `合成操作失败：${path} HTTP ${response.status}`);
  return response.json();
}

async function measureCold() {
  await waitFor(() => shown('#page-overview.active') && shown('#ov-version')?.textContent &&
    resources('/api/overview').length, '总览没有显示');
  const navigation = performance.getEntriesByType('navigation')[0];
  const overview = resources('/api/overview')[0];
  const status = resources('/api/status')[0];
  const scripts = performance.getEntriesByType('resource').filter(entry => new URL(entry.name).pathname.endsWith('.js'))
    .map(entry => ({path:new URL(entry.name).pathname, start_ms:entry.startTime, end_ms:entry.responseEnd,
      decoded_bytes:entry.decodedBodySize}));
  return {ttfb_ms:navigation.responseStart, dom_content_loaded_ms:navigation.domContentLoadedEventEnd,
    overview_response_end_ms:overview.responseEnd, document_bytes:navigation.decodedBodySize,
    status_request_start_ms:status.startTime, scripts: scripts,
    overview_initial_timeline_reads:resources('/api/timeline').length};
}

function chatRows() {
  return [...document.querySelectorAll('#chat-log > .msg')];
}

function scrollAnchor(log) {
  const top = log.getBoundingClientRect().top;
  const row = chatRows().find(node => node.getBoundingClientRect().bottom > top);
  return {row, before:row.getBoundingClientRect().top - top,
    id:row.querySelector('.bubble').textContent.match(/#\d+/)[0]};
}

function awaitLastReply(log, identifier) {
  return new Promise((resolve, reject) => {
    const observer = new MutationObserver(() => {
      const last = log.lastElementChild?.querySelector('.bubble')?.textContent;
      if (!last?.includes('#' + identifier)) return;
      clearTimeout(timer);
      observer.disconnect();
      resolve(performance.now());
    });
    const timer = setTimeout(() => {
      observer.disconnect();
      reject(Error(`收不到合成回复 #${identifier}`));
    }, 5000);
    observer.observe(log, {childList:true, subtree:true});
  });
}

async function measureLive(count) {
  const log = document.querySelector('#chat-log');
  const incremental = log.hasAttribute('data-incremental') || typeof state.incrementalUpdates === 'boolean' && state.incrementalUpdates;
  const expected = incremental ? 500 : 1000;
  await waitFor(() => chatRows().length === expected && document.querySelector('#conn-text').textContent === '已连接',
    `压力场景初次未见 ${expected} 条（实际 ${chatRows().length} 条）`);
  await waitFor(() => window.fixturePendingHistory() === 0 &&
    resources('/api/history').length >= (incremental ? 1 : 2), '首次连接的历史补读未结束');
  await paintOpportunity();
  log.scrollTop = Math.round(log.scrollHeight / 2);
  const anchor = scrollAnchor(log);
  const original = chatRows();
  const originalSet = new Set(original);
  const beforeTop = log.scrollTop;
  const historyReads = resources('/api/history').length;
  const initialCursor = state.loaded.chatCursor;
  const typingReads = resources('/api/chat/typing').length;
  const initialNodes = log.childElementCount;
  const mutations = [];
  const recorder = new MutationObserver(records => mutations.push(...records));
  recorder.observe(log, {childList:true});
  const events = [];
  for (let identifier = 1001; identifier < 1001 + count; identifier++) {
    const arrives = awaitLastReply(log, identifier);
    const started = performance.now();
    const event = await fixtureAction('chat', 'append-synthetic-reply');
    check(event.id === identifier, `合成回复顺序不对：${event.id}`);
    const inserted = await arrives;
    await paintOpportunity();
    events.push({id:identifier, append_ms:inserted - started, paint_opportunity_ms:performance.now() - started,
      message_nodes:chatRows().length, direct_nodes:log.childElementCount,
      anchor_delta_px:(anchor.row.getBoundingClientRect().top - log.getBoundingClientRect().top) - anchor.before});
  }
  recorder.disconnect();
  const removed = mutations.flatMap(change => [...change.removedNodes]);
  const added = mutations.flatMap(change => [...change.addedNodes]);
  const moved = added.filter(node => originalSet.has(node)).length;
  const retained = original.filter(node => node.parentElement === log).length;
  const newReads = resources('/api/history').slice(historyReads);
  const result = {initial_messages:original.length, initial_direct_nodes:initialNodes,
    initial_scroll_top_px:beforeTop, final_scroll_top_px:log.scrollTop, anchor:anchor.id,
    anchor_retained:anchor.row.parentElement === log, original_messages_retained:retained,
    original_messages_moved:moved, removed_message_nodes:removed.filter(node => node.matches?.('.msg')).length,
    added_message_nodes:added.filter(node => node.matches?.('.msg')).length,
    new_history_requests:newReads.map(entry => new URL(entry.name).search),
    new_typing_requests:resources('/api/chat/typing').length - typingReads, events};
  check(result.anchor_retained && events.every(event => Math.abs(event.anchor_delta_px) <= 1),
    `连续更新没有保持可见消息位置：${incremental ? 'candidate' : 'baseline'} ${JSON.stringify({
      anchor:result.anchor, retained:result.anchor_retained, beforeTop, finalTop:log.scrollTop,
      offsets:events.map(event => event.anchor_delta_px),
    })}`);
  check(result.new_history_requests.length === 0,
    `连续更新发生历史读取：${incremental ? 'candidate' : 'baseline'} ${JSON.stringify({
      initialCursor, finalCursor:state.loaded.chatCursor, before:historyReads,
      requests:result.new_history_requests,
    })}`);
  check(result.original_messages_moved === 0 && result.added_message_nodes === count, '记录节点被整批重排');
  if (incremental) {
    check(events.every(event => event.message_nodes === expected), '聊天节点超过上限');
    check(result.removed_message_nodes === count && retained === expected - count, '未逐条修剪最旧消息');
  } else {
    check(events.at(-1).message_nodes === expected + count, '基线没有显示全部合成消息');
  }
  return result;
}

async function measureBackfill() {
  const log = document.querySelector('#chat-log');
  await waitFor(() => chatRows().length === 50 && shown('#chat-older'), '标准 50 条历史未加载');
  await paintOpportunity();
  log.scrollTop = Math.round(log.scrollHeight / 3);
  const anchor = scrollAnchor(log);
  const previous = resources('/api/history').length;
  shown('#chat-older').click();
  await waitFor(() => chatRows().length === 100, '更早的消息未前插');
  await paintOpportunity();
  const offset = anchor.row.getBoundingClientRect().top - log.getBoundingClientRect().top - anchor.before;
  const reads = resources('/api/history').slice(previous).map(entry => new URL(entry.name).search);
  check(reads.length === 1 && reads[0] === '?before_id=951', '向前分页没有使用正确游标');
  check(anchor.row.parentElement === log && Math.abs(offset) <= 1, '向前分页没有保留滚动位置');
  return {anchor:anchor.id, anchor_delta_px:offset, message_nodes:chatRows().length, history_requests:reads};
}

async function measureTimeline() {
  const list = document.querySelector('#tl-list');
  async function fixtureState() {
    const response = await fetch('/__fixture');
    check(response.ok, `无法读合成场景状态：HTTP ${response.status}`);
    return response.json();
  }
  await waitFor(() => list.querySelector('.tl-item .tl-body-toggle') &&
    document.querySelector('#conn-text').textContent === '已连接', '时间线未就绪');
  const before = list.querySelector('.tl-item .tl-body-toggle');
  const row = before.closest('.tl-item');
  before.focus();
  before.click();
  const start = resources('/api/timeline').length;
  const first = await fixtureAction('push', 'timeline');
  const byId = id => list.querySelector(`[data-record-id="${id}"]`);
  await waitFor(() => byId(first.id), '时间线没有按游标追到事件');
  const incrementalReads = resources('/api/timeline').slice(start).map(entry => new URL(entry.name).search);
  check(incrementalReads.length === 1 && new URLSearchParams(incrementalReads[0]).get('since_id') === String(first.id - 1),
    '时间线没有按上一条序号增量读取');
  check(row.isConnected && document.activeElement === before && before.getAttribute('aria-expanded') === 'true',
    '实时追加抢走了焦点或折叠了正文');
  await fixtureAction('slow-timeline', 'hold-first-page');
  const beforeFilter = resources('/api/timeline').length;
  const hours = document.querySelector('#tl-hours');
  hours.value = '6';
  hours.dispatchEvent(new Event('change'));
  await waitFor(async () => (await fixtureState()).timeline_in_flight, '没有发生受控慢读');
  const second = await fixtureAction('push', 'timeline');
  await waitFor(() => byId(second.id), '未补齐时间线');
  const visibleBeforeOldReadFinished = (await fixtureState()).timeline_in_flight;
  check(visibleBeforeOldReadFinished, '新一代被失效的慢读阻塞');
  const filterReads = () => resources('/api/timeline').slice(beforeFilter).map(entry => new URL(entry.name).search);
  await waitFor(() => filterReads().length === 2, '没有等到迟到的旧响应');
  await paintOpportunity();
  const finalIds = [...list.querySelectorAll('.tl-item')].map(node => Number(node.dataset.recordId));
  check(finalIds.filter(id => id === second.id).length === 1, '迟到响应覆盖或重复新记录');
  check(filterReads().every(query => query === '?hours=6&limit=100'), '筛选首读没有换用正确时间范围');
  return {retained_focus:true, retained_expanded_body:true, incremental_requests:incrementalReads,
    visible_before_old_read_finished:visibleBeforeOldReadFinished, final_record_ids:finalIds,
    slow_snapshot_then_new_id:second.id, filter_requests:filterReads()};
}
