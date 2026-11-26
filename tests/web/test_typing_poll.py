"""输入状态轮询：未知不等于停止输入。"""

import pytest

from tests.web import test_page_script
from tests.web.test_page_script import run_page_script


@pytest.mark.parametrize(('response', 'expected_reads'), [
    ('reply(503, {})', 1),
    ('reply(200, {typing:"false"})', 1),
    ('reply(403, {})', 0),
])
def test_typing_poll(response: str, expected_reads: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """执行真正的轮询，逐次检查气泡和停止后的补读。"""
    marker = '  vm.runInContext(input.html.match'
    stub = "  vm.runInContext('installResourcePages = () => () => {}', context);\n"
    monkeypatch.setattr(test_page_script, 'HARNESS', test_page_script.HARNESS.replace(
        marker, stub + marker,
    ))
    run_page_script("""
        state.loggedIn = true;
        state.page = 'inbox';
        inbox.selected = {key:'test'};
        let reads = 0, visible = false;
        refreshInboxMessages = () => { reads++; };
        showInboxTypingBubble = () => { visible = true; };
        removeInboxTypingBubble = () => { visible = false; };
        const yes = reply(200, {typing:true}), no = reply(200, {typing:false});
        const answers = [yes, RESPONSE, no, no];
        fetch = async () => answers.shift();
        followConversationTyping();
        await new Promise(setImmediate);
        assert.deepEqual([visible, reads], [true, 0]);
        for (const count of [0, EXPECTED, EXPECTED]) {
          const tick = typingSession.timer;
          clearTimeout(tick);
          await tick();
          assert.deepEqual([visible, reads, state.loggedIn], [false, count, true]);
        }
    """.replace('RESPONSE', response).replace('EXPECTED', str(expected_reads)))
