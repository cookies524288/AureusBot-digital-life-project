"""用页面实际注册的事件和定时器检查前后台刷新。"""

import pytest

from tests.web import test_page_script
from tests.web.test_page_script import run_page_script


@pytest.fixture(autouse=True)
def foreground_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """给既有 Node 页面夹具安装手动时钟与文档事件分发。"""
    marker = "  vm.runInContext(input.html.match"
    setup = """
      vm.runInContext(`
        installResourcePages = () => () => {};
        const listeners = [], jobs = new Map(); let now = 0;
        globalThis.performance = {now: () => now};
        document.addEventListener = (name, fn) => {
          if (name === 'visibilitychange') listeners.push(fn);
        };
        function hidden(value) { document.hidden = value; listeners.forEach(fn => fn()); }
        function schedule(fn, ms, repeat) {
          const job = {fn, at: now + ms, repeat}; jobs.set(job, job); return job;
        }
        setTimeout = (fn, ms) => schedule(fn, ms, 0);
        setInterval = (fn, ms) => schedule(fn, ms, ms);
        clearTimeout = job => jobs.delete(job);
        async function advance(ms) {
          const end = now + ms;
          while (true) {
            const next = [...jobs.values()].sort((a,b) => a.at-b.at)[0];
            if (!next || next.at > end) break;
            now = next.at;
            if (next.repeat) next.at += next.repeat; else jobs.delete(next);
            next.fn(); await new Promise(setImmediate);
          }
          now = end; await new Promise(setImmediate);
        }
      `, context);
    """
    monkeypatch.setattr(
        test_page_script,
        "HARNESS",
        test_page_script.HARNESS.replace(
            marker,
            setup + marker,
        ),
    )


@pytest.mark.parametrize("elapsed", [250, 1])
def test_hidden_timeline(elapsed: int) -> None:
    run_page_script(
        """
        state.loggedIn = true; state.page = 'timeline'; openSocket();
        const event = topic => sockets[0].onmessage({data:JSON.stringify({type:'event', topic})});
        let reads = 0;
        fetch = async path => {
          assert.match(path, /^\\/api\\/timeline\\?/); reads++;
          return reply(200, {records:[], summary:{test:String(reads)}, next_before_id:null});
        };
        await loadTimeline(false);
        const shown = () => $('tl-summary').children[0].children[1].textContent;
        assert.equal(shown(), '1');
        hidden(true); event('timeline'); event('timeline'); await advance(ELAPSED);
        assert.equal(reads, 1); assert.equal(shown(), '1');
        hidden(false); await advance(0);
        assert.equal(shown(), '2'); assert.equal(reads, 2);
        event('resources'); hidden(true); hidden(false); await advance(500);
        assert.equal(reads, 2);
        event('timeline'); await advance(200); event('timeline'); await advance(249);
        assert.equal(reads, 2); await advance(1); assert.equal(reads, 3);
    """.replace("ELAPSED", str(elapsed))
    )


@pytest.mark.parametrize(
    "leave",
    [
        "await showPage('monitor')",
        "showAuth('login'); state.loggedIn = true",
    ],
)
def test_pending_does_not_follow_navigation_or_login(leave: str) -> None:
    run_page_script(
        """
        state.loggedIn = true; state.page = 'timeline'; openSocket();
        const paths = [];
        fetch = async path => { paths.push(path); return reply(503, {}); };
        sockets[0].onmessage({data:JSON.stringify({type:'event', topic:'timeline'})});
        await advance(10); hidden(true);
        LEAVE;
        const before = paths.slice(); hidden(false); await advance(500);
        assert.deepEqual(paths, before);
    """.replace("LEAVE", leave)
    )


def test_monitor_suspends_and_catches_up_once() -> None:
    run_page_script("""
        state.loggedIn = true; state.page = 'monitor'; openSocket();
        const paths = [];
        fetch = async path => { paths.push(path); return reply(503, {}); };
        hidden(true); await advance(20000); assert.deepEqual(paths, []);
        sockets[0].onmessage({data:JSON.stringify({type:'event', topic:'timeline'})});
        hidden(false); await advance(9999);
        assert.equal(paths.length, 4); assert.equal(new Set(paths).size, 4);
        await advance(1); assert.deepEqual(paths.slice(4), paths.slice(0,4));
    """)


def test_timeline_since_joins_the_existing_round_without_replacing_focused_rows() -> None:
    run_page_script(
        """
        state.loggedIn = true; state.page = 'timeline'; openSocket();
        const row = id => ({id, round_id:1, actor:'me', event_type:'judgment',
          time:'2026-11-27T00:00:00Z', visibility:'private', content:'合成正文'.repeat(100)});
        let release;
        const queries = [];
        fetch = async path => {
          const query = new URLSearchParams(path.split('?')[1]);
          const cursor = query.get('since_id'); queries.push(cursor);
          if (cursor === null) await new Promise(resolve => release = resolve);
          const id = cursor === null ? 1 : 2;
          return reply(200, {records:[row(id)], since_id:id,
            has_more:false, next_before_id:cursor === null ? 1 : null, context:{boundary_id:null}});
        };
        const snapshot = loadTimeline(false);
        await advance(0);
        sockets[0].onmessage({data:JSON.stringify({type:'event', topic:'timeline', data:{id:2}})});
        await advance(250);
        assert.deepEqual(queries, [null]);
        release(); await snapshot;
        const firstGroup = $('tl-list').children[0], firstRow = firstGroup.children[1];
        firstRow.focus();
        await advance(0);
        assert.deepEqual(queries, [null, '1']);
        assert.equal($('tl-list').children[0], firstGroup);
        assert.equal(firstGroup.children[1], firstRow);
        assert.equal(firstGroup.children[2].dataset.recordId, '2');
        assert.equal(document.activeElement, firstRow);
        assert.equal(tl.next, 1);
    """,
        include_resource_pages=False,
    )


def test_connected_overview_and_typing_do_not_poll() -> None:
    run_page_script(
        """
        WebSocket.OPEN = 1;
        state.loggedIn = true; state.page = 'overview'; openSocket();
        const ws = sockets[0]; ws.readyState = 1; ws.onopen();
        function event(topic, data) {
          ws.onmessage({data:JSON.stringify({type:'event', topic, data})});
        }
        const paths = [];
        fetch = async path => {
          paths.push(path);
          if (path === '/api/overview') return reply(200, {thinking:false});
          if (path.startsWith('/api/timeline?')) {
            return reply(200, {records:[], since_id:10, has_more:false});
          }
          if (path === '/api/chat/typing') return reply(200, {typing:false});
          throw Error(path);
        };
        renderOverview = () => {};
        await loadOverview(); await advance(30000);
        assert.deepEqual(paths, ['/api/overview', '/api/timeline?hours=24&limit=60']);
        event('timeline', {id:11}); event('round', {active:true, web:true}); await advance(1999);
        assert.equal(paths.length, 2);
        await advance(1);
        assert.deepEqual(paths.slice(2), [
          '/api/overview', '/api/timeline?hours=24&limit=60&since_id=10',
        ]);
        clearLiveTimers(); state.page = 'chat';
        followConversationTyping();
        assert.equal(typingSession.wasActive, true);
        event('round', {active:false}); await advance(30000);
        assert.equal(typingSession.wasActive, false);
        assert.equal(paths.length, 4);
        ws.onclose();
        reconnectLater = () => {};
        await advance(2999);
        assert.equal(paths.length, 4);
        await advance(1);
        assert.equal(paths.filter(path => path === '/api/chat/typing').length, 1);
        hidden(true); const after = paths.length; await advance(30000);
        assert.equal(paths.length, after);
    """,
        include_resource_pages=False,
    )


def test_inbox_events_refresh_only_current_messages_and_retain_the_activity_cursor() -> None:
    run_page_script(
        """
        WebSocket.OPEN = 1;
        state.loggedIn = true; state.page = 'inbox'; openSocket();
        const ws = sockets[0]; ws.readyState = 1; ws.onopen();
        const key = recipient_id => ({platform:'qq', account_id:'fixture', kind:'group',
          recipient_id, temporary_group_id:null});
        inbox.selected = {key:key('A')};
        const paths = [];
        let cursor = 32, summaries = 0, messages = 0;
        loadInbox = async () => { summaries++; };
        refreshInboxMessages = async () => { messages++; };
        fetch = async path => {
          paths.push(path);
          return reply(200, path.startsWith('/api/inbox/typing') ? {typing:false} :
            {records:[], since_id:cursor, has_more:false});
        };
        startInboxActivity(inbox.selected); await advance(30000);
        assert.equal(paths.length, 1);
        const event = conversation => ws.onmessage({
          data:JSON.stringify({type:'event', topic:'inbox', data:{conversation}}),
        });
        cursor = 33; event(key('B')); await advance(250);
        assert.deepEqual([summaries, messages], [1, 0]);
        assert.match(paths.at(-1), /since_id=32$/);
        hidden(true); event(key('A')); event(key('B')); await advance(30000);
        assert.deepEqual([summaries, messages, paths.length], [1, 0, 2]);
        hidden(false); await advance(0);
        assert.deepEqual([summaries, messages], [2, 1]);
        assert.equal(paths.filter(path => path.endsWith('since_id=33')).length, 1);
        const query = new URLSearchParams(paths[0].split('?')[1]);
        assert.deepEqual(JSON.parse(query.get('conversation')), key('A'));
    """,
        include_resource_pages=False,
    )
