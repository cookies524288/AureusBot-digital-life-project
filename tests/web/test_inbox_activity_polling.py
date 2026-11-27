"""用页面时钟验证收件箱断线后的活动轮询随导航结束。"""

from tests.web.test_live_foreground import foreground_clock as foreground_clock
from tests.web.test_page_script import run_page_script


def test_disconnected_inbox_activity_polls_until_leaving() -> None:
    run_page_script(
        """
        state.loggedIn = true; state.page = 'inbox';
        inbox.selected = {key:{platform:'qq', account_id:'fixture', kind:'group',
          recipient_id:'A', temporary_group_id:null}};
        const requests = [];
        loadInbox = async () => {};
        refreshInboxMessages = async () => {};
        PAGES.persona.load = async () => {};
        fetch = async path => {
          requests.push(path);
          return reply(200, {records:[], since_id:32, has_more:false});
        };
        const params = new URLSearchParams({hours:'1', limit:'60',
          conversation:JSON.stringify(inbox.selected.key)});
        const snapshot = '/api/timeline?' + params;
        startInboxActivity(inbox.selected); await advance(0); await advance(14999);
        assert.deepEqual(requests, [snapshot]);
        await advance(1);
        assert.deepEqual(requests, [snapshot, snapshot + '&since_id=32']);
        await showPage('persona'); await advance(30000);
        assert.equal(state.page, 'persona');
        assert.deepEqual(requests, [snapshot, snapshot + '&since_id=32']);
        """,
        include_resource_pages=False,
    )
