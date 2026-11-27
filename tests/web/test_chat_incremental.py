"""C1：执行实际页面脚本，核对持续聊天合并和有界历史。"""

from tests.web.test_page_script import run_page_script


def test_chat_only_shows_accepted_sends_and_keeps_newer_drafts() -> None:
    run_page_script(
        r"""
        state.loggedIn = true;
        api = async () => ({ok:true, data:[]});
        toast = () => {};
        openSocket();
        await loadChatHistory();
        const ws = sockets.at(-1);
        ws.readyState = 1;
        const outgoing = [];
        ws.send = data => outgoing.push(JSON.parse(data));
        const push = frame => ws.onmessage({data:JSON.stringify(frame)});
        const box = $('chat-text');
        const messages = () => chatEntries.filter(entry => entry.node.className === 'msg user');
        const draft = text => { box.value = text; box.listeners.input[0]({target:box}); };
        draft('第一句');
        sendChat();
        const first = outgoing.at(-1).request_id;
        assert.equal(messages().length, 0);
        draft('正输入的第二句');
        push({type:'accepted', request_id:first + 1, message_key:'web:wrong'});
        assert.equal(messages().length, 0);
        push({type:'accepted', request_id:first, message_key:'web:first'});
        assert.equal(messages().length, 1);
        assert.equal(box.value, '正输入的第二句');
        replaceChatWindow(state.loaded, []);
        assert.equal(messages().length, 1);
        appendChatHistory([{id:10, who:'user', content:'第一句', channel:'web',
          message_key:'web:first', time:'2026-11-27T12:00:00Z'}]);
        assert.equal(messages().length, 1);
        assert.equal(messages()[0].id, 10);

        sendChat();
        const second = outgoing.at(-1).request_id;
        draft('第三句草稿');
        push({type:'error', request_id:second, code:'inbound_queue_full', detail:'这句没有收到。'});
        assert.equal(messages().length, 1);
        assert.equal(box.value, '第三句草稿');
        const notice = chatEntries.find(entry => entry.node.className === 'msg notice');
        assert.match(notice.node.children[0].textContent, /正输入的第二句/);
        sendChat();
        const third = outgoing.at(-1).request_id;
        push({type:'accepted', request_id:third, message_key:'web:third'});
        assert.equal(messages().length, 2);
        assert.equal(box.value, '');

        draft('收到一半就断开');
        sendChat();
        ws.onclose();
        assert.equal(box.value, '收到一半就断开');
        assert.equal(messages().length, 2);
        assert.equal(outgoing.length, 4);
        const last = outgoing.at(-1).request_id;
        push({type:'accepted', request_id:last, message_key:'web:old-connection'});
        assert.equal(messages().length, 2);
        """,
        include_resource_pages=False,
    )


def test_chat_live_event_appends_without_full_reload() -> None:
    run_page_script(
        r"""
        const row = (id, key) => ({id, delivery_key:key, who:'me', time:'2026-11-27T12:00:00Z',
          channel:'web', content:`回复 #${id}`});
        state.loggedIn = true;
        const calls = [];
        api = async path => {
          calls.push(path);
          if (path === '/api/history') return {ok:true, data:[row(10, '10:1')]};
          assert.equal(path, '/api/history?replies_only=true&since_id=12&limit=201');
          return {ok:true, data:[row(15, '13:1'), row(18, '16:1')]};
        };
        openSocket();
        const ws = sockets.at(-1);
        const push = message => ws.onmessage({data:JSON.stringify(message)});
        await loadChatHistory();
        const log = $('chat-log');
        const messages = () => log.children.filter(node => node.className === 'msg me');
        const original = messages()[0];
        push({...row(12, '11:1'), type:'reply'});
        const live = messages().at(-1);
        push({type:'event', topic:'chat', data:{id:12, previous_id:10, delivery_key:'11:1'}});
        assert.equal(state.loaded.chatCursor, 12);
        assert.deepEqual(calls, ['/api/history']);
        push({type:'event', topic:'chat', data:{id:18, previous_id:15, delivery_key:'16:1'}});
        await state.loaded.chatDeltaRequest.promise;
        assert.equal(messages().length, 4);
        assert.equal(messages()[0], original);
        assert.equal(messages()[1], live);
        assert.equal(state.loaded.chatCursor, 18);
        assert.equal(calls.length, 2);
        """,
        include_resource_pages=False,
    )


def test_chat_entries_capped_and_older_history_preserves_scroll() -> None:
    run_page_script(
        r"""
        const log = $('chat-log');
        const rect = (item, offset) => {
          const index = log.children.indexOf(item);
          const top = index * 20 - offset;
          return {top, bottom:top + 20};
        };
        log.scrollTop = 0;
        log.getBoundingClientRect = () => ({top:0});
        Object.defineProperty(log, 'scrollHeight', {get:() => log.children.length * 20});
        log.clientHeight = 200;
        const originalInsert = log.insertBefore;
        log.insertBefore = function(node, next) {
          node.getBoundingClientRect = () => rect(node, log.scrollTop);
          originalInsert.call(log, node, next);
        };
        const row = id => ({id, who:'me', time:'2026-11-27T12:00:00Z',
          channel:'web', content:String(id)});
        api = async path => {
          if (path === '/api/history') {
            return {ok:true, data:Array.from({length:1000}, (_, i) => row(i + 51))};
          }
          assert.equal(path, '/api/history?before_id=551');
          return {ok:true, data:Array.from({length:50}, (_, i) => row(i + 501))};
        };
        await loadChatHistory();
        assert.equal(chatEntries.length, 500);
        assert.equal(chatEntriesByKey.size, 500);
        assert.equal(log.children.filter(node => node.className === 'chat-day').length, 1);
        assert.equal(chatEntries[0].id, 551);
        log.scrollTop = 60;
        const anchor = chatEntries[3].node;
        const offset = anchor.getBoundingClientRect().top;
        await loadOlderChatHistory();
        assert.equal(anchor.getBoundingClientRect().top, offset);
        assert.equal(chatEntries.length, 500);
        assert.equal(chatEntries[0].id, 501);
        assert.equal(chatEntries.at(-1).id, 1000);
        assert.equal(log.children.filter(node => node.className === 'chat-day').length, 1);
        """,
        include_resource_pages=False,
    )


def test_chat_reconnect_bounds_recovery_and_reloads_once_only_for_large_gap() -> None:
    run_page_script(
        r"""
        const row = id => ({id, delivery_key:`${id}:1`, who:'me', time:'2026-11-27T12:00:00Z',
          channel:'web', content:String(id)});
        state.loggedIn = true;
        state.page = 'chat';
        const calls = [];
        api = async path => {
          if (path === '/api/chat/typing') return {ok:true, data:{typing:false}};
          calls.push(path);
          if (path.startsWith('/api/history?')) {
            return {ok:true, data:Array.from({length:201}, (_, i) => row(i + 11))};
          }
          return {ok:true, data:[row(calls.length === 1 ? 10 : 999)]};
        };
        openSocket();
        await loadChatHistory();
        sockets.at(-1).onclose();
        openSocket();
        await sockets.at(-1).onopen();
        assert.deepEqual(calls, [
          '/api/history', '/api/history?replies_only=true&since_id=10&limit=201', '/api/history',
        ]);
        assert.equal(state.loaded.chatCursor, 999);
        assert.deepEqual(chatEntries.map(item => item.id), [999]);
        """,
        include_resource_pages=False,
    )
