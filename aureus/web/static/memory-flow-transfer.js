'use strict';

(() => {
  const flowPath = '/api/resources/memory/flow';
  const transferPath = '/api/resources/memory/transfer';
  const packageLabels = {messages:'消息', paragraphs:'段落', relations:'关系', fact_claims:'事实',
    episodes:'情景记忆', other:'其他文字记录'};
  const eventTypes = {
    profile_created:'画像创建', profile_updated:'画像更新', profile_snapshot:'画像快照',
    profile_override_set:'画像维护', person_merged:'人物资料合并', relation_frozen:'关系冻结',
    delete_executed:'删除操作执行', delete_restored:'删除操作恢复'
  };
  // 迁入记忆的正文开头带来源序号；事件摘要同样只在展示时去掉（D130）。
  const memoryText = value => typeof value === 'string'
    ? value.replace(/^(?:statement|paragraph):[0-9a-f]{32,}:\s*/i, '') : value;
  let flow;

  function sectionHead(title, note) {
    return el('div', {class:'memory-section-head'},
      el('div', {}, el('h3', {text:title}), el('p', {class:'muted', text:note})));
  }

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
    const summary = memoryText(item.summary);
    return el('div', {class:'resource-record'}, el('button', {class:'btn small', type:'button', onclick:() =>
      openEditor(type, el('div', {class:'stack resource-detail'},
        el('time', {datetime:item.recorded_at, text:stamp}),
        el('p', {text:summary}),
        item.conversations.length && el('p', {text:'聊天流：' + item.conversations.join(' · ')})))
    }, el('span', {class:'resource-label'},
      el('span', {class:'resource-title', text:summary}),
      el('span', {class:'resource-meta', text:type + ' · ' + stamp}))));
  }

  function createFlow(panel) {
    const type = el('select', {}, el('option', {value:'', text:'全部事件'}),
      Object.entries(eventTypes).map(([value, text]) => el('option', {value, text})));
    const start = el('input', {type:'datetime-local', step:'1'});
    const end = el('input', {type:'datetime-local', step:'1'});
    const refresh = el('button', {class:'btn small', type:'submit', text:'刷新'});
    const shortcuts = [[24, '最近24小时'], [168, '最近7天'], [720, '最近30天']]
      .map(([hours, label]) => el('button', {class:'btn small', type:'button', text:label,
        onclick:() => apply(hours)}));
    const controls = el('fieldset', {class:'stack', style:'border:0;padding:0;margin:0;min-width:0'},
      el('div', {class:'row resource-filters memory-flow-filters'},
        el('label', {class:'stack'}, '事件类型', type),
        el('label', {class:'stack'}, '开始时间', start),
        el('label', {class:'stack'}, '结束时间', end)),
      el('div', {class:'row memory-flow-shortcuts'}, ...shortcuts, refresh));
    const filters = el('form', {}, controls);
    let applied = new URLSearchParams();
    let dateDraftChanged = false;
    for (const field of [start, end]) {
      field.addEventListener('input', () => { dateDraftChanged = true; });
    }

    function localTime(value) {
      const date = new Date(value);
      return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
    }

    function apply(hours) {
      dateDraftChanged = false;
      applied = new URLSearchParams();
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
    panel.replaceChildren(sectionHead('记忆流', '查看全局记忆事件，按事件类型和时间范围筛选；不区分聊天范围。'), filters);
    const pager = createResourcePager({el, panel, id:'memory-flow', render:renderEntry, error:fail,
      cacheKey: ({offset, limit}) => offset === 0
        ? 'memory-flow:' + new URLSearchParams(applied) + ':' + limit : null,
      empty:() => '没有符合条件的记忆事件。调整时间范围或事件类型后再试。', request:({offset, limit, signal}) => {
        const params = new URLSearchParams(applied);
        params.set('offset', offset);
        params.set('limit', limit);
        return api(flowPath + '?' + params, 'GET', undefined, {}, signal);
      }, loaded:data => {
        if (!applied.has('hours')) return;
        applied.delete('hours');
        applied.set('start', data.start);
        applied.set('end', data.end);
        if (dateDraftChanged) return;
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
    const input = el('input', {type:'file', accept:'.jsonl,application/x-ndjson'});
    const submit = el('button', {class:'btn primary small', type:'button', text:'确认追加导入', disabled:true});
    const cancel = el('button', {class:'btn small', type:'button', text:'取消', disabled:true});
    const result = el('p', {role:'status'});
    const summary = el('div', {class:'stack', 'aria-live':'polite'});
    const controls = el('div', {class:'stack'}, el('label', {class:'stack'}, '选择记忆包', input),
      summary, el('div', {class:'row'}, submit, cancel), result);
    let previewRequest, approvedFile;

    function clearPreview() {
      previewRequest?.abort();
      previewRequest = approvedFile = null;
      submit.disabled = cancel.disabled = true;
      summary.replaceChildren();
    }

    input.addEventListener('change', async () => {
      clearPreview();
      const file = input.files[0];
      result.textContent = '';
      if (!file) return;
      const controller = new AbortController();
      previewRequest = controller;
      cancel.disabled = false;
      result.textContent = '正在检视记忆包…';
      const response = await api(transferPath + '/preview', 'POST', file, {}, controller.signal);
      if (previewRequest !== controller || input.files[0] !== file) return;
      previewRequest = null;
      if (!response.ok) {
        result.textContent = '未通过检视：' + errorText(response);
        return;
      }
      const {total, counts} = response.data;
      const breakdown = Object.entries(packageLabels).filter(([key]) => counts[key] > 0)
        .map(([key, label]) => `${label} ${counts[key]} 条`).join(' · ');
      summary.replaceChildren(
        el('p', {text:`文字包共 ${total} 条。`}),
        breakdown && el('p', {text:breakdown}),
        el('p', {class:'muted', text:'这是包内数量，不是预计新增数量。确认后才检查与本地记录是否相同或冲突。'}));
      result.textContent = '检视通过，请确认是否追加导入。';
      approvedFile = file;
      submit.disabled = false;
    });
    function cancelPreview() {
      if (cancel.disabled) return;
      clearPreview();
      input.value = '';
      result.textContent = '已取消，未导入。';
    }
    cancel.addEventListener('click', cancelPreview);
    input.addEventListener('cancel', cancelPreview);
    submit.addEventListener('click', async () => {
      const file = approvedFile;
      if (!file || input.files[0] !== file || submit.disabled) return;
      clearPreview();
      input.disabled = true;
      result.textContent = '导入中…';
      const response = await api(transferPath, 'POST', file);
      if (response.ok) { clearPageCache('memory'); clearPageCache('memory-flow'); }
      result.textContent = response.ok
        ? `新增 ${response.data.added} 条，跳过相同记录 ${response.data.skipped} 条。`
        : '导入未确认成功：' + errorText(response);
      input.value = '';
      input.disabled = false;
    });
    panel.replaceChildren(
      sectionHead('导入导出', '文字记忆包与 MaiBot 知识包都只在全局范围内导入或导出；确认预检结果后才写入。'),
      el('div', {class:'memory-transfer-grid'},
        el('div', {class:'card stack memory-transfer-card'},
          el('h4', {text:'文字记忆包'}),
          el('p', {text:'文字记忆包包含原件、来源、证据和网页维护的修订及召回设置。已移除项留在本机，不随包导出；图片附件、媒体资产和本地时间线经历也不随包传输。'}),
          el('div', {class:'row'}, el('a', {class:'btn', href:transferPath, download:true, text:'导出文字记忆包'})),
          controls),
        window.createMemoryBundlePanel()));
  };
})();
