"""用页面实际注册的事件和定时器检查前后台刷新。"""

import pytest

from tests.web import test_page_script
from tests.web.test_page_script import run_page_script


@pytest.fixture(autouse=True)
def foreground_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """给既有 Node 页面夹具安装手动时钟与文档事件分发。"""
    marker = '  vm.runInContext(input.html.match'
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
    monkeypatch.setattr(test_page_script, 'HARNESS', test_page_script.HARNESS.replace(
        marker, setup + marker,
    ))


@pytest.mark.parametrize('elapsed', [250, 1])
def test_hidden_timeline(elapsed: int) -> None:
    run_page_script("""
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
    """.replace('ELAPSED', str(elapsed)))


@pytest.mark.parametrize('leave', [
    "await showPage('monitor')",
    "showAuth('login'); state.loggedIn = true",
])
def test_pending_does_not_follow_navigation_or_login(leave: str) -> None:
    run_page_script("""
        state.loggedIn = true; state.page = 'timeline'; openSocket();
        const paths = [];
        fetch = async path => { paths.push(path); return reply(503, {}); };
        sockets[0].onmessage({data:JSON.stringify({type:'event', topic:'timeline'})});
        await advance(10); hidden(true);
        LEAVE;
        const before = paths.slice(); hidden(false); await advance(500);
        assert.deepEqual(paths, before);
    """.replace('LEAVE', leave))


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
