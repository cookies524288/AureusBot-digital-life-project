"""3.64 Q：列表刷新时同步当前会话名，不重开同一路由的会话。"""

from tests.web.test_inbox_refresh import isolate_resource_forms as isolate_resource_forms
from tests.web.test_page_script import run_page_script

SETUP = r"""
    let now = 0;
    globalThis.performance = {now:() => now};
    state.loggedIn = true; state.page = 'inbox'; inbox.view = 'qq';
    const prefix = 'A'.repeat(1023);
    const entry = (account, suffix, unread = 0) => ({name:prefix + suffix, unread_count:unread,
      key:{platform:'qq', account_id:account, kind:'group', recipient_id:'42'}});
    const listing = (...conversations) => reply(200, {enabled:true, conversations, archives:[]});
    const first = entry('1', 'X'), other = entry('2', 'Y');
    const listPath = '/api/inbox/conversations';
    const replies = [listing(first, other)], requests = [];
    fetch = async (path, options) => {
      requests.push(options.method + ' ' + path);
      if (path === listPath) return replies.shift();
      if (path.startsWith('/api/inbox/messages?')) {
        const account = new URLSearchParams(path.split('?')[1]).get('account_id');
        return reply(200, {messages:[{id:Number(account), source:'inbox', direction:'incoming',
          content:'合成消息 ' + account, attachments:[]}], next_before_id:null});
      }
      if (path.startsWith('/api/inbox/typing?')) return reply(200, {typing:true});
      if (path.startsWith('/api/timeline?')) return reply(200, {records:[]});
      if (path.startsWith('/api/inbox/history-cleanup?')) return reply(200, {preview:null});
      return reply(404, {});
    };
    const title = $('inbox-thread-name'), list = $('inbox-conversations');
    const history = $('inbox-messages');
    const labels = () => Array.from(list.children).filter(n => n.tagName === 'BUTTON')
      .map(n => n.firstElementChild.textContent);
    await loadInbox();
    await new Promise(setImmediate);
    assert.equal(title.textContent, first.name);
    assert.deepEqual(labels(), [first.name, other.name]);
    const row = history.firstElementChild, activity = inboxActivitySession, typing = typingSession;
    assert.ok(row && activity && typing?.wasActive);
    const refresh = async (...conversations) => {
      now += INBOX_CACHE_MS + 1;
      replies.push(listing(...conversations));
      await loadInbox();
    };
    const kept = name => {
      assert.equal(title.textContent, name);
      assert.equal(inbox.selected.name, name);
      assert.equal(history.firstElementChild, row);
      assert.equal(inboxActivitySession, activity);
      assert.equal(typingSession, typing);
      assert.ok(typing.wasActive);
    };
    requests.length = 0;
"""


def test_same_route_refresh_updates_title_without_reopening() -> None:
    run_page_script(SETUP + r"""
        let displayed = title.textContent, writes = 0;
        Object.defineProperty(title, 'textContent', {
          get:() => displayed, set(value) { displayed = value; writes++; },
        });
        const renamed = entry('1', 'Z', 8);
        await refresh(other, renamed);
        assert.deepEqual(labels(), [other.name, renamed.name]);
        kept(renamed.name);
        assert.equal(inbox.selected.unread_count, 8);
        assert.equal(writes, 1);
        const renamedOther = entry('2', 'W');
        await refresh(renamedOther, entry('1', 'Z', 3));
        assert.deepEqual(labels(), [renamedOther.name, renamed.name]);
        kept(renamed.name);
        assert.equal(inbox.selected.unread_count, 3);
        assert.equal(writes, 1);
        assert.deepEqual(requests, ['GET ' + listPath, 'GET ' + listPath]);
    """)


def test_late_list_cannot_undo_rename_and_removed_selection_still_switches() -> None:
    run_page_script(SETUP + r"""
        let finish;
        replies.push(new Promise(resolve => { finish = resolve; }));
        now += INBOX_CACHE_MS + 1;
        const late = loadInbox();
        // 走发布代码的切页取消，第二次 GET 不会复用在途请求；模拟旧响应仍迟到。
        abortPageRequests();
        const latest = entry('1', 'N');
        await refresh(other, latest);
        kept(latest.name);
        finish(listing(first, other));
        await late;
        kept(latest.name);
        assert.deepEqual(labels(), [other.name, latest.name]);
        assert.deepEqual(requests, ['GET ' + listPath, 'GET ' + listPath]);
        await refresh(other);
        assert.equal(inbox.selected.key.account_id, other.key.account_id);
        assert.equal(title.textContent, other.name);
        assert.notEqual(history.firstElementChild, row);
        const reads = requests.filter(path => path.startsWith('GET /api/inbox/messages?'));
        assert.equal(reads.length, 1);
        assert.equal(new URLSearchParams(reads[0].split('?')[1]).get('account_id'), '2');
        assert.ok(requests.every(path => path.startsWith('GET ')));
    """)
