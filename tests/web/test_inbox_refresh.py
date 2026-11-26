"""收件箱首屏预取：只用完整刷新替换缓存。"""

import pytest

from tests.web import test_page_script
from tests.web.test_page_script import run_page_script


@pytest.fixture(autouse=True)
def isolate_resource_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟 DOM 不解析资源页表单；该页与收件箱无关。"""
    marker = "  vm.runInContext(input.html.match"
    stub = "  vm.runInContext('installResourcePages = () => () => {}', context);\n"
    monkeypatch.setattr(test_page_script, "HARNESS", test_page_script.HARNESS.replace(
        marker, stub + marker,
    ))


SETUP = """
let now = 0;
globalThis.performance = {now:() => now};
state.loggedIn = true;
state.page = 'inbox';
inbox.selected = {archive:'imported'};
const key = 'inbox-messages:imported';
const page = (ids, next = null) => reply(200, {messages:ids.map(id => ({
  id, source:'imported', content:String(id), attachments:[],
})), next_before_id:next});
const error = reply(500, {detail:'历史暂不可用'});
const answers = [], cursors = [];
fetch = async path => {
  if (path === '/api/status') return reply(401, {});
  cursors.push(new URLSearchParams(path.split('?')[1]).get('before_id'));
  return answers.shift();
};
const rows = () => Array.from($('inbox-messages').children,
  row => row.children[1].children[1].children[0].textContent).join(',');
const retry = $('inbox-history-retry');
const seed = async () => {
  answers.push(page([4,3], 3), page([2,1]));
  await loadInboxMessages();
  assert.equal(rows(), '1,2,3,4');
  now += INBOX_MESSAGE_CACHE_MS + 1;
  cursors.length = 0;
};
"""


@pytest.mark.parametrize("first_page_failed", [True, False])
def test_cached_refresh_retries_from_latest(first_page_failed: bool) -> None:
    failure = (
        "answers.push(error);"
        if first_page_failed
        else "answers.push(page([6,5], 5), error);"
    )
    run_page_script(SETUP + "await seed();" + failure + """
        await loadInboxMessages();
        assert.equal(rows(), '1,2,3,4');
        assert.equal(pageCache.get(key).at, 0);
        assert.equal(retry.classList.contains('hidden'), false);
        assert.match($('inbox-history-text').textContent, /历史暂不可用/);
        answers.push(error);
        cursors.length = 0;
        await loadInboxMessages();
        assert.deepEqual(cursors, [null]);
        answers.push(page([6,5], 5), page([4,3]));
        retry.click();
        await new Promise(setImmediate);
        assert.deepEqual(cursors, [null, null, '5']);
        assert.equal(rows(), '3,4,5,6');
        assert.equal(pageCache.get(key).at, now);
        assert.ok(retry.classList.contains('hidden'));
    """)


def test_cold_partial_retry_uses_history_cursor() -> None:
    run_page_script(SETUP + """
        answers.push(page([4,3], 3), error);
        await loadInboxMessages();
        assert.equal(rows(), '3,4');
        assert.equal(retry.classList.contains('hidden'), false);
        answers.push(page([2,1]));
        retry.click();
        await new Promise(setImmediate);
        assert.deepEqual(cursors, [null, '3', '3']);
        assert.equal(rows(), '1,2,3,4');
    """)


def test_expired_session_does_not_restore_cached_messages() -> None:
    run_page_script(SETUP + """
        await seed();
        answers.push(page([6,5], 5), reply(401, {}));
        await loadInboxMessages();
        assert.equal(state.loggedIn, false);
        assert.equal(pageCache.has(key), false);
        assert.ok($('app').classList.contains('hidden'));
    """)


def test_late_failure_does_not_change_new_thread() -> None:
    run_page_script(SETUP + """
        await seed();
        let finish;
        answers.push(page([6,5], 5), new Promise(resolve => { finish = resolve; }));
        const old = loadInboxMessages();
        await new Promise(setImmediate);
        inbox.selected = {archive:'other'};
        answers.push(page([9]));
        await loadInboxMessages();
        finish(error);
        await old;
        assert.equal(rows(), '9');
        assert.ok(retry.classList.contains('hidden'));
        assert.equal(pageCache.get(key).at, 0);
    """)
