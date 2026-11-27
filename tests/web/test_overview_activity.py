"""总览最近动态：缓存和网络结果显示同一份活动列表。"""

import pytest

from tests.web.test_live_foreground import foreground_clock as foreground_clock
from tests.web.test_page_script import run_page_script

SETUP = """
state.loggedIn = true; state.page = 'persona';
const gauge = el('div', {class:'ov-gauge'}); gauge.append($('ov-meter'));
attachOverviewField = () => ({});
const record = (id, label) => ({id, event_type:label, time:'2026-11-20T12:00:00Z'});
const snapshot = (records, since_id) => reply(200, {records, since_id, has_more:false});
const query = '/api/timeline?hours=24&limit=60';
const reads = [];
let result = () => snapshot([record(10, '测试活动甲')], 10);
fetch = async (path, {method}) => {
  assert.equal(method, 'GET');
  if (path === '/api/overview') return reply(200, {});
  if (path === query || path.startsWith(query + '&since_id=')) { reads.push(path); return result(); }
  if (path.startsWith('/api/timeline?') && path.includes('limit=100')) return snapshot([], 0);
  throw Error('不应另发活动请求：' + path);
};
const labels = () => Array.from($('ov-activity').children,
  li => li.firstElementChild?.textContent ?? li.textContent);
"""


@pytest.mark.parametrize('empty', [False, True])
def test_prefetched_activity_on_entry(empty: bool) -> None:
    run_page_script(SETUP + ("result = () => snapshot([], 10);" if empty else '') + """
        await PAGE_PREFETCH.overview();
        const visit = showPage('overview');
        assert.deepEqual(labels(), [EXPECTED]);
        await visit; await advance(0);
        assert.deepEqual(labels(), [EXPECTED]); assert.deepEqual(reads, [query]);
    """.replace('EXPECTED', repr('近 24 小时没有动态' if empty else '测试活动甲')))


def test_return_with_expired_activity_then_replace() -> None:
    run_page_script(SETUP + """
        await showPage('overview'); await advance(0);
        assert.deepEqual(labels(), ['测试活动甲']); assert.deepEqual(reads, [query]);
        await showPage('timeline'); await advance(OVERVIEW_CACHE_MS + 1);
        let release; result = () => new Promise(resolve => { release = resolve; });
        $('ov-activity').replaceChildren();
        const visit = showPage('overview'); await advance(0);
        const expectedReads = [query, query + '&since_id=10'];
        assert.deepEqual(labels(), ['测试活动甲']); assert.deepEqual(reads, expectedReads);
        release(snapshot([record(11, '测试活动乙'), record(12, '测试活动丙'), record(13, '测试活动丁')], 13));
        await visit; await advance(0);
        assert.deepEqual(labels(), ['测试活动丁', '测试活动丙', '测试活动乙']);
        assert.deepEqual(reads, expectedReads);
    """)


def test_cold_activity_failure_is_not_empty() -> None:
    run_page_script(SETUP + """
        result = () => reply(503, {detail:'合成动态服务不可用'});
        await showPage('overview'); await advance(0);
        assert.deepEqual(labels(), ['暂时无法读取动态']); assert.deepEqual(reads, [query]);
        assert.deepEqual(Array.from($('ov-activity-flow').children, node => node.textContent), ['暂时无法读取活动']);
        assert.equal($('toasts').firstElementChild.textContent, '合成动态服务不可用');
        result = () => snapshot([], 0);
        await refreshOverviewLive();
        assert.deepEqual(labels(), ['近 24 小时没有动态']);
        assert.deepEqual(reads, [query, query]);
    """)
