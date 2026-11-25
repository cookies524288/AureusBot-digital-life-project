'use strict';

function createResourcePageControls({el, label, select}) {
  const previous = el('button', {type:'button', class:'btn small', text:'上一页', disabled:true});
  const next = el('button', {type:'button', class:'btn small', text:'下一页', disabled:true});
  const status = el('span', {class:'muted', role:'status'});
  const jump = el('input', {type:'text', inputmode:'numeric', enterkeyhint:'go', class:'resource-page-jump', placeholder:'跳页',
    'aria-label':'跳转页码（输入后按回车）', title:'输入页码后按回车', disabled:true});
  const navigation = el('div', {class:'row resource-pagination', role:'group', 'aria-label':label}, previous, status, jump, next);
  let page = 0, pages = null;

  function go(target) {
    jump.value = '';
    jump.setCustomValidity('');
    select(target);
  }
  previous.addEventListener('click', () => go(page - 1));
  next.addEventListener('click', () => go(page + 1));
  jump.addEventListener('input', () => jump.setCustomValidity(''));
  jump.addEventListener('blur', () => jump.setCustomValidity(''));
  jump.addEventListener('keydown', event => {
    if (event.key !== 'Enter' || event.isComposing) return;
    event.preventDefault();
    const value = jump.value.trim();
    const target = Number(value) - 1;
    if (!/^\d+$/.test(value) || target < 0 || target >= pages) {
      jump.setCustomValidity(`请输入 1 到 ${pages} 之间的页码。`);
      jump.reportValidity();
      return;
    }
    jump.setCustomValidity('');
    if (target !== page) go(target);
    else jump.value = '';
  });
  return {navigation, sync(target, total, busy = false) {
    page = target;
    pages = total;
    previous.disabled = pages == null || page === 0;
    next.disabled = pages == null || page + 1 >= pages;
    jump.disabled = pages == null || pages <= 1;
    if (jump.disabled) { jump.value = ''; jump.setCustomValidity(''); }
    navigation.style.visibility = !busy && pages == null ? 'hidden' : '';
    status.textContent = pages == null ? (busy ? '读取中…' : '')
      : `第 ${pages ? page + 1 : 0} / ${pages} 页${busy ? '…' : ''}`;
  }};
}

function createPagedResource({el, list, label = '分页', request, render, measure, prepare, loaded, empty, error,
  clearOnError = false, isVisible = () => true}) {
  let page = 0, pages = null, limit = 0, active = false, pending;
  const controls = createResourcePageControls({el, label, select:read});

  function sync() {
    controls.sync(page, active ? pages : null, Boolean(pending));
    list.setAttribute('aria-busy', Boolean(pending));
  }
  function message(text) {
    list.replaceChildren(el('p', {class:'muted resource-empty', text}));
  }
  async function read(target = page) {
    if (!active || !isVisible()) return;
    pending?.abort();
    const controller = new AbortController();
    pending = controller;
    page = target;
    sync();
    prepare?.();
    message('读取中…');
    const response = await request({offset:target * limit, limit, signal:controller.signal});
    if (pending !== controller || !isVisible()) return;
    pending = null;
    if (!response.ok) {
      pages = null;
      if (clearOnError) page = 0;
      sync();
      list.replaceChildren(el('div', {class:'row resource-empty'}, el('span', {text:'读取失败'}),
        el('button', {class:'btn small', type:'button', text:'重试', onclick:() => read(clearOnError ? 0 : target)})));
      error(response);
      return;
    }
    const total = response.data.total;
    pages = Number.isInteger(total) && total >= 0 ? Math.ceil(total / limit) : null;
    if (target > 0 && pages != null && target >= pages) {
      return read(Math.max(0, pages - 1));
    }
    sync();
    if (response.data.items.length) list.replaceChildren(...response.data.items.map(render));
    else message(response.data.enabled ? (empty?.(response.data) ?? '没有符合条件的记录') : '此功能当前关闭');
    loaded?.(response.data);
  }
  function reset(newLimit = measure()) {
    limit = newLimit;
    page = 0;
    pages = null;
    return read();
  }
  return {
    list, navigation:controls.navigation, reset, read,
    show() {
      active = true;
      const capacity = measure();
      const pending = capacity === limit ? read() : reset(capacity);
      // 刚切到页面时列表可能还没完成布局；下一帧按实际高度再核对一次容量（批次 3）。
      const recheck = () => {
        if (!active || !list.isConnected) return;
        const next = measure();
        if (next !== limit) reset(next);
      };
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(recheck);
      setTimeout(recheck, 50);
      return pending;
    },
    hide() { active = false; pending?.abort(); pending = null; pages = null; sync(); prepare?.(); },
    resize() {
      if (!active || !list.clientWidth) return;
      const capacity = measure();
      if (capacity !== limit) reset(capacity);
    },
  };
}

function createResourcePager({el, panel, id, grid = false, ...options}) {
  const list = el('div', {id:id + '-result', class:'resource-list' + (grid ? ' resource-grid' : ''), 'aria-live':'polite'});
  const pager = createPagedResource({...options, el, list, measure});
  panel.append(list, pager.navigation);

  function measure() {
    const style = getComputedStyle(list);
    const gap = parseFloat(style.rowGap);
    const row = parseFloat(style.getPropertyValue(grid ? '--resource-tile' : '--resource-row'));
    const rows = Math.max(1, Math.floor((list.clientHeight + gap) / (row + gap)));
    const columns = grid ? Math.max(1, Math.floor((list.clientWidth + gap) / (row + gap))) : 1;
    return Math.min(100, rows * columns);
  }
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(pager.resize).observe(list);
  if (grid) {
    const sizes = el('div', {class:'row', role:'group', 'aria-label':'缩略图大小'});
    const buttons = ['small', 'medium', 'large'].map((size, index) => el('button', {
      type:'button', class:'btn small' + (size === 'medium' ? ' primary' : ''),
      'aria-pressed':size === 'medium' ? 'true' : 'false', 'data-image-size':size, text:['小', '中', '大'][index],
      onclick:() => {
        list.dataset.size = size;
        for (const button of buttons) {
          const selected = button.getAttribute('data-image-size') === size;
          button.classList.toggle('primary', selected);
          button.setAttribute('aria-pressed', selected);
        }
        pager.resize();
      }
    }));
    sizes.append(...buttons);
    panel.insertBefore(sizes, list);
  }
  return pager;
}

function installResourcePages({$, api, el, fail, fmtTime, openEditor, toast, PAGES}) {
  const labels = {paragraphs:'段落', relations:'关系', fact_claims:'事实',
    episodes:'情景记忆', experiences:'其他经历'};
  let memoryTab = 'query', queryKind = 'text';
  let activeMemory;
  const selected = new Map();

  function activate(attribute, value) {
    for (const button of document.querySelectorAll('[' + attribute + ']')) {
      const active = button.getAttribute(attribute) === value;
      button.classList.toggle('primary', active);
      button.setAttribute('aria-pressed', active);
      button.setAttribute('aria-disabled', active);
    }
  }
  function switchPanel(ids, value) {
    for (const [name, id] of Object.entries(ids)) $(id).classList.toggle('hidden', name !== value);
  }
  function action(label, onclick) {
    return el('button', {class:'btn small', type:'button', text:label, onclick});
  }
  function actions(label, ...buttons) {
    return el('div', {class:'row resource-actions', role:'group', 'aria-label':label}, ...buttons);
  }
  function field(label, value) {
    return value == null || value === '' ? null : el('p', {}, el('strong', {text:label + '：'}), String(readable(value)));
  }
  const terms = {...labels, entities:'实体', messages:'消息', mai_messages:'消息', active:'有效', inactive:'已停用', conflicted:'有冲突', superseded:'已替代',
    retracted:'已撤回', deleted:'已删除', missing:'缺失', image_assets:'图片资产', images:'图片', message:'消息',
    summary_facts:'会话总结事实', platform_message:'平台消息定位', platform_image:'图片组件定位',
    attachment_archived:'归档附件', media_type:'媒体类型', source_key:'原记录编号', reference:'引用',
    evidence:'证据', name:'名称', id:'编号', kind:'类别', status:'状态', profile:'人物画像',
    person_profile_snapshots:'人物画像', person_profile_overrides:'画像正文覆盖',
    person_profile_alias_overrides:'画像别名覆盖'};
  const term = key => terms[key] ?? key;
  const instanceLabels = {aureus:'Aureus', 'a-memorix-bundle':'记忆包'};
  const instanceLabel = value => instanceLabels[value] ?? value;
  function readable(value) {
    if (typeof value === 'boolean') return value ? '是' : '否';
    if (Array.isArray(value)) return value.map(readable).join('、');
    if (value && typeof value === 'object') return Object.entries(value)
      .filter(([, content]) => content != null && content !== '').map(([key, content]) => term(key) + '：' + readable(content)).join('\n');
    return value;
  }
  // 来源只保留用户认得的实例名和资料类型；表名、库角色和原主键是内部定位，不展示（批次 3）。
  function sourceId(value) {
    if (typeof value === 'string' && value.startsWith('[')) {
      try {
        const parts = JSON.parse(value);
        if (Array.isArray(parts) && parts.length >= 3) {
          return [instanceLabel(parts[0]), term(parts[2])].filter(Boolean).join(' · ');
        }
        return readable(parts);
      } catch { return value; }
    }
    return readable(value);
  }
  function sourceText(source) {
    if (!source || typeof source !== 'object') return source;
    return [instanceLabel(source.instance), term(source.table ?? source.kind)]
      .filter(value => value != null && value !== '').join(' · ') || readable(source);
  }
  // 长期记忆卡片：类型、来源和时间的展示口径与查询接口返回的字段一致（D122）。
  function memoryCategory(item) {
    return item.labels?.[0] ?? item.source?.table ?? 'live';
  }
  function memoryType(item) {
    return labels[memoryCategory(item)] ?? item.source?.table ?? '经历';
  }
  function memorySource(item) {
    return (item.source && sourceText(item.source)) || '本地经历';
  }
  // 画像的 occurred_at/expires_at 是秒级时间戳；文字记录的时间是 ISO 字符串，两者都兼容。
  function fmtStamp(value) {
    if (value == null || value === '') return '';
    const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
    return isNaN(date) ? String(value) : fmtTime(date.toISOString(), true);
  }
  function typeBadge(type, category) {
    return el('span', {class:'memory-type-badge memory-type-' + category, text:type});
  }
  function section(title, value) {
    const text = value == null || value === '' ? '暂无'
      : typeof value === 'string' ? value : readable(value);
    return el('section', {class:'memory-detail-section'},
      el('h4', {text:title}), el('p', {class:text === '暂无' ? 'muted' : undefined, text}));
  }
  function detail(title, ...content) {
    openEditor(title, el('div', {class:'stack resource-detail'}, ...content), true);
  }
  // 列表行：主文本在上，状态、来源等辅助信息另起一行，长文本最多折两行（批次 3）。
  function record(title, open, box) {
    const label = el('span', {class:'resource-label'},
      el('span', {class:'resource-title', text:typeof title === 'string' ? title : title.text}),
      typeof title === 'object' && title.meta && el('span', {class:'resource-meta', text:title.meta}));
    return el('div', {class:'resource-record'}, box,
      el('button', {class:'btn small', type:'button', onclick:open}, label));
  }
  function searchForm(label, placeholder, ...controls) {
    return el('form', {class:'row resource-filters'},
      el('input', {type:'search', name:'q', 'aria-label':label, placeholder, maxlength:200}),
      ...controls, el('button', {class:'btn small primary', text:'查询'}));
  }
  function searchable(options, form, endpoint, getParams) {
    let params;
    function apply() {
      params = getParams(form.elements);
      return pager.reset();
    }
    const pager = createResourcePager({...options, el, error:fail, request:({offset, limit, signal}) => {
      params ??= getParams(form.elements);
      return api(endpoint + '?' + new URLSearchParams({...params, offset, limit}), 'GET', undefined, {}, signal);
    }});
    form.addEventListener('submit', event => { event.preventDefault(); apply(); });
    form.addEventListener('change', event => { if (event.target.name !== 'q') apply(); });
    return Object.assign(pager, {apply, resetApplied(overrides) {
      params = {...params, ...overrides};
      return pager.reset();
    }});
  }
  function textControl(label, value, max, required = false) {
    const input = el('textarea', {maxlength:max, rows:max > 2000 ? 6 : 2, required});
    input.value = value;
    return {input, field:el('label', {class:'stack'}, label, input)};
  }
  async function change(endpoint, method, payload, refresh, button) {
    button.disabled = true;
    try {
      const response = await api(endpoint, method, payload);
      if (!response.ok) { fail(response); return false; }
      toast('已保存');
      await refresh();
      return true;
    } finally { button.disabled = false; }
  }
  function mediaTile(title, source, placeholder, open) {
    const fallback = el('span', {class:'resource-placeholder muted', text:placeholder});
    let preview = fallback;
    if (source) {
      preview = el('img', {src:source, alt:'', loading:'lazy'});
      preview.addEventListener('error', () => preview.replaceWith(fallback));
    }
    return el('button', {type:'button', class:'btn small resource-tile', 'aria-label':title, onclick:open},
      preview, el('span', {text:title}));
  }
  function fullImage(url) {
    if (!url) return null;
    const image = el('img', {src:url, alt:'原图'});
    image.addEventListener('error', () => image.replaceWith(field('原图', '暂不可用')));
    return image;
  }
  const selection = $('memory-select-all'), correct = $('memory-correct');
  const toggleRemoved = $('memory-toggle-removed');
  const selectedCount = $('memory-selected-count');
  let removedView = false;
  function syncSelection() {
    const boxes = [...memory.list.querySelectorAll('input[type=checkbox]:not(:disabled)')];
    selection.disabled = !boxes.length;
    selection.checked = boxes.length > 0 && boxes.every(box => box.checked);
    selection.indeterminate = selected.size > 0 && !selection.checked;
    correct.disabled = removedView || !selected.size;
    toggleRemoved.disabled = !selected.size;
    selectedCount.textContent = selected.size ? `已选 ${selected.size} 条` : '';
  }
  function memoryTarget(item) {
    return {kind:item.kind ?? (item.source ? 'history' : 'live'), entry_id:item.entry_id ?? item.record_id};
  }
  const maintenanceEndpoint = '/api/resources/memory/maintenance';
  const maintenanceLabels = {correct:'修正正文', enable:'恢复召回', disable:'停用召回', freeze:'冻结关系',
    unfreeze:'解除冻结', protect:'保护关系', unprotect:'解除保护', remove:'移除', restore:'恢复'};
  function locationKey({instance, role, table, key}) {
    return JSON.stringify([instance, role, table, key]);
  }
  function lifecycleActions(item, omitted = []) {
    const relation = item.source.table === 'relations';
    const operations = (item.status !== 'active' ? [] : item.is_removed ? ['restore'] : relation ? [
      item.is_frozen ? 'unfreeze' : 'freeze', item.is_protected ? 'unprotect' : 'protect',
      ...(!item.is_frozen && !item.is_protected ? [item.is_enabled ? 'disable' : 'enable', 'remove'] : []),
    ] : ['correct', item.is_enabled ? 'disable' : 'enable', 'remove'])
      .filter(operation => !omitted.includes(operation));
    return actions('记忆检修', ...operations.map(operation =>
      action(maintenanceLabels[operation], () => lifecycleForm(item, operation))),
    action('纠错历史', () => lifecycleHistory(item)));
  }
  function showLifecycleError(response, message, container) {
    if (!container.isConnected) return;
    if ([401, 403, 404].includes(response.status)) {
      detail('记忆检修不可用', field('状态', '此资料或登录状态已不可用。'));
      void memory.read();
      return;
    }
    message.textContent = errorText(response);
  }
  async function lifecycleHistory(item, offset = 0) {
    const body = el('div', {class:'stack resource-detail'});
    const message = el('p', {role:'status', text:'正在读取纠错历史…'});
    body.append(message);
    openEditor('纠错历史', body);
    const params = new URLSearchParams({source:locationKey(item.source), offset, limit:20});
    const response = await api(maintenanceEndpoint + '/history?' + params);
    if (!body.isConnected || !$('editor-dialog').open) return;
    if (!response.ok) { showLifecycleError(response, message, body); return; }
    const {items, total} = response.data;
    message.textContent = total ? `${offset + 1}–${offset + items.length} / ${total} 项` : '没有已执行的操作';
    body.append(...items.map(operation => el('div', {class:'stack'},
      field(maintenanceLabels[operation.action], fmtTime(operation.applied_at, true)),
      field('撤销日期', operation.undone_at && fmtTime(operation.undone_at, true)),
      operation.can_undo && action('撤销此操作', async event => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          const result = await api(`${maintenanceEndpoint}/${encodeURIComponent(operation.plan_id)}/undo`, 'POST');
          if (!result.ok) { showLifecycleError(result, message, body); return; }
          if (body.isConnected && $('editor-dialog').open) await lifecycleHistory(item, offset);
          await memory.read();
        } finally { button.disabled = false; }
      }))));
    body.append(actions('纠错历史分页',
      offset > 0 && action('上一页', () => lifecycleHistory(item, offset - 20)),
      offset + 20 < total && action('下一页', () => lifecycleHistory(item, offset + 20))));
  }
  function lifecycleForm(item, operation) {
    const label = maintenanceLabels[operation];
    const editor = operation === 'correct' ? textControl('修正正文', item.text, 20000, true) : null;
    const result = el('p', {class:'muted', role:'status'});
    const submit = el('button', {class:'btn primary small', text:'生成预览'});
    const form = el('form', {class:'stack'}, field('来源', sourceText(item.source)), editor?.field, submit, result);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      submit.disabled = true;
      try {
        const response = await api(maintenanceEndpoint + '/preview', 'POST', {
          source:locationKey(item.source), action:operation, ...(editor ? {text:editor.input.value} : {}),
        });
        if (!form.isConnected || !$('editor-dialog').open) return;
        if (!response.ok) { showLifecycleError(response, result, form); return; }
        const preview = response.data;
        const editorPane = el('div', {class:'stack'});
        async function changePlan(method, button) {
          button.disabled = true;
          try {
            const changed = await api(`${maintenanceEndpoint}/${encodeURIComponent(preview.plan_id)}/${method}`, 'POST');
            if (!changed.ok) { showLifecycleError(changed, message, editorPane); return; }
            if (editorPane.isConnected && $('editor-dialog').open) {
              message.textContent = method === 'apply' ? '已执行' : '已撤销';
              buttons.replaceChildren(...(method === 'apply' ? [undo] : []));
            }
            await memory.read();
          } finally { button.disabled = false; }
        }
        const message = el('p', {role:'status'});
        const confirm = action('确认' + label, event => changePlan('apply', event.currentTarget));
        const undo = action('撤销本次操作', event => changePlan('undo', event.currentTarget));
        const buttons = actions('纠错计划', action('返回草稿', () => openEditor(label, form)), confirm);
        editorPane.append(field('原件版本', preview.original_version),
          field('旧值', preview.before_text), field('新值', preview.after_text),
          field('影响', `召回${preview.before.is_enabled && !preview.before.is_removed ? '开启' : '关闭'} → ${preview.state.is_enabled && !preview.state.is_removed ? '开启' : '关闭'}；原件保留`),
          field('过期时间', fmtTime(preview.expires_at, true)), buttons, message);
        openEditor(label + '预览', editorPane);
      } finally { submit.disabled = false; }
    });
    openEditor(label, form);
  }
  function editMemories(items) {
    const inputs = items.map(item => ({item, ...textControl(labels[item.labels?.[0]] || '记忆正文', item.text, 20000, true)}));
    const save = el('button', {class:'btn primary small', text:'保存修正'});
    const form = el('form', {class:'stack'}, inputs.map(row => row.field), save);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      save.disabled = true;
      for (const {item, input} of inputs) {
        if (item.text === input.value) continue;
        const response = await api('/api/resources/memory', 'PATCH', {...memoryTarget(item), text:input.value});
        if (!response.ok) { fail(response); save.disabled = false; return; }
      }
      if (form.isConnected) $('editor-dialog').close();
      toast('已保存');
      await memory.read();
    });
    openEditor('修正记忆', form);
  }
  function confirmRemoval(items, removed) {
    const label = removed ? '移除' : '恢复';
    const button = action('确认' + label, async () => {
      button.disabled = true;
      try {
        const response = await api('/api/resources/memory/batch', 'PATCH', {
          removed, targets:items.map(memoryTarget),
        });
        if (!response.ok) { fail(response); return; }
        if (button.isConnected) $('editor-dialog').close();
        toast(`已${label} ${response.data.changed} 条文字记忆`);
        await memory.read();
      } finally { button.disabled = false; }
    });
    openEditor(label + '所选文字记忆', el('div', {class:'stack'},
      el('p', {text:`仅${label}本页选中的 ${items.length} 条文字记忆，不影响其他记录。`}),
      el('p', {text:removed ? '所选记录将不再参与召回或导出。原件、证据和时间线保留，可在「查看已移除」中恢复。'
        : '恢复仅撤销移除，不会启用原已停用、过期或受限的记录。'}),
      el('div', {class:'row'}, action('取消', () => $('editor-dialog').close()), button)));
  }
  function memoryRelated(item) {
    const lines = [];
    if (item.actor_label) lines.push('人物：' + item.actor_label);
    if (item.person_key) lines.push('人物键：' + item.person_key);
    for (const entry of item.evidence ?? []) {
      lines.push([term(entry.kind), sourceText(entry.source), term(entry.status)]
        .filter(Boolean).join(' · '));
    }
    return lines.join('\n');
  }
  function openGraphFor(item) {
    const text = (item.actor_label || item.person_key || item.text || '').trim().split(/\s+/)[0] || '';
    $('editor-dialog').close();
    showQuery('graph');
    graph.search(text.slice(0, 60));
  }
  // 详情操作沿用现有能力：图谱跳到关系图；修正和删除按记录类型走既有接口或检修预览。
  function memoryActions(item, {guided, relation}) {
    const buttons = [action('图谱', () => openGraphFor(item))];
    if (item.is_removed) {
      if (!guided) buttons.push(action('恢复', () => confirmRemoval([item], false)));
    } else {
      if (!relation) buttons.push(action('修正', () => guided ? lifecycleForm(item, 'correct') : editMemories([item])));
      if (!relation || (!item.is_frozen && !item.is_protected)) {
        buttons.push(action('删除', () => guided ? lifecycleForm(item, 'remove') : confirmRemoval([item], true)));
      }
    }
    return actions('记忆操作', ...buttons);
  }
  function memoryCard(item, open, box, titleOverride) {
    const category = memoryCategory(item);
    const type = memoryType(item);
    const status = item.is_removed ? '已移除' : item.is_recallable ? '' : '未参与召回';
    const title = titleOverride || item.actor_label || type;
    return el('article', {class:'memory-card' + (item.is_removed ? ' is-removed' : '')},
      box,
      el('div', {class:'memory-card-main'},
        el('div', {class:'memory-card-head'},
          typeBadge(type, category),
          el('button', {class:'memory-card-title', type:'button', text:title, onclick:open}),
          status && el('span', {class:'memory-card-status', text:status})),
        el('p', {class:'memory-card-summary', text:item.text || '空文本'}),
        el('div', {class:'memory-card-meta'},
          el('span', {class:'memory-source-tag', text:memorySource(item)}),
          item.recorded_at && el('time', {datetime:item.recorded_at, text:fmtTime(item.recorded_at, true)}))));
  }
  function memoryDetail(item, {guided, relation}) {
    const type = memoryType(item);
    const source = memorySource(item);
    const body = el('div', {class:'stack resource-detail memory-detail'},
      el('div', {class:'memory-detail-meta'}, typeBadge(type, memoryCategory(item)),
        el('span', {class:'muted', text:source})),
      field('类型与来源', type + ' · ' + source),
      field('时间', item.recorded_at ? fmtTime(item.recorded_at, true) : '暂无'),
      field('全文', item.text || '暂无'),
      field('来源', source),
      field('关联条目', memoryRelated(item) || '暂无'),
      field('原件', item.original_text),
      field('召回状态', item.is_recallable ? '参与召回' : '不参与召回'),
      relation && field('关系状态', item.is_frozen ? '已冻结' : item.is_protected ? '受保护' : '正常'),
      memoryActions(item, {guided, relation}),
      guided ? lifecycleActions(item, ['correct', 'remove'])
        : !item.is_removed && el('div', {class:'row resource-actions'}, action(item.is_enabled ? '停用召回' : '恢复召回',
          async event => {
            const button = event.currentTarget;
            if (await change('/api/resources/memory', 'PATCH', {...memoryTarget(item), is_enabled:!item.is_enabled},
              memory.read, button) && button.isConnected) $('editor-dialog').close();
          })),
      el('div', {class:'row resource-actions'}, action('关闭', () => $('editor-dialog').close())));
    detail(type || '文字记忆', body);
  }
  function renderMemory(item) {
    if (item.kind === 'summary') return renderSummaryFact(item);
    const type = memoryType(item);
    const relation = item.source?.table === 'relations';
    const guided = ['fact_claims', 'relations', 'episodes'].includes(item.source?.table);
    const box = el('input', {type:'checkbox', 'aria-label':'选择' + type, disabled:guided,
      title:guided ? '此类型在详情中修正或移除' : null});
    if (!guided) box.addEventListener('change', () => {
      if (box.checked) selected.set(box, item); else selected.delete(box);
      syncSelection();
    });
    return memoryCard(item, () => memoryDetail(item, {guided, relation}), box);
  }
  function renderSummaryFact(item) {
    const box = el('input', {type:'checkbox', 'aria-label':'选择会话总结事实', disabled:true,
      title:'会话总结事实在详情中维护'});
    return memoryCard(item, async () => {
      const state = el('p', {class:'muted', role:'status', text:'正在复核事实来源…'});
      detail('会话总结事实', state);
      const params = new URLSearchParams({source:locationKey(item.source)});
      const response = await api('/api/resources/memory/facts/detail?' + params);
      if (!state.isConnected || !$('editor-dialog').open) return;
      if (!response.ok) {
        state.textContent = response.status === 404 ? '没有此事实，或当前不可见。' : errorText(response);
        if (response.status === 404) await memory.read();
        return;
      }
      const fact = response.data;
      const merged = {...item, ...fact};
      const source = memorySource(merged);
      const body = el('div', {class:'stack resource-detail memory-detail'},
        el('div', {class:'memory-detail-meta'}, typeBadge('事实', 'fact_claims'),
          el('span', {class:'muted', text:source})),
        field('类型与来源', '会话总结事实 · ' + source),
        field('时间', merged.recorded_at ? fmtTime(merged.recorded_at, true) : '暂无'),
        field('全文', fact.text || '暂无'),
        field('来源', source),
        field('关联条目', memoryRelated(merged) || '暂无'),
        field('状态', fact.status === 'superseded' ? '已更正' : term(fact.status)),
        field('召回状态', merged.is_recallable ? '参与召回' : '不参与召回'),
        memoryActions(merged, {guided:true, relation:false}),
        lifecycleActions(merged, ['correct', 'remove']),
        el('div', {class:'row resource-actions'}, action('关闭', () => $('editor-dialog').close())));
      detail('会话总结事实', body);
    }, box, '会话总结事实');
  }
  const counts = $('memory-counts');
  const memory = searchable({panel:$('memory-query-text'), id:'memory', render:renderMemory, prepare:() => {
    selected.clear();
    selection.checked = selection.indeterminate = false;
    selection.disabled = correct.disabled = toggleRemoved.disabled = true;
    counts.textContent = selectedCount.textContent = '';
  }, empty:() => removedView
    ? '没有已移除的文字记忆。'
    : '没有符合条件的文字记忆。可以先在私聊里聊几句，或调整筛选条件后查询。',
  loaded:data => {
    if (data.counts) counts.textContent = Object.entries(data.counts).filter(([kind]) => kind in labels)
      .map(([kind, count]) => labels[kind] + ' ' + count).join('　');
    syncSelection();
  }}, $('memory-search'), '/api/resources/memory', fields => {
    removedView = fields.removed.checked;
    fields.disabled.disabled = removedView;
    toggleRemoved.textContent = removedView ? '恢复所选' : '移除所选';
    return {q:fields.q.value, kind:'all', category:fields.type.value, removed:removedView,
      recall:removedView || fields.disabled.checked ? '' : 'enabled'};
  });
  $('memory-refresh').addEventListener('click', memory.apply);
  correct.addEventListener('click', () => editMemories([...selected.values()]));
  toggleRemoved.addEventListener('click', () => confirmRemoval([...selected.values()], !removedView));
  selection.addEventListener('change', () => {
    const checked = selection.checked;
    for (const box of memory.list.querySelectorAll('input[type=checkbox]:not(:disabled)')) {
      box.checked = checked;
      box.dispatchEvent(new Event('change'));
    }
  });

  const profilePanel = $('memory-query-profiles');
  const profileForm = searchForm('搜索人物画像', '搜索人物、画像或来源');
  profilePanel.append(profileForm);
  const profileMaintenance = '/api/resources/profiles/maintenance';
  async function openProfileMaintenance(item) {
    const profile = item.record;
    const source = JSON.stringify([profile.instance, profile.kind, profile.source_key]);
    const status = el('p', {role:'status', text:'正在复核画像来源…'});
    openEditor('检修人物画像', status);
    const response = await api(profileMaintenance + '?' + new URLSearchParams({source}));
    if (!status.isConnected || !$('editor-dialog').open) return;
    if (!response.ok) { showLifecycleError(response, status, status); return; }
    const {effective, choices, history, edit} = response.data;
    const summary = textControl('画像正文（留空使用来源值）', effective.summary || '', 20000);
    const aliases = textControl('人工别名（每行一个，留空使用来源值）', effective.aliases.join('\n'), 13000);
    const selected = choices.map((choice, index) => {
      const input = el('input', {type:'checkbox', checked:effective.evidence.includes(index)});
      return {input, label:el('label', {class:'row'}, input, readable(choice))};
    });
    const message = el('p', {role:'status'});
    const submit = el('button', {class:'btn primary small', text:'生成预览'});
    const form = el('form', {class:'stack resource-detail'}, field('画像来源', sourceText(profile)),
      summary.field, aliases.field, el('fieldset', {}, el('legend', {text:'引用已有证据'}),
        selected.map(entry => entry.label)), submit, message);
    async function operate(plan, operation, container, feedback, button) {
      button.disabled = true;
      try {
        const changed = await api(`${profileMaintenance}/${encodeURIComponent(plan)}/${operation}`, 'POST');
        if (!changed.ok) { showLifecycleError(changed, feedback, container); return; }
        toast(operation === 'apply' ? '画像检修已应用' : '画像检修已撤销');
        if (container.isConnected && $('editor-dialog').open) await openProfileMaintenance(item);
        await profiles.read();
      } finally { button.disabled = false; }
    }
    form.append(el('h4', {text:'纠错历史'}), ...history.map(operation => el('div', {class:'row'},
      field('执行日期', fmtTime(operation.applied_at, true)),
      field('撤销日期', operation.undone_at && fmtTime(operation.undone_at, true)),
      operation.can_undo && action('撤销此操作', event =>
        operate(operation.id, 'undo', form, message, event.currentTarget)))));
    form.addEventListener('submit', async event => {
      event.preventDefault();
      submit.disabled = true;
      try {
        const names = aliases.input.value.split('\n').map(name => name.trim()).filter(Boolean);
        const evidence = selected.flatMap(({input}, index) => input.checked ? [index] : []);
        const text = summary.input.value.trim() || null;
        const unchanged = (left, right) => JSON.stringify(left) === JSON.stringify(right);
        const planned = await api(profileMaintenance + '/preview', 'POST', {source, edit:{
          summary:text === effective.summary ? edit.summary : text,
          aliases:unchanged(names, effective.aliases) ? edit.aliases : names.length ? names : null,
          evidence:unchanged(evidence, effective.evidence) ? edit.evidence
            : evidence.length === choices.length ? null : evidence,
        }});
        if (!form.isConnected || !$('editor-dialog').open) return;
        if (!planned.ok) { showLifecycleError(planned, message, form); return; }
        const preview = planned.data;
        const feedback = el('p', {role:'status'});
        const pane = el('div', {class:'stack resource-detail'},
          field('旧画像', preview.before), field('新画像', preview.after), feedback);
        pane.append(actions('画像纠错计划', action('返回草稿', () => openEditor('检修人物画像', form)),
          action('确认应用', event => operate(preview.plan_id, 'apply', pane, feedback, event.currentTarget))));
        openEditor('画像检修预览', pane);
      } finally { submit.disabled = false; }
    });
    openEditor('检修人物画像', form);
  }
  async function openProfileAliases(item, button) {
    const endpoint = `/api/resources/profiles/${item.id}/aliases`;
    const generation = editorGeneration;
    button.disabled = true;
    try {
      const response = await api(endpoint);
      if (!response.ok) { fail(response); return; }
      if (button.isConnected && $('editor-dialog').open && generation === editorGeneration) {
        editProfileAliases(item, endpoint, response.data);
      }
    } finally { button.disabled = false; }
  }
  function editProfileAliases(item, endpoint, aliases) {
    const editor = el('textarea', {rows:4, required:true});
    editor.value = aliases.effective_aliases.join('\n');
    const save = el('button', {class:'btn primary small', text:'保存别名'});
    const undo = action('撤销本机别名', () => write(null));
    const form = el('form', {class:'stack resource-detail'},
      field('画像来源', sourceText(item.record)),
      field('原有别名', aliases.original_aliases.join('、') || '无'),
      field('当前别名', aliases.effective_aliases.join('、') || '无'),
      field('本机维护', aliases.local_aliases === null ? '未覆盖' : '已覆盖'),
      el('label', {class:'stack'}, '人工别名（每行一个）', editor),
      el('p', {text:'保存会替换当前别名。撤销只移除本机覆盖，原画像和证据均保留。'}),
      el('div', {class:'row'}, save, undo, action('关闭', () => $('editor-dialog').close())));
    undo.disabled = aliases.local_aliases === null;
    async function write(value) {
      editor.disabled = undo.disabled = save.disabled = true;
      try {
        const response = await api(endpoint, 'PATCH', {aliases:value});
        if (!response.ok) { fail(response); return; }
        toast(value === null ? '已撤销本机别名' : '已保存别名');
        if (form.isConnected && $('editor-dialog').open) editProfileAliases(item, endpoint, response.data);
      } finally {
        editor.disabled = save.disabled = false;
        undo.disabled = aliases.local_aliases === null;
      }
    }
    form.addEventListener('submit', event => {
      event.preventDefault();
      write(editor.value.split('\n'));
    });
    openEditor('维护人物别名', form);
  }
  const profiles = searchable({panel:profilePanel, id:'profiles', render:item => {
    if (item.entry_type === 'person') {
      const title = item.name || '未命名人物';
      return record({text:title, meta:'人物档案'}, () => detail('人物档案',
        field('当前称呼', item.name), field('首次记录时间', fmtTime(item.first_seen, true))));
    }
    const profile = item.record;
    const title = item.name || profile.name || profile.nickname || '待关联人物';
    return record({text:title, meta:'人物画像'}, () => detail(title, field('画像', profile.summary || profile.impression),
      field('原件别名', profile.aliases?.join('、')),
      field('关联状态', item.pending_reason), field('来源', sourceText(profile)),
      item.can_edit_aliases ? actions('画像检修', action('检修画像', () => openProfileMaintenance(item)),
        action('维护别名', event => openProfileAliases(item, event.currentTarget))) : null));
  }, empty:() => profileForm.elements.q.value.trim()
    ? '没有符合条件的人物画像，试试清空搜索。'
    : '还没有人物画像。人物在聊天中出现并被整理后会显示在这里。'},
  profileForm, '/api/resources/profiles', fields => ({q:fields.q.value}));

  const picturePanel = $('memory-pictures');
  const pictureForm = searchForm('搜索图片', '搜索描述、会话或附件', el('label', {}, '原图 ',
    el('select', {name:'availability', 'aria-label':'原图可用性'},
      ...Object.entries({'':'全部', archived:'已归档', reference:'仅有引用'})
        .map(([value, text]) => el('option', {value, text})))));
  picturePanel.append(pictureForm);
  let similarTo = null;
  const resetImages = action('返回图片列表', () => {
    similarTo = null;
    resetImages.classList.add('hidden');
    pictureForm.classList.remove('hidden');
    return pictures.reset();
  });
  resetImages.classList.add('hidden');
  picturePanel.append(resetImages);
  let pictureParams = {q:'', availability:''};
  const pictures = createResourcePager({el, panel:picturePanel, id:'pictures', grid:true, error:fail,
    request:({offset, limit, signal}) => api(similarTo == null
      ? '/api/resources/pictures?' + new URLSearchParams({...pictureParams, offset, limit})
      : '/api/resources/images?' + new URLSearchParams({similar_to:similarTo, limit}), 'GET', undefined, {}, signal),
    empty:() => similarTo != null
      ? '没有找到相似图片，试试换一张图片或放宽条件。'
      : pictureForm.elements.q.value.trim() || pictureForm.elements.availability.value
        ? '没有符合条件的图片，试试清空搜索或原图筛选。'
        : '还没有图片记忆。聊天中收到并归档图片后会显示在这里。',
    render:item => {
      const title = item.text || item.conversation || '未填写描述';
      const url = item.digest ? '/api/resources/media/' + encodeURIComponent(item.digest) : null;
      return mediaTile(title, url, '原图未归档', () => detail(title, fullImage(url),
        field('时间', item.timestamp && fmtTime(item.timestamp, true)), field('来源', sourceText(item.source)),
        field('状态', term(item.status)),
        !url && field('原图', '原图未归档'), item.score != null ? field('相似度', Number(item.score).toFixed(3))
          : item.kind === 'inbox' && action('查找相似图片', () => {
            $('editor-dialog').close();
            similarTo = item.id;
            pictureForm.classList.add('hidden');
            resetImages.classList.remove('hidden');
            return pictures.reset();
          })));
    },
  });
  function searchPictures() {
    pictureParams = {q:pictureForm.elements.q.value, availability:pictureForm.elements.availability.value};
    return pictures.reset();
  }
  pictureForm.addEventListener('submit', event => { event.preventDefault(); searchPictures(); });
  pictureForm.elements.availability.addEventListener('change', searchPictures);
  const operations = createResourcePager({el, panel:$('memory-panel-operations'), id:'memory-operations', error:fail,
    request:({offset, limit, signal}) => api('/api/resources/memory/operations?' + new URLSearchParams({offset, limit}),
      'GET', undefined, {}, signal),
    empty:() => '没有可见的移除或恢复记录。在文字查询中选中记录并移除后，操作会记录在这里。',
    render:item => el('div', {class:'resource-record'},
      el('span', {text:`${item.kind === 'remove' ? '移除' : '恢复'} ${item.changed} 条`}),
      el('time', {datetime:item.occurred_at, class:'muted',
        text:`${new Date(item.occurred_at).getFullYear()}年${fmtTime(item.occurred_at, true)}`})),
  });
  function graphGrantManager(endpoint, pauseGraph, resumeGraph) {
    const manager = el('details', {class:'stack'}, el('summary', {text:'管理图谱展示授权'}));
    const message = el('p', {class:'muted', role:'status'});
    const batches = el('div', {class:'stack'});
    const choices = {entities:'实体', relations:'关系（两端也须获准）', evidence:'独立证据'};
    let pending, saving = false;
    manager.append(el('p', {class:'muted', text:'只列已登记的发布批次，不预览内容。浏览授权不改变资料本身或内部召回。'}),
      message, batches);

    function clear() {
      pending?.abort();
      pending = null;
      message.textContent = '';
      batches.replaceChildren();
    }
    async function load() {
      clear();
      const request = new AbortController();
      pending = request;
      message.textContent = '正在读取发布批次…';
      const response = await api(endpoint, 'GET', undefined, {}, request.signal);
      if (pending !== request || !manager.open) return;
      pending = null;
      if (!response.ok) {
        message.textContent = '无法读取发布批次。';
        batches.append(action('重试读取批次', load));
        return fail(response);
      }
      message.textContent = !response.data.enabled ? '当前未接入展示授权管理。'
        : response.data.items.length ? '' : '没有可授权的发布批次。旧批次需从原已校验来源重新发布。';
      batches.replaceChildren(...response.data.items.map(render));
    }
    function render(batch) {
      const group = el('fieldset', {class:'stack'});
      group.append(el('legend', {text:instanceLabel(batch.instance)}));
      const options = Object.entries(choices).map(([value, text]) => {
        const input = el('input', {type:'checkbox', value});
        input.checked = batch.categories.includes(value);
        group.append(el('label', {class:'row'}, input, el('span', {text})));
        return input;
      });
      const password = el('input', {type:'password', required:true});
      password.autocomplete = 'current-password';
      group.append(el('label', {class:'stack'}, el('span', {text:'确认当前网页密码'}), password));
      const form = el('form', {class:'stack'}, group);
      const save = el('button', {type:'submit', class:'btn small primary', text:'保存展示授权'});
      const revoke = action('撤回本批授权', () => {
        if (form.reportValidity()) saveGrant([]);
      });
      revoke.disabled = !batch.categories.length;
      group.append(el('div', {class:'row'}, save, revoke));
      form.addEventListener('submit', event => {
        event.preventDefault();
        saveGrant(options.filter(input => input.checked).map(input => input.value));
      });

      async function saveGrant(categories) {
        if (saving) return;
        saving = group.disabled = true;
        const secret = password.value;
        password.value = '';
        pauseGraph();
        let changed = false;
        try {
          const response = await api(`${endpoint}/${encodeURIComponent(batch.batch_id)}`, 'PUT',
            {categories, password:secret});
          if (!response.ok) {
            fail(response);
            if (response.status === 404 && manager.open) await load();
            return;
          }
          changed = true;
          toast(categories.length ? '图谱展示授权已更新。' : '本批图谱展示授权已撤回。');
        } finally {
          saving = group.disabled = false;
          resumeGraph(changed);
        }
        if (manager.open) await load();
      }
      return form;
    }
    manager.addEventListener('toggle', () => manager.open ? load() : clear());
    return {element:manager, close() { manager.open = false; clear(); }};
  }
  function graphView(panel) {
    const endpoint = '/api/resources/memory/graph';
    const pageSize = 16, accessInterval = 5000;
    const refresh = action('刷新', () => pager.reset());
    const unanchor = action('返回全部图谱', () => browse(null));
    const form = searchForm('搜索关系图', '实体名或关系谓词', refresh, unanchor);
    form.append(action('返回文字记录', () => showQuery('text')));
    const count = el('span', {class:'muted', role:'status', style:'min-height:1.5em'});
    const detailStatus = el('span', {class:'muted', role:'status'});
    const list = el('div', {id:'memory-graph-result', class:'resource-list memory-graph-list',
      tabindex:'0', role:'region', 'aria-label':'当前页图谱列表', 'aria-live':'polite'});
    const canvas = createMemoryGraphCanvas({el, open, label});
    const updates = typeof BroadcastChannel === 'undefined' ? null : new BroadcastChannel('memory-graph-grants');
    const grants = graphGrantManager(endpoint + '/grants', pauseForGrant, finishGrant);
    let anchor = null, query = '', active = false, suspended = false, savingGrant = false, revision;
    let detailRequest, accessRequest, accessTimer;
    const sources = new Set();
    unanchor.classList.add('hidden');

    function closeDetail() {
      detailRequest?.abort();
      detailRequest = null;
      detailStatus.textContent = '';
      const dialog = $('editor-dialog');
      const body = $('editor-body');
      if (!body.querySelector('[data-graph-detail]')) return;
      body.replaceChildren();
      if (dialog.open) dialog.close();
    }
    function invalidate() {
      clearTimeout(accessTimer);
      accessRequest?.abort();
      accessRequest = null;
      revision = null;
      sources.clear();
      count.textContent = '';
      list.scrollTop = 0;
      canvas.clear();
      closeDetail();
    }
    function applyBrowse(source) {
      anchor = source;
      unanchor.classList.toggle('hidden', !anchor);
      query = form.elements.q.value = '';
      return pager.reset();
    }
    function browse(source) {
      if (!source || sources.has(source)) return applyBrowse(source);
    }
    function accessChanged() {
      return anchor ? applyBrowse(null) : pager.reset();
    }
    function pauseForGrant() {
      savingGrant = true;
      pager.hide();
      list.replaceChildren();
    }
    function finishGrant(notify) {
      if (notify) updates?.postMessage('changed');
      savingGrant = false;
      applyBrowse(null);
      if (active && !suspended && !document.hidden) pager.show();
    }
    updates?.addEventListener('message', () => {
      if (active && !suspended && !savingGrant) accessChanged();
    });
    function verifyAccess(signal) {
      return api(endpoint + '/access', 'GET', undefined, {}, signal);
    }
    async function checkAccess() {
      clearTimeout(accessTimer);
      if (!revision || accessRequest) return;
      const request = new AbortController();
      accessRequest = request;
      const response = await verifyAccess(request.signal);
      if (request !== accessRequest) return;
      accessRequest = null;
      if (!response.ok || response.graphRevision !== revision) return accessChanged();
      accessTimer = setTimeout(checkAccess, accessInterval);
    }
    function label(item) {
      return item.kind === 'entity' ? `实体 · ${item.name}`
        : `${item.subject.name} → ${item.predicate} → ${item.object.name}`;
    }
    async function open(source) {
      if (!sources.has(source)) return;
      closeDetail();
      const request = new AbortController();
      detailRequest = request;
      detailStatus.textContent = '正在读取关系图详情…';
      const response = await api(endpoint + '/detail?' + new URLSearchParams({source}), 'GET', undefined, {}, request.signal);
      if (request !== detailRequest) return;
      const access = response.ok ? await verifyAccess(request.signal) : null;
      if (request !== detailRequest) return;
      detailRequest = null;
      detailStatus.textContent = '';
      if (!response.ok) {
        fail(response);
        return response.status === 404 || response.status === 409 ? accessChanged() : pager.reset();
      }
      if (!access?.ok || revision !== access.graphRevision || revision !== response.graphRevision
          || response.data.source !== source) return accessChanged();
      const item = response.data;
      const evidence = item.evidence.map(key => field('证据来源', sourceId(key)));
      const nodes = item.kind === 'entity' ? [item] : item.subject.source === item.object.source
        ? [item.subject] : [item.subject, item.object];
      openEditor('关系图详情', el('div', {class:'stack resource-detail', 'data-graph-detail':''},
        field('资料', label(item)), field('来源', sourceId(item.source)),
        ...(item.kind === 'relation' ? [field('起点来源', sourceId(item.subject.source)),
          field('终点来源', sourceId(item.object.source))] : []),
        ...evidence,
        ...nodes.map(node => action(`浏览 ${node.name} 的一跳关系`, () => browse(node.source))),
        ...(item.kind === 'relation' ? nodes.map((node, index) =>
          action(`查看${index === 0 ? '起点' : '终点'}实体：${node.name}`, () => open(node.source))) : [])));
    }
    const pager = createPagedResource({el, list, measure:() => pageSize, label:'关系图分页', error:fail,
      prepare:invalidate, clearOnError:true, isVisible:() => active && !suspended && !savingGrant && !document.hidden,
      request:async ({offset, limit, signal}) => {
        const params = new URLSearchParams({q:query, offset, limit});
        if (anchor) params.set('neighbor_of', anchor);
        const response = await api(endpoint + '?' + params, 'GET', undefined, {}, signal);
        if (response.ok && !signal.aborted) {
          const access = await verifyAccess(signal);
          if (!access.ok) return access;
          if (!response.graphRevision || response.graphRevision !== access.graphRevision) {
            return {ok:false, status:409, data:{detail:'图谱可见范围已变更，请重试。'}};
          }
          Object.assign(response.data, {enabled:true, revision:response.graphRevision});
        }
        return response;
      },
      empty:data => !data.covered
        ? '当前没有获准的图谱资料。展开「管理图谱展示授权」授权一批资料后再查看。'
        : query
          ? '没有符合条件的图谱资料。试试清空搜索或点击「返回全部图谱」。'
          : '当前没有可见的图谱资料。可换个实体再试，或在「管理图谱展示授权」中检查授权范围。',
      loaded:data => {
        revision = data.revision;
        if (revision) accessTimer = setTimeout(checkAccess, accessInterval);
        if (!data.covered) return;
        count.textContent = `共 ${data.total} 项可见资料（当前查询）`;
        for (const item of data.items) {
          sources.add(item.source);
          if (item.kind === 'relation') {
            sources.add(item.subject.source);
            sources.add(item.object.source);
          }
        }
        canvas.draw(data.items);
      },
      render:item => {
        const row = record({text:label(item), meta:item.kind === 'relation' ? '关系' : '实体'}, () => open(item.source));
        const accessibleLabel = `${label(item)} · 来源：${sourceId(item.source)}`;
        row.lastChild.setAttribute('aria-label', accessibleLabel);
        row.lastChild.setAttribute('title', accessibleLabel);
        return row;
      },
    });
    panel.append(grants.element, form, count, detailStatus, el('div', {class:'memory-graph-layout'}, canvas.element,
      el('div', {class:'stack'}, el('h3', {text:'当前页图谱列表'}), list, pager.navigation)));
    form.addEventListener('submit', event => {
      event.preventDefault();
      query = form.elements.q.value;
      return pager.reset();
    });
    function suspend() {
      if (!active || suspended) return;
      suspended = true;
      grants.close();
      pager.hide();
      list.replaceChildren();
    }
    function resume() {
      if (!active || !suspended || document.hidden) return;
      suspended = false;
      if (!savingGrant) return pager.show();
    }
    document.addEventListener('visibilitychange', () => document.hidden ? suspend() : resume());
    window.addEventListener('pagehide', suspend);
    window.addEventListener('pageshow', resume);
    window.addEventListener('focus', checkAccess);
    return {
      show() {
        active = true;
        suspended = Boolean(document.hidden);
        if (!suspended && !savingGrant) return pager.show();
      },
      hide() {
        active = suspended = false;
        grants.close();
        pager.hide();
        list.replaceChildren();
        applyBrowse(null);
      },
      search(text) {
        form.elements.q.value = text;
        query = text;
        return pager.reset();
      },
    };
  }
  const graph = graphView($('memory-panel-graph'));
  function prepareMemory(tab = memoryTab) {
    activeMemory?.hide();
    activeMemory = null;
    memoryTab = tab;
    activate('data-memory-tab', tab);
    switchPanel({query:'memory-panel-query', images:'memory-panel-images', flow:'memory-panel-flow',
      operations:'memory-panel-operations', transfer:'memory-panel-transfer', inspection:'memory-panel-inspection'}, tab);
    if (tab === 'query') {
      activate('data-memory-query', queryKind);
      switchPanel({text:'memory-query-text', profiles:'memory-query-profiles', graph:'memory-panel-graph'}, queryKind);
      activeMemory = {text:memory, profiles, graph}[queryKind];
    } else if (tab === 'images') activeMemory = pictures;
    else if (tab === 'flow') activeMemory = window.getMemoryFlow?.();
    else if (tab === 'operations') activeMemory = operations;
    else if (tab === 'transfer') return () => window.loadMemoryTransfer?.();
    const view = activeMemory;
    return () => view?.show();
  }
  const showMemoryTab = tab => prepareMemory(tab)();
  function bindTabs(attribute, select) {
    for (const button of document.querySelectorAll('[' + attribute + ']')) button.addEventListener('click', () => {
      if (button.getAttribute('aria-disabled') !== 'true') select(button);
    });
  }
  bindTabs('data-memory-tab', button => showMemoryTab(button.dataset.memoryTab));
  function showQuery(kind) {
    queryKind = kind;
    return showMemoryTab('query');
  }
  function jumpToQuery(kind, form) {
    showQuery(kind);
    form.elements.q.focus();
  }
  bindTabs('data-memory-query', button => showQuery(button.dataset.memoryQuery));
  $('memory-open-graph').addEventListener('click', () => showQuery('graph'));
  $('memory-inspect-text').addEventListener('click', () => jumpToQuery('text', $('memory-search')));
  $('memory-inspect-profiles').addEventListener('click', () => jumpToQuery('profiles', profileForm));
  $('memory-removal-query').addEventListener('click', () => jumpToQuery('text', $('memory-search')));
  // 学习页内联工作台：主页面创建 Shadow DOM，CSS 以 <style> 注入，JS 按需加载。
  const workbench = {attempt:0, loading:null, mounted:false};

  function applyLearningWorkbenchTheme(theme) {
    const host = $('learning-workbench-host');
    if (!host) return;
    const root = host.shadowRoot?.getElementById('root');
    for (const target of [host, root]) {
      if (!target) continue;
      target.classList.toggle('dark', theme === 'dark');
      target.dataset.dashboardStyle = 'aureus';
    }
  }
  window.__aureusWorkbenchApplyTheme = applyLearningWorkbenchTheme;

  function loadLearningWorkbench() {
    if (workbench.mounted) return Promise.resolve();
    if (workbench.loading) return workbench.loading;
    const host = $('learning-workbench-host');
    const status = $('learning-workbench-status');
    const errorBox = $('learning-workbench-error');
    status.classList.remove('hidden');
    errorBox.classList.add('hidden');
    workbench.loading = (async () => {
      const response = await fetch('/learning-workbench/workbench.css', {cache:'no-store'});
      if (!response.ok) throw new Error('样式加载失败（' + response.status + '）。');
      // <style> 里不能带 @charset，Vite 为中文样式加在开头的这一行先去掉。
      const css = (await response.text()).replace(/^@charset\s+"[^"]*";\s*/, '');
      const shadow = host.shadowRoot ?? host.attachShadow({mode:'open'});
      shadow.replaceChildren();
      const style = document.createElement('style');
      style.textContent = css;
      const root = document.createElement('div');
      root.id = 'root';
      shadow.append(style, root);
      window.__aureusWorkbenchGlobalScope = true;
      window.__aureusWorkbenchHost = root;
      applyLearningWorkbenchTheme(document.documentElement.dataset.theme);
      workbench.attempt += 1;
      await import('/learning-workbench/workbench.js' + (workbench.attempt > 1 ? '?retry=' + workbench.attempt : ''));
      workbench.mounted = true;
      status.classList.add('hidden');
    })().catch(error => {
      workbench.mounted = false;
      host.shadowRoot?.replaceChildren();
      status.classList.add('hidden');
      errorBox.classList.remove('hidden');
      $('learning-workbench-error-text').textContent =
        '学习工作台加载失败：' + (error instanceof Error ? error.message : String(error));
      console.error(error);
    }).finally(() => {
      workbench.loading = null;
    });
    return workbench.loading;
  }
  $('learning-workbench-retry').addEventListener('click', loadLearningWorkbench);
  PAGES.learning = {load:loadLearningWorkbench};
  PAGES.memory = {prepare:prepareMemory, leave:() => activeMemory?.hide()};
  return showMemoryTab;
}
