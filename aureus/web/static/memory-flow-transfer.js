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

  document.getElementById('memory-inspection').addEventListener('click', async () => {
    const report = el('div', {class:'stack resource-detail', role:'status'}, '正在核对本地文字索引…');
    openEditor('只读检修', report);
    const response = await api('/api/resources/memory/inspection');
    if (!response.ok) {
      report.textContent = errorText(response);
      return;
    }
    const {coverage, checked, checks} = response.data;
    report.replaceChildren(
      el('p', {text:coverage}),
      el('p', {text: checked ? `已检查 ${checked} 条。各项原因可能重叠。` : '没有符合范围的条目，未检查任何原件。'}),
      el('ul', {}, checks.map(check => el('li', {text:`${check.label}：${check.count} 条。${check.detail}`})))
    );
  });

  function renderEntry(item) {
    const stamp = item.recorded_at ? new Date(item.recorded_at).toLocaleString('zh-CN') : '时间未记录';
    const type = eventTypes[item.event_type] ?? item.event_type;
    const title = type + ' · ' + stamp + ' · ' + item.summary;
    return el('div', {class:'resource-record'}, el('button', {class:'btn small', type:'button', onclick:() =>
      openEditor(type, el('div', {class:'stack resource-detail'},
        el('time', {datetime:item.recorded_at, text:stamp}),
        el('p', {text:item.summary}),
        item.conversations.length && el('p', {text:'聊天流：' + item.conversations.join(' · ')})))
    }, el('span', {text:title})));
  }

  function createFlow(panel) {
    const conversation = el('select', {}, el('option', {value:'', text:'全部聊天流'}));
    const type = el('select', {}, el('option', {value:'', text:'全部事件'}),
      Object.entries(eventTypes).map(([value, text]) => el('option', {value, text})));
    const start = el('input', {type:'datetime-local', step:'1'});
    const end = el('input', {type:'datetime-local', step:'1'});
    const refresh = el('button', {class:'btn small', type:'submit', text:'刷新'});
    const shortcuts = [[24, '最近24小时'], [168, '最近7天'], [720, '最近30天']]
      .map(([hours, label]) => el('button', {class:'btn small', type:'button', text:label,
        onclick:() => apply(hours)}));
    const controls = el('fieldset', {class:'stack', style:'border:0;padding:0;margin:0;min-width:0'},
      el('div', {class:'row resource-filters'},
        el('label', {class:'stack'}, '聊天流', conversation),
        el('label', {class:'stack'}, '事件类型', type),
        el('label', {class:'stack'}, '开始时间', start),
        el('label', {class:'stack'}, '结束时间', end)),
      el('div', {class:'row', style:'flex-wrap:wrap'}, ...shortcuts, refresh));
    const filters = el('form', {}, controls);
    let applied = new URLSearchParams();

    function localTime(value) {
      const date = new Date(value);
      return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
    }

    function apply(hours) {
      applied = new URLSearchParams();
      if (conversation.value) applied.set('conversation', conversation.value);
      if (type.value) applied.set('event_type', type.value);
      if (hours) applied.set('hours', hours);
      else {
        if (start.value) applied.set('start', new Date(start.value).toISOString());
        if (end.value) applied.set('end', new Date(end.value).toISOString());
      }
      return pager.reset();
    }

    filters.addEventListener('submit', event => {
      event.preventDefault();
      apply();
    });
    panel.replaceChildren(filters);
    const pager = createResourcePager({el, panel, id:'memory-flow', render:renderEntry, error:fail,
      empty:() => '没有符合条件的记忆事件', request:({offset, limit}) => {
        const params = new URLSearchParams(applied);
        params.set('offset', offset);
        params.set('limit', limit);
        return api(flowPath + '?' + params);
      }, loaded:data => {
        const selected = conversation.value;
        conversation.replaceChildren(el('option', {value:'', text:'全部聊天流'}),
          ...data.conversations.map(value => el('option', {value, text:value})));
        conversation.value = selected;
        if (!applied.has('hours')) return;
        applied.delete('hours');
        applied.set('start', data.start);
        applied.set('end', data.end);
        start.value = localTime(data.start);
        end.value = localTime(data.end);
      },
    });
    return pager;
  }

  window.getMemoryFlow = () => {
    flow ??= createFlow($('memory-panel-flow'));
    return flow;
  };

  window.loadMemoryTransfer = () => {
    const panel = $('memory-panel-transfer');
    if (panel.firstChild) return;
    const input = el('input', {type:'file', accept:'.jsonl,application/x-ndjson', required:true});
    const submit = el('button', {class:'btn primary small', type:'submit', text:'追加导入', disabled:true});
    const result = el('p', {role:'status'});
    const form = el('form', {class:'stack'}, el('label', {class:'stack'}, '选择记忆包', input),
      el('div', {class:'row'}, submit), result);
    input.addEventListener('change', () => { submit.disabled = !input.files.length; });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const file = input.files[0];
      if (!file || submit.disabled) return;
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
        input.disabled = false;
        submit.disabled = !input.files.length;
      }
    });
    panel.replaceChildren(el('div', {class:'card stack'},
      el('p', {text:'文字记忆包包含原件、来源、证据和网页维护的修订及召回设置。已移除项留在本机，不随包导出；图片附件、媒体资产和本地时间线经历也不随包传输。'}),
      el('div', {class:'row'}, el('a', {class:'btn', href:transferPath, download:true, text:'导出文字记忆包'})),
      form));
  };
})();
