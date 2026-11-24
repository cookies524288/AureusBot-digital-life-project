"""用可控制返回顺序的假接口检查网页切换和按钮状态。"""

import re

from tests.web.test_page_script import ROOT, run_page_script


def test_controls_start_disabled_before_javascript() -> None:
    html = (ROOT / "aureus/web/static/index.html").read_text(encoding="utf-8")
    for control in ("chat-send", "ky-add", "md-add-model", "tl-more", "inbox-more"):
        assert re.search(rf'<button\b[^>]*\bid="{control}"[^>]*\bdisabled\b', html), control


def test_images_bookmark_opens_requested_tab_without_loading_memory_query() -> None:
    run_page_script(
        r"""
        const paths = [];
        fetch = async path => { paths.push(path); return reply(500, {detail:'测试读取失败'}); };
        state.loggedIn = true;
        Object.assign($('pictures-result'), {clientHeight:200, clientWidth:200});
        globalThis.getComputedStyle = () => ({rowGap:'10px', getPropertyValue:() => '80px'});
        history.replaceState = (_, __, hash) => { location.hash = hash; };
        await showPage('images');
        assert.equal(state.page, 'memory');
        assert.equal(location.hash, '#memory');
        assert.equal($('memory-panel-images').classList.contains('hidden'), false);
        assert.equal(paths.length, 1);
        const [path, query] = paths[0].split('?');
        assert.equal(path, '/api/resources/pictures');
        assert.equal(new URLSearchParams(query).get('availability'), '');
        """
    )


def test_navigation_callback_follows_only_the_visit_that_requested_it() -> None:
    run_page_script(
        r"""
        const loads = [];
        PAGES.config.load = () => new Promise(resolve => loads.push(resolve));
        PAGES.persona.load = () => {};
        let selections = 0;
        const jump = showPage('config', () => selections++);
        await showPage('persona');
        const returning = showPage('config');
        loads[0]();
        await jump;
        assert.equal(selections, 0);
        loads[1]();
        await returning;
        $('page-config').classList.add('active');
        await showPage('config', () => selections++);
        assert.equal(selections, 1);
        """
    )


def test_last_model_read_wins_when_routes_share_the_config() -> None:
    run_page_script(
        r"""
        const loads = [];
        api = () => new Promise(resolve => loads.push(resolve));
        function config(name) {
          return {ok:true, data:{config:{providers:[{name}], models:[], organs:{}}}};
        }
        const older = loadModelsConfig();
        const newer = loadModelsConfig();
        loads[1](config('new'));
        await newer;
        loads[0](config('old'));
        await older;
        assert.equal(mdl.cfg.providers[0].name, 'new');
        """
    )


def test_key_actions_stay_unavailable_until_a_successful_read() -> None:
    run_page_script(
        r"""
        const loads = [];
        api = () => new Promise(resolve => loads.push(resolve));
        const first = loadKeysPage();
        assert.equal($('ky-add').disabled, true);
        const second = loadKeysPage();
        loads[0]({ok:true, data:{vault:'ok', entries:[]}});
        await first;
        assert.equal($('ky-add').disabled, true);
        loads[1]({ok:false, status:0});
        await second;
        assert.equal($('ky-add').disabled, true);
        assert.match($('ky-status').textContent, /连不上服务/);
        const retry = loadKeysPage();
        loads[2]({ok:true, data:{vault:'ok', entries:[]}});
        await retry;
        assert.equal($('ky-add').disabled, false);
        assert.equal($('ky-status').classList.contains('hidden'), true);
        """
    )


def test_timeline_paginates_once_and_never_uses_a_cursor_for_another_filter() -> None:
    run_page_script(
        r"""
        const loads = [];
        api = path => new Promise(resolve => loads.push({path, resolve}));
        renderTimeline = () => {};
        const result = next => ({ok:true, data:{records:[], next_before_id:next}});
        const first = loadTimeline(false);
        loads[0].resolve(result(42));
        await first;
        const more = loadTimeline(true);
        await loadTimeline(true);
        assert.equal(loads.length, 2);
        assert.match(loads[1].path, /before_id=42/);
        tl.filter = 'judgment';
        const switched = loadTimeline(false, true);
        loads[2].resolve({ok:false, status:0});
        await switched;
        loads[1].resolve(result(35));
        await more;
        assert.equal($('tl-more').disabled, true);
        await loadTimeline(true);
        assert.equal(loads.length, 3);
        """
    )


def test_overview_reference_updates_without_exposing_a_raw_object() -> None:
    run_page_script(
        r"""
        showOverviewReference({reply_reference:{content:'建议不回复\n依据：只是在道别',
          channel:'qq', visibility:'group', time:'2026-11-23T10:00:00Z'}});
        assert.equal($('ov-reference').textContent, '建议不回复\n依据：只是在道别');
        assert.match($('ov-reference-source').textContent, /QQ · 群聊/);
        showOverviewReference({reply_reference:null});
        assert.equal($('ov-reference').textContent, '暂无回复参考');
        assert.equal($('ov-reference-source').textContent, '');
        """
    )


def test_timeline_details_translate_fields_and_preserve_plain_text() -> None:
    run_page_script(
        r"""
        const card = timelineItem({id:1, actor:'me', channel:'qq', visibility:'group',
          event_type:'reply_reference', content:'建议不回复',
          metadata:{channel:'qq',plan:{reply:false,reason:'<b>只是道别</b>'},in_reply_to:[9]}},false);
        const descendants = node => [node, ...node.children.flatMap(descendants)];
        const nodes = descendants(card);
        const text = nodes.map(node => node.textContent);
        const labels = ['群聊','QQ','规划建议','是否建议回复','否','依据','<b>只是道别</b>','9'];
        for (const expected of labels) {
          assert.ok(text.includes(expected), expected);
        }
        assert.equal(nodes.some(node => ['PRE','B'].includes(node.tagName)), false);
        """
    )


def test_monitor_visibility_tags_do_not_change_timeline_or_overview_labels() -> None:
    run_page_script(
        r"""
        const scopes = ['private', 'user_only', 'group', 'public'];
        const originalLabels = ['仅自己可见', '仅用户可见', '群聊', '公开'];
        const records = scopes.map((visibility, index) => ({
          id:index + 1, actor:'system', event_type:'judgment', visibility,
          content:'合成记录 ' + visibility, time:'2026-11-24T10:00:00Z',
        }));
        const badge = card => card.children[0].children
          .find(node => node.className === 'tag')?.textContent;
        api = async path => ({ok:true, data:path === '/api/overview' ? {} : {records}});
        state.loggedIn = true;
        state.page = 'monitor';
        await PAGES.monitor.load();
        for (const id of ['mo-hormones', 'mo-recent', 'mo-persona']) {
          const rows = $(id).children;
          assert.equal(rows.length, records.length);
          for (const [index, record] of records.entries()) {
            const row = rows.find(node => node.dataset.recordId === String(record.id));
            assert.equal(row.children[1].textContent, record.content);
            assert.equal(badge(row), index < 2 ? '仅用户可见' : undefined);
          }
        }
        tl.records = records;
        renderTimeline();
        for (const [index, record] of records.entries()) {
          assert.equal(record.visibility, scopes[index]);
          assert.equal(badge($('tl-list').children.at(-1 - index)), originalLabels[index]);
          showOverviewReference({reply_reference:record});
          assert.ok($('ov-reference-source').textContent.endsWith(originalLabels[index]));
        }
        """
    )


def test_model_probe_details_describe_media_and_embedding_dimensions() -> None:
    run_page_script(
        r"""
        const lines = probeInputLines({messages:[{content:[{type:'text',text:'颜色？'},
          {type:'image_url',image_url:{url:'data:image/png;base64,FAKE'}}]}]});
        assert.deepEqual(lines, ['颜色？','测试图片']);
        assert.equal(probeReplyText({capability:'embedding',reply:'[0.1,0.2]'}), '收到 2 维向量。');
        assert.equal(probeReplyText({capability:'vision',reply:'蓝色'}), '蓝色');
        """
    )
