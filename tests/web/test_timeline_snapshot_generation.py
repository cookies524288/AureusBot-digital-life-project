"""时间线变更前的慢读取不能作为变更后的页面或预取快照。"""

import pytest

from tests.web.test_live_foreground import foreground_clock as foreground_clock
from tests.web.test_page_script import run_page_script

SETUP = """
state.loggedIn = true;
$('tl-hours').value = '24';
openSocket();
const notify = topic => sockets[0].onmessage({data:JSON.stringify({type:'event', topic})});
const query = 'hours=24&limit=100', url = '/api/timeline?' + query;
const shown = () => $('tl-summary').children[0]?.children[1].textContent;
const cached = () => cacheRead('timeline:' + query)?.data.summary.sample;
const requests = [], pending = [];
let version = '旧';
fetch = async (path, {method}) => {
  requests.push(method === 'GET' ? path : method + ' ' + path);
  const data = path === '/api/overview' ? {version} :
    {records:[], summary:{sample:version}, next_before_id:null};
  if (version === '旧') await new Promise(resolve => pending.push({path, resolve}));
  return reply(200, data);
};
function changeTimeline() { version = '新'; notify('timeline'); }
function release(path) {
  for (const read of pending) if (!path || read.path === path) read.resolve();
}
"""


def test_foreground_refresh_does_not_share_the_pre_event_read() -> None:
    run_page_script(SETUP + """
        state.page = 'timeline';
        const old = loadTimeline(false);
        changeTimeline(); await advance(200); changeTimeline(); await advance(249);
        assert.deepEqual(requests, [url]);
        await advance(1);
        const during = shown();
        release(); await old; await advance(0);
        assert.deepEqual([requests, during, shown(), cached()], [[url, url], '新', '新', '新']);
    """)


def test_hidden_timeline_discards_old_page_read_until_foreground() -> None:
    run_page_script(SETUP + """
        state.page = 'timeline';
        const old = loadTimeline(false);
        hidden(true); changeTimeline(); await advance(500);
        release(); await old;
        assert.deepEqual([requests, shown(), cached()], [[url], undefined, undefined]);
        hidden(false); await advance(0);
        assert.deepEqual([requests, shown(), cached()], [[url, url], '新', '新']);
    """)


@pytest.mark.parametrize('old_first', [True, False], ids=['before-entry', 'after-new-page'])
def test_off_page_prefetch_never_restores_invalidated_cache(old_first: bool) -> None:
    run_page_script(SETUP + """
        state.page = 'persona';
        const old = PAGE_PREFETCH.timeline();
        changeTimeline(); await advance(500);
        assert.deepEqual(requests, [url]);
        if (OLD_FIRST) { release(); await old; }
        const visit = showPage('timeline'); await advance(0);
        const during = shown();
        release(); await Promise.all([old, visit]);
        assert.deepEqual([requests, during, shown(), cached()], [[url, url], '新', '新', '新']);
    """.replace('OLD_FIRST', 'true' if old_first else 'false'))


def test_unrelated_event_keeps_same_snapshot_shared_across_navigation() -> None:
    run_page_script(SETUP + """
        state.page = 'persona';
        const prefetch = PAGE_PREFETCH.timeline();
        notify('round'); await advance(250);
        const visit = showPage('timeline');
        const again = loadTimeline(false);
        release(); await Promise.all([prefetch, visit, again]);
        assert.deepEqual([requests, shown(), cached()], [[url], '旧', '旧']);
    """)


def test_completed_overview_is_not_replayed_by_slow_timelines() -> None:
    run_page_script(SETUP + """
        state.page = 'persona';
        const overview = '/api/overview';
        const round = '/api/timeline?hours=24&limit=60';
        const paths = [overview, round];
        const snapshot = () => [cacheRead('overview')?.data.version,
          cacheRead('overview-activity')?.data.summary.sample,
          cacheRead('overview-round')?.data.summary.sample];
        const old = PAGE_PREFETCH.overview();
        release(overview); await advance(0);
        const early = snapshot();
        changeTimeline(); await advance(500);
        await PAGE_PREFETCH.overview();
        const fresh = snapshot();
        release(); await old;
        assert.deepEqual(requests, paths.concat(paths));
        assert.deepEqual(fresh, ['新', '新', '新']);
        assert.deepEqual(snapshot(), fresh);
        assert.deepEqual(early, ['旧', undefined, undefined]);
    """)


@pytest.mark.parametrize('page', ['overview', 'monitor'])
def test_aggregate_prefetch_shares_only_unaffected_reads_across_event(page: str) -> None:
    run_page_script(SETUP + """
        state.page = 'persona';
        const page = 'PAGE', overview = '/api/overview';
        const old = PAGE_PREFETCH[page]();
        const initial = requests.slice();
        changeTimeline(); await advance(500);
        assert.deepEqual(requests, initial);
        const fresh = PAGE_PREFETCH[page]();
        release(overview); await advance(0);
        release(); await Promise.all([old, fresh]);
        const timeline = initial.filter(path => path.startsWith('/api/timeline?'));
        assert.deepEqual(requests, initial.concat(timeline));
        if (page === 'overview') {
          assert.equal(cacheRead('overview').data.version, '旧');
          for (const key of ['overview-activity', 'overview-round']) {
            assert.equal(cacheRead(key).data.summary.sample, '新');
          }
        } else {
          const data = cacheRead('monitor').data;
          assert.ok(data.ov.ok); assert.equal(data.ov.data.version, '旧');
          for (const key of ['hormones', 'recent', 'persona']) {
            assert.ok(data[key].ok); assert.equal(data[key].data.summary.sample, '新');
          }
        }
    """.replace("'PAGE'", repr(page)))
