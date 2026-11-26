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


@pytest.mark.parametrize(('event', 'old_first'), [
    ('inbox', True), ('inbox', False), ('reconnect', False), ('round', False),
])
def test_inbox_list_snapshot(event: str, old_first: bool) -> None:
    run_page_script(RECONNECT + """
        const event = EVENT, oldFirst = OLD_FIRST, paths = [], url = '/api/inbox/conversations';
        const key = {kind:'group', platform:'qq', account_id:'10', recipient_id:'1'};
        let release;
        fetch = async (path, {method}) => {
          paths.push(path); assert.equal(method, 'GET');
          if (path === '/api/status') return reply(200, {});
          assert.equal(path, url);
          const name = release ? '新名' : '旧名';
          if (!release) await new Promise(resolve => { release = resolve; });
          return reply(200, {enabled:true, conversations:[{name, key}]});
        };
        state.loggedIn = true; state.page = 'persona'; inbox.view = 'qq';
        inbox.selected = {key, name:'初始'};
        openSocket(); await sockets[0].onopen();
        const old = PAGE_PREFETCH.inbox();
        if (event === 'reconnect') await reconnect();
        else sockets[0].onmessage({data:JSON.stringify({type:'event', topic:event})});
        await advance(500);
        const initial = event === 'reconnect' ? [url, '/api/status'] : [url];
        assert.deepEqual(paths, initial);
        if (oldFirst) {
          release(); await old;
          assert.equal(cacheRead('inbox-conversations'), null);
        }
        const visit = showPage('inbox'); await advance(0);
        const expected = event === 'round' ? '旧名' : '新名';
        assert.deepEqual(paths, event === 'round' ? initial : [...initial, url]);
        if (event !== 'round') assert.equal($('inbox-thread-name').textContent, expected);
        release(); await Promise.all([old, visit]);
        assert.equal($('inbox-thread-name').textContent, expected);
        assert.equal(cacheRead('inbox-conversations').data.conversations[0].name, expected);
        assert.deepEqual(paths, event === 'round' ? initial : [...initial, url]);
    """.replace('EVENT', repr(event)).replace('OLD_FIRST', 'true' if old_first else 'false'))


def test_late_foreground_list_does_not_start_another_message_refresh() -> None:
    run_page_script("""
        const url = '/api/inbox/conversations', paths = [];
        const key = {kind:'group', platform:'qq', account_id:'10', recipient_id:'1'};
        let release;
        state.loggedIn = true; state.page = 'inbox'; inbox.view = 'qq';
        inbox.selected = {key, name:'初始'};
        fetch = async path => {
          paths.push(path);
          if (path !== url) return reply(200, {messages:[], next_before_id:null});
          const name = release ? '新名' : '旧名';
          if (!release) await new Promise(resolve => { release = resolve; });
          return reply(200, {enabled:true, conversations:[{name, key}]});
        };
        openSocket(); await sockets[0].onopen(); await advance(0);
        paths.length = 0;  // 只计推送，不计连接建立时跟随输入状态的查询。
        const notify = () => sockets[0].onmessage({data:'{"type":"event","topic":"inbox"}'});
        notify(); await advance(250); notify(); await advance(250);
        const messages = `${inboxMessageURL()}?${inboxPageQuery(null, null)}`;
        assert.deepEqual(paths, [url, url, messages]);
        release(); await advance(0);
        assert.deepEqual(paths, [url, url, messages]);
        assert.equal($('inbox-thread-name').textContent, '新名');
    """)
