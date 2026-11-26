"""总览最近动态：缓存和网络结果显示同一份活动列表。"""

import pytest

from tests.web.test_live_foreground import foreground_clock as foreground_clock
from tests.web.test_page_script import run_page_script

SETUP = """
state.loggedIn = true; state.page = 'persona';
const gauge = el('div', {class:'ov-gauge'}); gauge.append($('ov-meter'));
attachOverviewField = () => ({});
const record = label => ({event_type:label, time:'2026-11-20T12:00:00Z'});
let reads = 0, result = () => reply(200, {records:[record('测试活动甲')]});
fetch = async (path, {method}) => {
  assert.equal(method, 'GET');
  if (path === '/api/timeline?hours=24&limit=3') { reads++; return result(); }
  return reply(200, {records:[]});
};
const labels = () => Array.from($('ov-activity').children,
  li => li.firstElementChild?.textContent ?? li.textContent);
"""


@pytest.mark.parametrize('empty', [False, True])
def test_prefetched_activity_on_entry(empty: bool) -> None:
    run_page_script(SETUP + ("result = () => reply(200, {records:[]});" if empty else '') + """
        await PAGE_PREFETCH.overview();
        const visit = showPage('overview');
        assert.deepEqual(labels(), [EXPECTED]);
        await visit; await advance(0);
        assert.deepEqual(labels(), [EXPECTED]); assert.equal(reads, 1);
    """.replace('EXPECTED', repr('近 24 小时没有动态' if empty else '测试活动甲')))


def test_return_with_expired_activity_then_replace() -> None:
    run_page_script(SETUP + """
        await showPage('overview'); await advance(0);
        assert.deepEqual(labels(), ['测试活动甲']); assert.equal(reads, 1);
        await showPage('timeline'); await advance(OVERVIEW_CACHE_MS + 1);
        let release; result = () => new Promise(resolve => { release = resolve; });
        $('ov-activity').replaceChildren();
        await showPage('overview');
        assert.deepEqual(labels(), ['测试活动甲']); assert.equal(reads, 2);
        release(reply(200, {records:[record('测试活动乙')]})); await advance(0);
        assert.deepEqual(labels(), ['测试活动乙']); assert.equal(reads, 2);
    """)


def test_cold_activity_failure_is_not_empty() -> None:
    run_page_script(SETUP + """
        result = () => reply(503, {});
        await showPage('overview'); await advance(0);
        assert.deepEqual(labels(), ['暂时无法读取动态']); assert.equal(reads, 1);
    """)
