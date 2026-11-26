"""完整最新页为空时，仅清理当前会话的旧快照。"""

import pytest

from tests.web.test_inbox_refresh import SETUP, isolate_resource_forms  # noqa: F401
from tests.web.test_page_script import run_page_script

# 通用夹具对任何选择器都会制造节点；此处只匹配列表里实际存在的输入气泡。
SETUP += """
$('inbox-messages').querySelector = selector => {
  assert.equal(selector, ':scope > .inbox-typing');
  return $('inbox-messages').children.find(row => row.classList.contains('inbox-typing')) ?? null;
};
"""


def test_empty_latest_replaces_messages_only_on_transition() -> None:
    run_page_script(SETUP + """
        await seed();
        const other = key + '-other';
        pageCache.set(other, pageCache.get(key));
        answers.push(error);
        await refreshInboxMessages();
        assert.equal(rows(), '1,2,3,4');
        assert.equal(pageCache.get(key).at, 0);
        const list = $('inbox-messages');
        answers.push(page([]));
        await refreshInboxMessages();
        assert.equal(rows(), '');
        assert.equal(inbox.messageKeys.size, 0);
        assert.equal(pageCache.has(key), false);
        assert.equal(pageCache.has(other), true);
        assert.equal(state.loggedIn, true);
        assert.match($('inbox-message-status').textContent, /没有可查看/);
        list.replaceChildren = () => assert.fail('重复空页不重绘');
        answers.push(page([]), page([4]));
        await refreshInboxMessages();
        await refreshInboxMessages();
        assert.equal(rows(), '4');
        assert.equal($('inbox-message-status').textContent, '');
    """)


@pytest.mark.parametrize('pending', ['loadInboxMessages', 'loadOlderInboxMessages'])
def test_late_history_cannot_restore_empty_snapshot(pending: str) -> None:
    run_page_script(SETUP + """
        await seed();
        inbox.nextBefore = 1;
        let finish;
    """ + ("answers.push(page([7], 7));" if pending == 'loadInboxMessages' else '') + """
        answers.push(new Promise(resolve => { finish = resolve; }));
    """ + f'const pending = {pending}();' + """
        await new Promise(setImmediate);
        assert.notEqual(cursors.at(-1), null);
        answers.push(page([]));
        await refreshInboxMessages();
        finish(page([8]));
        await pending;
        assert.equal(rows(), '');
        assert.equal(pageCache.has(key), false);
        assert.equal(inbox.nextBefore, null);
        assert.equal(inbox.loadingOlder, false);
    """)


def test_empty_history_and_late_latest_leave_messages_alone() -> None:
    run_page_script(SETUP + """
        await seed();
        inbox.nextBefore = 1;
        answers.push(page([]));
        await loadOlderInboxMessages();
        assert.equal(rows(), '1,2,3,4');
        assert.equal(inbox.nextBefore, null);
        let finish;
        answers.push(new Promise(resolve => { finish = resolve; }));
        const pending = refreshInboxMessages();
        inbox.selected = {archive:'other'};
        answers.push(page([9]));
        await loadInboxMessages();
        finish(page([]));
        await pending;
        assert.equal(rows(), '9');
        assert.equal(pageCache.get(key).at, 0);
    """)
