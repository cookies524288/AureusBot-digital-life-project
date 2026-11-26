"""3.64 N：发布列表和观察页保留全文；CSS 规则检查不代替实际布局测量。"""

import re
from pathlib import Path

from tests.web.test_inbox_refresh import isolate_resource_forms as isolate_resource_forms
from tests.web.test_page_script import run_page_script

PAGE = Path(__file__).resolve().parents[2] / "aureus/web/static/index.html"


def _rule(selector: str) -> dict[str, str]:
    css = PAGE.read_text(encoding="utf-8").split("<style>", 1)[1].split("</style>", 1)[0]
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    result = {}
    for selectors, block in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if selector in (part.strip() for part in selectors.split(",")):
            result.update(item.split(":", 1) for item in block.split(";") if ":" in item)
    return result


def test_conversation_name_wraps_instead_of_inheriting_button_nowrap() -> None:
    name = _rule(".inbox-conversation span:first-child")
    name.update(_rule(".inbox-conversation>span:first-child"))
    assert name.get("white-space", _rule(".btn")["white-space"]) == "normal"
    assert name.get("min-width") == "0"
    assert name.get("overflow-wrap") == "anywhere"
    assert _rule(".inbox-unread")["flex"] == "none"


def test_monitor_long_tokens_wrap_without_collapsing_newlines() -> None:
    state = _rule(".state-text") | _rule("#mo-state") | _rule("#mo-state .state-line")
    for rule in (state, _rule("#mo-task")):
        assert rule.get("overflow-wrap", "normal") == "anywhere"
        assert rule.get("white-space", "normal") == "pre-wrap"


def test_rendered_names_badges_and_monitor_lines_keep_the_full_text() -> None:
    run_page_script(r"""
        const token = 'A'.repeat(1023) + 'Z';
        const entry = (id, name, unread) => ({name, unread_count:unread,
          key:{kind:'group', platform:'qq', account_id:'10', recipient_id:id}});
        inbox.view = 'qq';
        inbox.conversations = [entry('1','短名',0), entry('2',token,128), entry('3','另一个',5)];
        inbox.selected = inbox.conversations[1];
        await renderInboxView({keepSelection:true});
        const rows = $('inbox-conversations').children.filter(item => item.tagName === 'BUTTON');
        assert.equal(rows.length, 3);
        for (const [index, row] of rows.entries()) {
          assert.ok(row.classList.contains('inbox-conversation'));
          assert.equal(row.firstElementChild.tagName, 'SPAN');
          assert.equal(row.firstElementChild.textContent, inbox.conversations[index].name);
        }
        assert.equal(rows[0].children.length, 1);
        const unread = rows[1].children[1];
        assert.ok(unread.classList.contains('inbox-unread'));
        assert.equal(unread.textContent, '99+');
        assert.equal(unread.getAttribute('aria-label'), '128 条未读消息');
        assert.equal(rows[2].children[1].textContent, '5');

        const empty = {ok:true, data:{records:[]}};
        const monitor = (task, lines) => renderMonitorResponses({
          ov:{ok:true, data:{current_task:task, state_text:lines.join('\n')}},
          hormones:empty, recent:empty, persona:empty,
        });
        monitor('检查', ['短行']);
        assert.equal($('mo-task').textContent, '检查');
        const stateBox = $('mo-state');
        assert.equal(stateBox.children[0].textContent, '短行');
        monitor(token + '\n任务末尾', ['第一行', token, '状态末尾']);
        assert.equal($('mo-task').textContent, token + '\n任务末尾');
        const lines = Array.from(stateBox.children, n => n.textContent);
        assert.deepEqual(lines, ['第一行', token, '状态末尾']);
        assert.ok(stateBox.children.every(n => n.classList.contains('state-line')));
    """)
