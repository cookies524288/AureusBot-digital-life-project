'use strict';

(() => {
  const flowPath = '/api/resources/memory/flow';
  const transferPath = '/api/resources/memory/transfer';
  let flow;

  function renderEntry(item) {
    const stamp = item.recorded_at ? new Date(item.recorded_at).toLocaleString('zh-CN') : '时间未记录';
    const source = item.source
      ? [item.source.instance, item.source.role, item.source.table, JSON.stringify(item.source.key)].join(' / ')
      : '本地时间线 #' + item.record_id;
    return el('article', {class:'card stack', style:'overflow-wrap:anywhere'},
      el('div', {class:'row'},
        el('time', {datetime:item.recorded_at, text:stamp}),
        item.time_precision && el('small', {text:item.time_precision}),
        item.actor_label && el('span', {text:item.actor_label}),
        !item.is_recallable && el('small', {text:'未参与召回'})),
      el('div', {text:item.text || '空文本', style:'white-space:pre-wrap'}),
      el('small', {text:source}));
  }

  function createFlow(panel) {
    const entries = el('div', {class:'stack', 'aria-live':'polite'});
    const refresh = el('button', {class:'btn small', type:'button', text:'刷新'});
    const more = el('button', {class:'btn small hidden', type:'button', text:'加载更多'});
    let next = null;

    async function read(append = false) {
      if (refresh.disabled || (append && next === null)) return;
      refresh.disabled = more.disabled = true;
      if (!append) {
        next = null;
        entries.textContent = '读取中…';
      }
      try {
        const res = await api(flowPath + '?offset=' + (next ?? 0));
        if (!res.ok) {
          if (!append) entries.textContent = errorText(res);
          else fail(res);
          return;
        }
        if (!append) entries.replaceChildren();
        if (!append && !res.data.items.length) {
          entries.textContent = res.data.enabled ? '还没有文字记忆' : '长期记忆功能当前关闭';
        }
        entries.append(...res.data.items.map(renderEntry));
        next = res.data.next_offset;
      } finally {
        refresh.disabled = more.disabled = false;
        more.classList.toggle('hidden', next === null);
      }
    }

    refresh.addEventListener('click', () => read());
    more.addEventListener('click', () => read(true));
    panel.replaceChildren(el('div', {class:'row'}, refresh), entries, el('div', {class:'row'}, more));
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
