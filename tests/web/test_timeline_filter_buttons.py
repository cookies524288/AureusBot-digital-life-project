"""3.64 O：按下状态、可见选中样式和实际时间线请求保持一致。"""

from tests.web.test_inbox_refresh import isolate_resource_forms as isolate_resource_forms
from tests.web.test_page_script import run_page_script


def test_timeline_filter_buttons_follow_the_query_and_keep_focus() -> None:
    run_page_script(r"""
        let now = 0;
        globalThis.performance = {now:() => now};
        const box = $('tl-chips'), list = $('tl-list');
        for (const host of [box, list]) {
          // 模拟浏览器写 textContent 时移除子节点；不改共用夹具或发布函数。
          Object.defineProperty(host, 'textContent', {set() {
            if (this.children.includes(document.activeElement)) document.activeElement = null;
            this.replaceChildren();
          }});
        }
        const requests = [];
        fetch = async (path, options) => {
          const [endpoint, query] = path.split('?');
          const params = new URLSearchParams(query);
          requests.push([options.method, endpoint, Object.fromEntries(params)]);
          const type = params.get('types') || 'message', older = params.has('before_id');
          const id = older ? 5 : type === 'message' ? 20 : 10;
          return reply(200, {records:[{id, event_type:type, actor:'me',
            time:'2026-11-27T01:02:03Z', content:'合成记录 ' + id}],
            next_before_id:older ? null : 7});
        };
        const picked = label => {
          const labels = Array.from(box.children, chip => chip.textContent);
          assert.equal(labels.filter(text => text === label).length, 1);
          assert.equal(labels.length, TL_FILTERS.length);
          for (const chip of box.children) {
            assert.equal(chip.tagName, 'BUTTON');
            const selected = chip.textContent === label;
            assert.equal(chip.getAttribute('aria-pressed'), String(selected), chip.textContent);
            assert.equal(chip.classList.contains('on'), selected, chip.textContent);
          }
        };
        const get = label => Array.from(box.children).find(chip => chip.textContent === label);
        const activate = async chip => {
          chip.focus();
          for (const handler of chip.listeners.click) handler({currentTarget:chip});
          await new Promise(setImmediate);
          assert.equal(document.activeElement, chip);
          assert.equal(get(chip.textContent), chip);
        };
        const rows = () => Array.from(list.children, row => row.dataset.recordId);
        const query = extra => ['GET', '/api/timeline', {hours:'24', limit:'100', ...extra}];
        $('tl-hours').value = '24';
        await PAGES.timeline.load();
        assert.deepEqual(requests, [query({})]);
        picked('全部');
        assert.deepEqual(rows(), ['20']);

        const replyButton = get('回复');
        await activate(replyButton);
        const replyQuery = {types:'reply_delivered'};
        assert.deepEqual(requests, [query({}), query(replyQuery)]);
        picked('回复');
        assert.deepEqual(rows(), ['10']);
        // 过期后重复点，不能靠命中缓存侥幸不发请求，也不能丢掉下一页游标。
        now += TIMELINE_CACHE_MS + 1;
        await activate(replyButton);
        picked('回复');
        assert.equal(requests.length, 2);
        $('tl-more').click();
        await new Promise(setImmediate);
        assert.deepEqual(requests, [query({}), query(replyQuery), query({...replyQuery,
          before_id:'7'})]);
        assert.deepEqual(rows(), ['5','10']);
        const last = list.lastChild;
        await activate(replyButton);
        assert.equal(list.lastChild, last);
        assert.equal(requests.length, 3);
        picked('回复');

        renderTimelineChips();
        picked('回复');
        assert.notEqual(get('回复'), replyButton);
        assert.equal(document.activeElement, get('回复'));
        assert.equal(requests.length, 3);
        await activate(get('全部'));
        picked('全部');
        assert.equal(requests.length, 4);
        assert.deepEqual(requests.at(-1), query({}));
        assert.deepEqual(rows(), ['20']);
    """)
