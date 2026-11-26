"""真重连后的时间线不能使用断开前的读取。"""

import pytest

from tests.web.test_live_foreground import foreground_clock as foreground_clock
from tests.web.test_page_script import run_page_script

RECONNECT = """
async function reconnect() {
  sockets[0].onclose(); await advance(3000);
  assert.equal(sockets.length, 2);
  await sockets[1].onopen(); await advance(0);
}
"""


@pytest.mark.parametrize('foreground', [True, False])
def test_timeline_reconnect(foreground: bool) -> None:
    run_page_script(RECONNECT + """
        const front = FRONT, paths = [], query = 'hours=24&limit=100';
        const url = '/api/timeline?' + query;
        let release;
        fetch = async path => {
          paths.push(path);
          if (path === '/api/status') return reply(200, {});
          const sample = release ? '新' : '旧';
          if (!release) await new Promise(resolve => { release = resolve; });
          return reply(200, {records:[], summary:{sample}, next_before_id:null});
        };
        state.loggedIn = true; state.page = front ? 'timeline' : 'persona';
        $('tl-hours').value = '24'; openSocket(); await sockets[0].onopen();
        assert.deepEqual(paths, []);
        const old = front ? loadTimeline(false) : PAGE_PREFETCH.timeline();
        await reconnect();
        if (!front) {
          assert.deepEqual(paths, [url, '/api/status']);
          release(); await old;
          assert.equal(cacheRead('timeline:' + query), null);
          await showPage('timeline');
        }
        release(); await old; await advance(0);
        assert.deepEqual(paths, [url, '/api/status', url]);
        assert.equal($('tl-summary').children[0].children[1].textContent, '新');
        assert.equal(cacheRead('timeline:' + query).data.summary.sample, '新');
    """.replace('FRONT', 'true' if foreground else 'false'))
