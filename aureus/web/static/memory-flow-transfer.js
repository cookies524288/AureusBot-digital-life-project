'use strict';

(() => {
  const flowPath = '/api/resources/memory/flow';
  const transferPath = '/api/resources/memory/transfer';
  const eventTypes = {
    profile_created:'画像创建', profile_updated:'画像更新', profile_snapshot:'画像快照',
    profile_override_set:'画像维护', person_merged:'人物资料合并', relation_frozen:'关系冻结',
    delete_executed:'删除操作执行', delete_restored:'删除操作恢复'
  };
  let flow;

  function renderEntry(item) {
    const stamp = item.recorded_at ? new Date(item.recorded_at).toLocaleString('zh-CN') : '时间未记录';
    return el('article', {class:'card stack', style:'overflow-wrap:anywhere'},
      el('div', {class:'row', style:'flex-wrap:wrap'},
        el('strong', {text:eventTypes[item.event_type]}),
        el('time', {datetime:item.recorded_at, text:stamp})),
      el('div', {text:item.summary}),
      item.conversations.length && el('small', {class:'muted', text:item.conversations.join(' · ')}));
  }

  function createFlow(panel) {
    const entries = el('div', {class:'stack', 'aria-live':'polite'});
    const conversation = el('select', {}, el('option', {value:'', text:'全部聊天流'}));
    const type = el('select', {}, el('option', {value:'', text:'全部事件'}),
      Object.entries(eventTypes).map(([value, text]) => el('option', {value, text})));
    const start = el('input', {type:'datetime-local', step:'1'});
    const end = el('input', {type:'datetime-local', step:'1'});
    const refresh = el('button', {class:'btn small', type:'submit', text:'刷新'});
    const more = el('button', {class:'btn small hidden', type:'button', text:'加载更多'});
    const shortcuts = [[24, '最近24小时'], [168, '最近7天'], [720, '最近30天']]
      .map(([hours, label]) => el('button', {class:'btn small', type:'button', text:label,
        onclick:() => read(false, hours)}));
    const controls = el('fieldset', {class:'stack', style:'border:0;padding:0;margin:0;min-width:0'},
      el('div', {class:'row', style:'flex-wrap:wrap'},
        el('label', {class:'stack'}, '聊天流', conversation),
        el('label', {class:'stack'}, '事件类型', type),
        el('label', {class:'stack'}, '开始时间', start),
        el('label', {class:'stack'}, '结束时间', end)),
      el('div', {class:'row', style:'flex-wrap:wrap'}, ...shortcuts, refresh));
    const filters = el('form', {}, controls);
    let next = null;
    let applied = new URLSearchParams();

    function localTime(value) {
      const date = new Date(value);
      return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
    }

    async function read(append = false, hours = null) {
      if (controls.disabled || (append && next === null)) return;
      controls.disabled = more.disabled = true;
      if (!append) {
        next = null;
        applied = new URLSearchParams();
        if (conversation.value) applied.set('conversation', conversation.value);
        if (type.value) applied.set('event_type', type.value);
        if (hours) applied.set('hours', hours);
        else {
          if (start.value) applied.set('start', new Date(start.value).toISOString());
          if (end.value) applied.set('end', new Date(end.value).toISOString());
        }
        entries.textContent = '读取中…';
      }
      try {
        const params = new URLSearchParams(applied);
        params.set('offset', next ?? 0);
        const res = await api(flowPath + '?' + params);
        if (!res.ok) {
          if (!append) entries.textContent = errorText(res);
          else fail(res);
          return;
        }
        if (!append) {
          const selected = conversation.value;
          conversation.replaceChildren(el('option', {value:'', text:'全部聊天流'}),
            ...res.data.conversations.map(value => el('option', {value, text:value})));
          conversation.value = selected;
          entries.replaceChildren();
        }
        if (hours) {
          applied.delete('hours');
          applied.set('start', res.data.start);
          applied.set('end', res.data.end);
          start.value = localTime(res.data.start);
          end.value = localTime(res.data.end);
        }
        if (!append && !res.data.items.length) {
          entries.textContent = res.data.enabled ? '没有符合条件的记忆事件' : '长期记忆功能当前关闭';
        }
        entries.append(...res.data.items.map(renderEntry));
        next = res.data.next_offset;
      } finally {
        controls.disabled = more.disabled = false;
        more.classList.toggle('hidden', next === null);
      }
    }

    filters.addEventListener('submit', event => {
      event.preventDefault();
      read();
    });
    more.addEventListener('click', () => read(true));
    panel.replaceChildren(filters, entries, el('div', {class:'row'}, more));
    return read;
  }

  window.loadMemoryFlow = () => {
    flow ??= createFlow($('memory-panel-flow'));
    return flow();
  };

  window.loadMemoryTransfer = () => {
    const panel = $('memory-panel-transfer');
    if (panel.firstChild) return;
    const input = el('input', {type:'file', accept:'.jsonl,application/x-ndjson', required:true});
    const submit = el('button', {class:'btn primary small', type:'submit', text:'追加导入'});
    const result = el('p', {role:'status'});
    const form = el('form', {class:'stack'}, el('label', {class:'stack'}, '选择记忆包', input),
      el('div', {class:'row'}, submit), result);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const file = input.files[0];
      if (!file) return;
      input.disabled = submit.disabled = true;
      result.textContent = '导入中…';
      try {
        const response = await fetch(transferPath, {
          method:'POST', credentials:'same-origin', body:file,
          headers:{'Content-Type':'application/x-ndjson'}
        });
        const data = await response.json();
        if (response.status === 401) await api('/api/status');
        if (response.ok) {
          result.textContent = `新增 ${data.added} 条，跳过相同记录 ${data.skipped} 条。`;
          form.reset();
        } else {
          result.textContent = errorText({status:response.status, data});
        }
      } catch {
        result.textContent = '导入请求失败，请稍后再试。';
      } finally {
        input.disabled = submit.disabled = false;
      }
    });
    panel.replaceChildren(el('div', {class:'card stack'},
      el('p', {text:'文字记忆包含长期记忆原件、来源、证据和网页维护的修订及召回设置。图片附件、媒体资产和本地时间线经历不随包传输。'}),
      el('div', {class:'row'}, el('a', {class:'btn', href:transferPath, download:true, text:'导出文字记忆包'})),
      form));
  };
})();
