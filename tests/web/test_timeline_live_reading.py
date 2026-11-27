"""推送到达时，正在读的时间线记录不应退回摘要。"""

import pytest

from tests.web.test_live_foreground import foreground_clock as foreground_clock
from tests.web.test_page_script import run_page_script


@pytest.mark.parametrize("focus", ["body", "details", "outside"])
def test_live_reading(focus: str) -> None:
    run_page_script(
        r"""
        state.loggedIn = true; state.page = 'timeline';
        state.incrementalUpdates = false;
        const list = $('tl-list'), page = $('page-timeline');
        Object.defineProperty(list, 'textContent', {set() { this.replaceChildren(); }});
        const descendants = n => n.children.flatMap(c => [c, ...descendants(c)]);
        list.querySelectorAll = selector => descendants(list)
          .filter(n => n.classList.contains(selector.slice(1)));
        const create = document.createElement;
        document.createElement = tag => {
          const n = create(tag);
          n.querySelector = selector => descendants(n)
            .find(c => c.classList.contains(selector.slice(1))) ?? null;
          return n;
        };
        const full = '相同开头'.repeat(90);
        const record = id => ({id, round_id:1, time:'2026-11-27T01:02:03Z', actor:'me',
          event_type:'message', content:full + id, metadata:{test:'详情'}});
        let ids = [1,2];
        fetch = async () => reply(200, {records:ids.map(record),summary:{},next_before_id:null});
        const push = async () => { handleLiveEvent({topic:'timeline'}); await advance(250); };
        const rows = () => list.querySelectorAll('.tl-item');
        const button = row => row.children.find(n => n.tagName === 'BUTTON');
        const details = row => row.children.find(n => n.tagName === 'DETAILS');
        const control = () => ({body:button(rows()[0]), details:details(rows()[0]).children[0],
          outside:$('tl-hours')})['FOCUS'];
        page.scrollHeight = 1000;
        await loadTimeline(false);
        assert.equal(list.children[0].tagName, 'SECTION');
        button(rows()[0]).click(); details(rows()[0]).open = true;
        control().focus(); page.scrollTop = 70;
        ids = [1,2,3]; await push();
        assert.equal(rows().length, 3);
        assert.equal(rows()[0].querySelector('.tl-content').textContent, full + '1');
        assert.equal(button(rows()[0]).getAttribute('aria-expanded'), 'true');
        assert.equal(details(rows()[0]).open, true);
        assert.equal(control(), document.activeElement);
        assert.equal(page.scrollTop, 70);
        assert.equal(button(rows()[1]).getAttribute('aria-expanded'), 'false');
        ids = [2,3]; await push();
        assert.equal(page.scrollTop, 1000);
        ids = [1,2,3]; await push();
        assert.equal(button(rows()[0]).getAttribute('aria-expanded'), 'false');
        assert.ok(!details(rows()[0]).open);
    """.replace("FOCUS", focus)
    )
