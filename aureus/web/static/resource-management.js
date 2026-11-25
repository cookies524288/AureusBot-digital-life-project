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
  const learningLabels = {jargon:'黑话', expression:'表达方式'};
  const reviewLabels = {pending:'待审', approved:'通过', rejected:'拒绝'};
  const jargonLabels = {candidate:'候选', confirmed:'已确认', non_jargon:'非黑话'};
  const reviewEndpoint = '/api/resources/learning/reviews';
  const scopeEndpoint = '/api/resources/learning/scope';
  let learningKind = 'expression', expressionView = 'list', memoryTab = 'query', queryKind = 'text';
  let activeLearning, activeMemory;
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
    evidence:'证据', name:'名称', id:'编号', kind:'类别', status:'状态', profile:'人物画像'};
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
  function detail(title, ...content) {
    openEditor(title, el('div', {class:'stack resource-detail'}, ...content));
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
  function editLearning(kind, refresh, item) {
    const title = learningLabels[kind];
    const text = textControl(title, item?.text ?? '', 80, true);
    const description = textControl(kind === 'jargon' ? '释义' : '使用场景', item?.detail ?? '', 160, kind !== 'jargon');
    const example = textControl('例句', item?.example ?? '', 2000);
    const save = el('button', {class:'btn primary small', text:'保存'});
    const form = el('form', {class:'stack'}, text.field, description.field, example.field, save);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (await change('/api/resources/learning', item ? 'PATCH' : 'POST', {
        kind, entry_key:item?.entry_key,
        content:{text:text.input.value, detail:description.input.value, example:example.input.value}
      }, refresh, save) && form.isConnected) $('editor-dialog').close();
    });
    openEditor((item ? '编辑' : '新增') + title, form);
  }
  function classifyJargon(keys, refresh) {
    if (!keys.length) return;
    let busy = false;
    const buttons = [['confirmed', '确认为黑话'], ['non_jargon', '判为非黑话']].map(([status, label]) =>
      action(label, async () => {
        if (busy) return;
        busy = true;
        for (const button of buttons) button.disabled = true;
        try {
          const result = await api('/api/resources/learning/jargon/classification', 'PATCH',
            {entry_keys:keys, status});
          if (!result.ok) { fail(result); return; }
          toast(`已更新 ${result.data.changed} 条黑话的定性。`);
          if (buttons[0].isConnected) $('editor-dialog').close();
          await refresh();
        } finally {
          busy = false;
          for (const button of buttons) button.disabled = false;
        }
      }));
    detail(`定性 ${keys.length} 条黑话资料`,
      actions('定性结论', ...buttons, action('取消', () => $('editor-dialog').close())));
  }
  function importLearning(kind, refresh) {
    const endpoint = '/api/resources/learning/package/' + kind;
    const input = el('input', {type:'file', accept:'.json,application/json', required:true});
    const info = el('p', {class:'muted', role:'status'});
    const submit = el('button', {type:'submit', class:'btn primary small', text:'预览'});
    const controls = el('fieldset', {class:'stack'}, el('label', {class:'stack'}, 'JSON 语言包', input), info, submit);
    const form = el('form', {class:'stack'}, controls);
    let digest;
    function reset() {
      digest = null;
      submit.textContent = '预览';
      info.textContent = '预览不会写入。未映射会话的条目只保存，不参与召回。';
    }
    input.addEventListener('change', reset);
    reset();
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (controls.disabled) return;
      controls.disabled = true;
      try {
        const response = await api(endpoint + (digest ? '?confirmed=' + digest : ''), 'POST', input.files[0]);
        if (!form.isConnected) return;
        if (!response.ok) { fail(response); return; }
        if (!digest) {
          digest = response.data.digest;
          info.textContent = `可导入保存 ${response.data.can_save} 条，可参与召回 ${response.data.can_recall} 条。重复记录将跳过。`;
          submit.textContent = '确认导入';
          return;
        }
        $('editor-dialog').close();
        toast(`已保存 ${response.data.added} 条，跳过 ${response.data.skipped} 条。`);
        await refresh();
      } finally { controls.disabled = false; }
    });
    openEditor('导入' + learningLabels[kind], form);
  }
  // 批量移除、恢复前先确认，并写明会影响多少条（批次 3）。
  function confirmBatch(title, count, restoring, proceed) {
    const label = restoring ? '恢复' : '移除';
    const confirm = action('确认' + label, () => { $('editor-dialog').close(); proceed(); });
    openEditor(label + title, el('div', {class:'stack'},
      el('p', {text:`仅${label}本页选中的 ${count} 条记录，不影响其他记录。`}),
      el('p', {text:restoring ? '恢复后这些记录重新参与召回。'
        : '移除后这些记录不再参与召回，可在「显示已移除」中恢复；原件保留。'}),
      el('div', {class:'row'}, action('取消', () => $('editor-dialog').close()), confirm)));
  }
  function batchControls(keys, endpoint, payload, refresh, selectionChanged, confirm) {
    const selected = new Map();
    let busy = false;
    const remove = action('移除所选', event => update(event.currentTarget, false));
    const restore = action('恢复所选', event => update(event.currentTarget, true));
    function sync() {
      remove.disabled = busy || ![...selected.values()].includes(false);
      restore.disabled = busy || ![...selected.values()].includes(true);
      selectionChanged?.(selected.size);
    }
    async function update(button, restoring) {
      const entries = [...selected].filter(([, removed]) => removed === restoring).map(([key]) => key);
      if (busy || !entries.length) return;
      const apply = async () => {
        busy = true;
        sync();
        try { await change(endpoint, 'PATCH', payload(entries, !restoring), refresh, button); }
        finally { busy = false; sync(); }
      };
      if (confirm) confirm(entries.length, restoring, apply);
      else await apply();
    }
    function checkbox(item) {
      const box = el('input', {type:'checkbox', 'aria-label':'选择 ' + (item.text || '未填写描述')});
      box.addEventListener('change', () => {
        if (box.checked) selected.set(keys(item), !!item.is_removed);
        else selected.delete(keys(item));
        sync();
      });
      return box;
    }
    sync();
    return {remove, restore, checkbox, keys:() => [...selected.keys()], clear() { selected.clear(); sync(); }};
  }
  async function editClusterGrant(item) {
    const endpoint = '/api/resources/learning/clusters/grants';
    const body = el('div', {class:'stack'}, el('p', {text:'正在核对这条表达…'}));
    openEditor('表达簇展示授权', body);
    const response = await api(endpoint + '?' + new URLSearchParams({entry_key:item.entry_key}));
    const isCurrent = () => body.isConnected && $('editor-dialog').open;
    if (!isCurrent()) return;
    if (!response.ok) { body.replaceChildren(el('p', {text:'暂时无法读取授权。'})); return fail(response); }
    const {enabled, granted, version} = response.data;
    if (!enabled || (!version && !granted)) {
      body.replaceChildren(el('p', {text:enabled ? '该表达当前不可授权。' : '当前未接入表达簇展示授权。'}));
      return;
    }
    const password = el('input', {type:'password', required:true, autocomplete:'current-password'});
    const grant = el('button', {type:'submit', class:'btn small primary', text:'授权这条表达'});
    const revoke = action('撤回这条授权', () => { if (form.reportValidity()) save(null); });
    const canGrant = version && version === item.cluster_display_version;
    grant.disabled = !canGrant;
    revoke.disabled = !granted;
    const controls = el('fieldset', {class:'stack'},
      el('p', {text:item.text}),
      el('p', {class:'muted', text:'只允许已登录者在表达簇查看该条表达与情景，不改变召回或语言包范围。'}),
      el('p', {text:granted ? '有已保存的展示授权' : '尚未授权展示'}),
      version && !canGrant && el('p', {text:'条目已变化；刷新表达列表后再授权。'}),
      el('label', {class:'stack'}, '确认当前网页密码', password), el('div', {class:'row'}, grant, revoke));
    const form = el('form', {class:'stack'}, controls);
    form.addEventListener('submit', event => { event.preventDefault(); if (canGrant) save(version); });
    body.replaceChildren(form);

    async function save(target) {
      if (controls.disabled) return;
      const secret = password.value;
      password.value = '';
      controls.disabled = true;
      try {
        const result = await api(endpoint, 'PUT', {entry_key:item.entry_key, version:target, password:secret});
        if (!isCurrent()) return;
        if (!result.ok) return fail(result);
        $('editor-dialog').close();
        toast(target ? '已授权该表达在簇中展示。' : '已撤回该表达的簇展示授权。');
      } finally { controls.disabled = false; }
    }
  }
  async function editLearningScope(kind, item, refresh) {
    const form = el('form', {class:'stack'}, el('p', {text:'读取已知会话…'}));
    openEditor('设置生效会话', form);
    const response = await api(scopeEndpoint);
    if (!form.isConnected) return;
    if (!response.ok) { fail(response); return; }
    const choices = response.data.items.map(target => {
      const input = el('input', {type:'checkbox', value:target.key});
      input.checked = item.scope_keys.includes(target.key);
      return {input, label:el('label', {}, input, ' ' + target.label)};
    });
    const summary = el('div', {class:'stack', 'aria-live':'polite'});
    const submit = el('button', {type:'submit', class:'btn primary small', text:'预览范围'});
    const controls = el('fieldset', {class:'stack'},
      el('p', {text:'仅选中的会话可使用该记录；多选即共享。清空并确认可撤销绑定。'}),
      !choices.length && el('p', {text:'收件箱暂无已知群聊或普通私聊。'}),
      choices.map(choice => choice.label), summary,
      el('div', {class:'row'}, submit, action('取消', () => $('editor-dialog').close())));
    form.replaceChildren(controls);
    let confirmedKeys = null;
    form.addEventListener('change', () => {
      confirmedKeys = null;
      summary.replaceChildren();
      submit.textContent = '预览范围';
    });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (controls.disabled) return;
      const keys = confirmedKeys ?? choices.filter(choice => choice.input.checked).map(choice => choice.input.value);
      controls.disabled = true;
      try {
        const result = await api(scopeEndpoint, 'POST', {
          kind, entry_key:item.entry_key, conversation_keys:keys, confirm:confirmedKeys !== null,
        });
        if (!form.isConnected) return;
        if (!result.ok) { fail(result); return; }
        if (!result.data.confirmed) {
          confirmedKeys = keys;
          summary.replaceChildren(field('当前授权', result.data.before.join('；') || '无'),
            field('确认后授权', result.data.after.join('；') || '无，停止参与召回'),
            el('p', {text:'这里只设置授权范围，不改变停用或移除状态。'}));
          submit.textContent = '确认范围';
          return;
        }
        $('editor-dialog').close();
        toast('已保存生效会话');
        await refresh();
      } finally { controls.disabled = false; }
    });
  }
  function learningView(kind) {
    const panel = $(kind === 'expression' ? 'learning-expression-list' : 'learning-panel-jargon');
    const disabled = el('label', {}, el('input', {name:'disabled', type:'checkbox'}), ' 显示停用和待映射');
    const removed = el('label', {}, el('input', {name:'removed', type:'checkbox'}), ' 显示已移除');
    const jargonFilter = kind === 'jargon' ? el('select', {name:'jargon_status', 'aria-label':'黑话定性'},
      ...Object.entries({'':'全部定性', ...jargonLabels}).map(([value, text]) => el('option', {value, text}))) : null;
    const shared = kind === 'jargon' ? el('input', {name:'shared', type:'checkbox'}) : null;
    const scopeDescription = '来自会话按已确认的学习来源筛选；授权会话只筛选明确授权的语言包。筛选不改变实际召回范围。';
    const scopeHint = el('p', {class:'muted', id:'learning-scope-hint-' + kind,
      'aria-live':'polite', text:scopeDescription});
    const conversationFilter = name => el('select', {name, 'aria-describedby':scopeHint.id, disabled:true});
    const origin = conversationFilter('origin_conversation_key');
    const authorized = conversationFilter('authorized_conversation_key');
    const originLabels = new Map();
    const toolbar = searchForm('搜索' + learningLabels[kind], '搜索词、释义或例句', disabled, removed,
      jargonFilter && el('label', {}, '黑话定性 ', jargonFilter), shared && el('label', {}, shared, ' 仅共享范围'),
      el('label', {}, '来自会话 ', origin), el('label', {}, '授权会话 ', authorized));
    const add = action('添加' + learningLabels[kind], () => editLearning(kind, pager.read));
    add.classList.add('primary');
    const upload = action('导入', () => importLearning(kind, pager.read));
    const download = action('导出', () => {
      const link = el('a', {href:'/api/resources/learning/package/' + kind, download:''});
      document.body.append(link); link.click(); link.remove();
    });
    const selectedCount = el('span', {class:'muted resource-selection-count', role:'status', 'aria-live':'polite'});
    const classifyBatch = jargonFilter && action('定性所选', () => classifyJargon(batch.keys(), pager.read));
    const batch = batchControls(item => item.entry_key, '/api/resources/learning/batch',
      (entry_keys, removed) => ({kind, entry_keys, removed}), () => pager.read(),
      count => {
        selectedCount.textContent = count ? `已选 ${count} 条` : '';
        if (classifyBatch) classifyBatch.disabled = !count || removed.firstChild.checked;
      }, (count, restoring, proceed) => confirmBatch('所选资料', count, restoring, proceed));
    const tools = [upload, download, add];
    for (const tool of tools) tool.disabled = true;
    panel.append(toolbar, scopeHint, actions(kind === 'expression' ? '表达操作' : '黑话操作', ...tools),
      actions('所选资料', selectedCount, classifyBatch, batch.remove, batch.restore));
    function render(item) {
      const state = item.is_removed ? '已移除' : item.pending_scope ? '待映射' : item.is_enabled ? '' : '已停用';
      const status = [state || '已启用', item.jargon_status ? jargonLabels[item.jargon_status] : '']
        .filter(Boolean).join(' · ');
      const heading = item.text + (state ? ' · ' + state : '');
      const open = () => detail(heading, field(kind === 'jargon' ? '释义' : '使用场景', item.detail),
        field('例句', item.example), field('发言者', item.speakers.join('、')),
        field('来自会话', originLabels.get(item.learning_source) || (item.learning_source ? '已记录的来源会话' : '未确认')),
        item.package_source != null && field('包内所载会话名称', item.package_source || '未提供名称'),
        item.jargon_status && field('定性', jargonLabels[item.jargon_status]),
        item.jargon_status && !item.detail && field('释义状态', '尚未填写'),
        item.original && field('原件', item.original.filter(Boolean).join('\n')),
        el('div', {class:'row'}, action('编辑', () => editLearning(kind, pager.read, item)),
          item.jargon_status && !item.is_removed && action('定性', () => classifyJargon([item.entry_key], pager.read)),
          kind === 'expression' && action('表达簇展示授权', () => editClusterGrant(item)),
          item.package_source != null && action('设置生效会话', () => editLearningScope(kind, item, pager.read)),
          !item.pending_scope && !item.is_removed && action(item.is_enabled ? '停用' : '恢复', async event => {
            const button = event.currentTarget;
            if (await change('/api/resources/learning', 'PATCH', {kind, entry_key:item.entry_key,
              is_enabled:!item.is_enabled}, pager.read, button) && button.isConnected) $('editor-dialog').close();
          })));
      return record({text:item.text || '未填写内容', meta:status}, open, batch.checkbox(item));
    }
    const pager = searchable({panel, id:'learning-' + kind, render,
      empty:() => {
        const filtered = toolbar.elements.q.value.trim() || disabled.firstChild.checked || removed.firstChild.checked
          || origin.value || authorized.value || jargonFilter?.value || shared?.checked;
        return filtered
          ? '没有符合条件的资料，试试清空搜索或筛选条件。'
          : `还没有${learningLabels[kind]}。点击「添加${learningLabels[kind]}」录入一条，或点「导入」从语言包导入。`;
      },
      prepare:() => {
        batch.clear(); for (const tool of tools) tool.disabled = true;
      }, loaded:data => { for (const tool of tools) tool.disabled = !data.enabled; }}, toolbar, '/api/resources/learning',
    fields => ({kind, q:fields.q.value, recall:fields.disabled.checked || fields.removed.checked ? '' : 'enabled',
      removed:fields.removed.checked, origin_conversation_key:origin.value, authorized_conversation_key:authorized.value,
      ...(fields.jargon_status?.value ? {jargon_status:fields.jargon_status.value} : {}),
      ...(shared?.checked ? {shared:true} : {})}));
    let scopeGeneration = 0;
    function loadScopes() {
      const generation = ++scopeGeneration;
      const filters = [[origin, '/api/resources/learning/origins', '来源'], [authorized, scopeEndpoint, '授权']];
      const hadScope = origin.value || authorized.value;
      const statuses = new Map(filters.map(([control, , label]) => [control, `正在确认${label}会话。`]));
      const all = () => el('option', {value:'', text:'全部资料'});
      const updateHint = () => { scopeHint.textContent = [...statuses.values()].join('') + scopeDescription; };
      originLabels.clear();
      for (const [control] of filters) {
        control.disabled = true;
        control.replaceChildren(all());
      }
      updateHint();
      if (hadScope) pager.resetApplied({origin_conversation_key:'', authorized_conversation_key:''});
      async function load(control, endpoint, label) {
        const response = await api(endpoint);
        if (generation !== scopeGeneration) return;
        if (!response.ok) statuses.set(control, `${label}会话列表读取失败，仍可浏览全部资料。`);
        else {
          control.replaceChildren(all(), ...response.data.items.map(target => {
            if (control === origin) originLabels.set(target.key, target.label);
            return el('option', {value:target.key, text:target.label});
          }));
          control.disabled = !response.data.items.length;
          statuses.set(control, control.disabled ? `暂无已知${label}会话。` : '');
        }
        updateHint();
      }
      for (const [control, endpoint, label] of filters) load(control, endpoint, label);
    }
    return {
      show() { loadScopes(); return pager.show(); },
      hide() { scopeGeneration++; pager.hide(); },
    };
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
  const emojiRegistrationHeader = 'X-Emoji-Registration-Description';
  function emojiRegistrationHeaders(description) {
    return description ? {[emojiRegistrationHeader]:encodeURIComponent(description)} : {};
  }
  function emojiUploadRegistration(label) {
    const register = el('input', {type:'checkbox'});
    const description = textControl(label, '', 500, true);
    const details = el('div', {hidden:true}, description.field);
    description.input.disabled = true;
    register.addEventListener('change', () => {
      details.hidden = description.input.disabled = !register.checked;
    });
    return {
      fields:[el('label', {}, register, ' 上传后直接登记'), details],
      headers() {
        if (!register.checked) return {};
        const text = description.input.value.trim();
        if (text) return emojiRegistrationHeaders(text);
        toast('请填写表情描述后再登记。', 'error');
        description.input.focus();
        return null;
      },
    };
  }
  function emojiUploadResult(response, headers) {
    if (!response.ok) return errorText(response);
    if (!response.data.created) return '已存在，保留原有记录';
    if (!headers[emojiRegistrationHeader]) return '已上传';
    return response.data.registered === false ? '已上传，登记容量已满' : '已上传并登记';
  }
  function emojiForm(refresh, item) {
    const submit = el('button', {type:'submit', class:'btn primary small', text:item ? '保存' : '上传', disabled:!item});
    const file = el('input', {type:'file', accept:'image/png,image/jpeg,image/gif,image/webp', required:true});
    const description = textControl('表情描述', item?.text ?? '', 500, item?.is_registered);
    const tags = textControl('标签（每行一个）', item?.tags?.join('\n') ?? '', 819);
    const registration = emojiUploadRegistration('表情描述');
    const controls = el('fieldset', {class:'stack'}, item ? [description.field, tags.field] : [
      el('label', {class:'stack'}, '图片文件', file),
      el('p', {class:'muted', text:'支持 PNG、JPEG、GIF 和 WebP，最多 5 MB。未勾选时可稍后登记。'}),
      ...registration.fields,
    ], submit);
    const form = el('form', {class:'stack'}, controls);
    file.addEventListener('change', () => { submit.disabled = !file.files.length; });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (controls.disabled) return;
      const values = tags.input.value.split('\n').map(tag => tag.trim()).filter(Boolean);
      if (values.length > 20 || values.some(tag => [...tag].length > 40)) {
        toast('最多填写 20 个标签，每个不超过 40 字。', 'error');
        return;
      }
      const headers = item ? {} : registration.headers();
      if (!headers) return;
      const endpoint = '/api/resources/emoji' + (item ? '/' + encodeURIComponent(item.digest) : '');
      controls.disabled = true;
      try {
        const response = await api(endpoint, item ? 'PATCH' : 'POST', item ? {
          description:description.input.value, tags:values,
        } : file.files[0], headers);
        if (!response.ok) { fail(response); return; }
        if (form.isConnected) $('editor-dialog').close();
        toast(item ? '已保存' : emojiUploadResult(response, headers));
        await refresh();
      } finally { controls.disabled = false; }
    });
    openEditor(item ? '编辑表情' : '上传表情', form);
  }
  function emojiBatchQueue(register) {
    const pageSize = 3;
    const items = el('div', {class:'stack emoji-upload-items'});
    const controls = createResourcePageControls({el, label:'批量上传分页', select:show});
    let entries = [], page = 0;

    function show(target = page) {
      page = target;
      const pages = Math.ceil(entries.length / pageSize);
      controls.navigation.hidden = !pages;
      controls.sync(page, pages);
      const offset = page * pageSize;
      items.replaceChildren(...entries.slice(offset, offset + pageSize).map((entry, index) => {
        const name = `${offset + index + 1}. ${entry.image.name}`;
        if (!register.checked) return el('p', {text:name + (entry.result ? '：' + entry.result : '')});
        const description = textControl(name + (entry.result ? '：' + entry.result : ''), entry.description, 500);
        description.input.addEventListener('input', () => { entry.description = description.input.value; });
        return description.field;
      }));
    }
    register.addEventListener('change', () => show());
    return {
      fields:[items, controls.navigation],
      select(files) {
        entries = Array.from(files, image => ({image, description:'', result:''}));
        show(0);
      },
      prepare(shared) {
        const batch = entries.map(entry => ({entry, text:register.checked ? entry.description.trim() || shared : ''}));
        const missing = register.checked ? batch.findIndex(({text}) => !text) : -1;
        if (missing === -1) return batch;
        show(Math.floor(missing / pageSize));
        items.querySelectorAll('textarea')[missing % pageSize].focus();
        toast('请为这张图片填写描述，或填写共享描述后再登记。', 'error');
        return null;
      },
      show,
    };
  }
  function emojiBatchForm(refresh) {
    const file = el('input', {type:'file', accept:'image/png,image/jpeg,image/gif,image/webp', multiple:true, required:true});
    const summary = el('p', {class:'muted', 'aria-live':'polite', text:'尚未选择图片'});
    const results = el('p', {class:'muted', role:'status'});
    const submit = el('button', {type:'submit', class:'btn primary small', text:'上传所选图片', disabled:true});
    const register = el('input', {type:'checkbox'});
    const shared = textControl('共享描述（逐图描述留空时使用）', '', 500);
    const sharedField = el('div', {hidden:true}, shared.field);
    shared.input.disabled = true;
    register.addEventListener('change', () => { sharedField.hidden = shared.input.disabled = !register.checked; });
    const queue = emojiBatchQueue(register);
    const controls = el('fieldset', {class:'stack emoji-batch-upload'},
      el('label', {class:'stack'}, '图片文件', file), summary,
      el('label', {}, register, ' 上传后直接登记'), sharedField, ...queue.fields, results, submit);
    const form = el('form', {class:'stack'}, controls);
    file.addEventListener('change', () => {
      summary.textContent = `已选择 ${file.files.length} 张图片。每张最多 5 MB。`;
      queue.select(file.files);
      results.textContent = '';
      submit.disabled = !file.files.length;
    });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (controls.disabled || !file.files.length) return;
      const batch = queue.prepare(shared.input.value.trim());
      if (!batch) return;
      controls.disabled = true;
      let succeeded = 0, duplicates = 0, failed = 0;
      for (const {entry} of batch) entry.result = '';
      queue.show();
      results.textContent = '上传中…';
      try {
        for (const {entry, text} of batch) {
          const headers = emojiRegistrationHeaders(text);
          const response = await api('/api/resources/emoji', 'POST', entry.image, headers);
          entry.result = emojiUploadResult(response, headers);
          if (!response.ok) failed++;
          else if (response.data.created) succeeded++;
          else duplicates++;
          queue.show();
        }
        results.textContent = `成功 ${succeeded} 张 · 重复 ${duplicates} 张 · 失败 ${failed} 张`;
        await refresh();
      } finally { controls.disabled = false; submit.disabled = !file.files.length; }
    });
    openEditor('批量上传表情', form);
  }
  async function emojiIntegrityPreview() {
    const result = el('div', {class:'stack', 'aria-live':'polite'}, el('p', {text:'正在检查原图…'}));
    detail('表情完整性预览', result,
      el('p', {class:'muted', text:'预览不修改登记、记录或文件。后台完整性检查只维护发送候选。'}));
    const response = await api('/api/resources/emoji/integrity-preview', 'POST');
    if (!response.ok) { result.textContent = errorText(response); return; }
    const {checked, unavailable, examples} = response.data;
    result.replaceChildren(el('p', {text:`检查 ${checked} 条活跃登记记录 · 原图不可用 ${unavailable} 条`}));
    if (examples.length) result.append(el('p', {class:'muted', text:'问题示例（最多 30 条）'}),
      el('ul', {}, ...examples.map(description => el('li', {text:description || '未填写描述'}))));
  }
  async function emojiReplacement(item, refresh, offset = 0) {
    const endpoint = '/api/resources/emoji/replacement/' + encodeURIComponent(item.digest);
    const status = el('p', {role:'status', text:'正在检查可替换的旧登记…'});
    const result = el('div', {class:'stack'});
    const controls = el('fieldset', {class:'stack'}, status, result);
    detail('替换表情登记', el('p', {text:'只撤销旧表情的登记，不删除原图或改动审批。'}),
      controls, action('取消', () => $('editor-dialog').close()));
    const visible = () => controls.isConnected && $('editor-dialog').open;
    function choose(choice) {
      result.replaceChildren(field('新登记', choice.incoming_description),
        field('取消旧登记', choice.outgoing_description || '未填写描述'),
        action('确认替换', async () => {
          if (controls.disabled) return;
          controls.disabled = true;
          try {
            const {incoming, outgoing, incoming_version, outgoing_version, expires, token} = choice;
            const saved = await api('/api/resources/emoji/replacement/confirm', 'POST',
              {incoming, outgoing, incoming_version, outgoing_version, expires, token});
            if (!saved.ok) { status.textContent = errorText(saved); return; }
            if (visible()) $('editor-dialog').close();
            toast('已替换登记，原图仍保留');
            await refresh();
          } finally { controls.disabled = false; }
        }), action('返回可替换列表', () => emojiReplacement(item, refresh, offset)));
    }
    const response = await api(endpoint + '?' + new URLSearchParams({offset}));
    if (!visible()) return;
    if (!response.ok) { status.textContent = errorText(response); return; }
    const {items, has_next, page_size, suggestion_enabled} = response.data;
    status.textContent = items.length ? '选择要撤销的旧登记。' : '没有可撤销的旧登记。';
    const previous = action('上一页', () => emojiReplacement(item, refresh, Math.max(0, offset - page_size)));
    const next = action('下一页', () => emojiReplacement(item, refresh, offset + page_size));
    previous.disabled = !offset;
    next.disabled = !has_next;
    const suggest = suggestion_enabled && items.length && action('请求模型建议（可能收费）', async () => {
      if (controls.disabled) return;
      controls.disabled = true;
      try {
        const suggestion = await api(endpoint + '/suggest', 'POST');
        if (!visible()) return;
        if (suggestion.ok && suggestion.data.item) choose(suggestion.data.item);
        else status.textContent = suggestion.ok ? '没有可用建议，登记未变更。' : errorText(suggestion);
      } finally { controls.disabled = false; }
    });
    result.replaceChildren(...items.map(choice => action(choice.outgoing_description || '未填写描述', () => choose(choice))),
      actions('替换候选分页', previous, next, suggest || null));
  }
  function emojiActions(item, refresh) {
    const actions = el('div', {class:'row'});
    const edit = action('编辑', () => emojiForm(refresh, item));
    function update(label, payload) {
      return action(label, async event => {
        const button = event.currentTarget;
        for (const control of actions.children) control.disabled = true;
        try {
          if (await change('/api/resources/emoji/' + encodeURIComponent(item.digest), 'PATCH', payload,
            refresh, button) && actions.isConnected) $('editor-dialog').close();
        } finally { for (const control of actions.children) control.disabled = false; }
      });
    }
    actions.append(edit);
    if (!item.is_banned) actions.append(update(item.is_registered ? '取消登记' : '登记', {
      is_registered:!item.is_registered,
    }));
    if (!item.is_registered && !item.is_banned && item.review !== 'rejected' && item.text?.trim()) {
      actions.append(action('替换旧登记', () => emojiReplacement(item, refresh)));
    }
    actions.append(update(item.is_banned ? '恢复' : '禁用', {is_banned:!item.is_banned}));
    return actions;
  }
  const emojiReviewLabels = {unreviewed:'未人工审核', approved:'已通过', rejected:'已拒绝'};
  const emojiReviewBadges = {unreviewed:'未审', approved:'通过', rejected:'拒绝'};
  function emojiReviewForm(item, refresh, pending) {
    if (pending.has(item.digest)) return;
    const title = item.text || '未填写描述';
    const review = item.review ?? 'unreviewed';
    const message = el('p', {class:'muted', role:'status'});
    async function submit(verdict) {
      if (pending.has(item.digest)) return;
      pending.add(item.digest);
      controls.disabled = true;
      message.textContent = '正在保存审批…';
      try {
        const response = await api('/api/resources/emoji/' + encodeURIComponent(item.digest) + '/review',
          'PATCH', {verdict});
        if (!response.ok || response.data?.review !== verdict) {
          message.textContent = response.ok ? '无法确认审批结果，请刷新后重试。' : errorText(response);
          if (!controls.isConnected || !$('editor-dialog').open) toast(message.textContent, 'error');
          return;
        }
        toast('表情审批：' + emojiReviewLabels[verdict]);
        if (controls.isConnected && $('editor-dialog').open) $('editor-dialog').close();
        await refresh();
      } finally { pending.delete(item.digest); controls.disabled = false; }
    }
    const controls = el('fieldset', {class:'row'},
      el('button', {type:'button', class:'btn primary small', text:'通过审批', disabled:review === 'approved',
        onclick:() => submit('approved')}),
      el('button', {type:'button', class:'btn danger small', text:'拒绝审批', disabled:review === 'rejected',
        onclick:() => submit('rejected')}));
    detail('表情审批', fullImage('/api/resources/media/' + encodeURIComponent(item.digest) + '?kind=emoji'),
      field('描述', title), field('当前结论', emojiReviewLabels[review]),
      el('p', {class:'muted', text:'拒绝后不再选用。通过不改变登记或禁用状态；仍须已登记、未禁用且原图可用才能发送。'}),
      controls, message);
  }
  function renderMedia(item, refresh, batch, openReview) {
    const emoji = item.media_kind === 'emoji';
    const status = emoji ? (item.is_removed ? '已移除' : item.is_banned ? '已禁用' : item.is_registered ? '已登记' : '未登记') : item.status;
    const title = item.text || '未填写描述';
    const review = item.review ?? 'unreviewed';
    const url = item.digest ? '/api/resources/media/' + encodeURIComponent(item.digest) + (emoji ? '?kind=emoji' : '') : null;
    const tile = mediaTile(emoji ? status + ' · ' + title : title, url, '原图不可用', () => detail(emoji ? '表情详情' : '图片详情', fullImage(url),
      field('描述', title), field('状态', term(status)), field('来源', sourceText(item.source)),
      emoji && field('审批', emojiReviewLabels[review]),
      emoji && field('格式', item.media_type?.split('/')[1]?.toUpperCase()),
      emoji && field('标签', item.tags?.join('、')),
      emoji && field('收集次数', item.collection_count), emoji && field('使用次数', item.use_count),
      !url && field('原图', '缺少原件'), emoji && url && !item.is_removed && emojiActions(item, refresh)));
    if (!batch || !item.entry_key) return tile;
    const approval = url && !item.is_removed && action(emojiReviewBadges[review], () => openReview(item));
    if (approval) {
      const label = '表情审批 · ' + emojiReviewLabels[review] + ' · ' + title;
      approval.classList.add('emoji-review-control');
      approval.classList.toggle('danger', review === 'rejected');
      approval.setAttribute('aria-label', label);
      approval.title = label;
      tile.classList.add('emoji-reviewable');
      tile.title = title;
    }
    return el('div', {class:'resource-tile-selection'}, tile, batch.checkbox(item), approval || null);
  }
  const emojiMaterialsEndpoint = '/api/resources/emoji/materials';
  function emojiMaterialExport(keys) {
    const license = textControl('选中素材的来源及许可声明', '', 500, true);
    const submit = el('button', {class:'btn primary small', text:'导出所选'});
    const form = el('form', {class:'stack'},
      el('p', {text:`只导出选中的 ${keys.length} 张原图及描述，不包含登记或审批。每份最多 20 张；请确认有分享权。`}),
      license.field, submit, action('取消', () => $('editor-dialog').close()));
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (submit.disabled || !license.input.value.trim()) return;
      submit.disabled = true;
      try {
        const response = await api(emojiMaterialsEndpoint + '/export', 'POST', {
          keys, source_license:license.input.value,
        });
        if (!response.ok) { fail(response); return; }
        if (!form.isConnected || !$('editor-dialog').open) return;
        const url = URL.createObjectURL(new Blob([JSON.stringify(response.data)], {type:'application/json'}));
        const link = el('a', {href:url, download:'aureus-emoji-materials.json'});
        document.body.append(link); link.click(); link.remove();
        URL.revokeObjectURL(url);
        $('editor-dialog').close();
      } finally { submit.disabled = false; }
    });
    openEditor('导出 Aureus 表情素材集', form);
  }
  function emojiMaterialImport(refresh) {
    const input = el('input', {type:'file', accept:'.json,application/json', required:true});
    const register = el('input', {type:'checkbox'});
    const submit = el('button', {class:'btn primary small', text:'预检'});
    const status = el('p', {role:'status', class:'muted'});
    const members = el('ul');
    const controls = el('fieldset', {class:'stack'}, el('label', {class:'stack'}, 'Aureus v1 JSON 素材集', input),
      el('label', {}, register, ' 同时登记有描述且有空位的新素材'), submit);
    const form = el('form', {class:'stack'},
      el('p', {text:'这是 Aureus 扩展格式，不是 MaiBot 专用接口或 A_Memorix 记忆包。重复素材保留原有登记和审批。'}),
      controls, status, members, action('取消', () => $('editor-dialog').close()));
    let digest;
    function reset() {
      digest = null;
      submit.textContent = '预检';
      status.textContent = '最多 20 张、32 MiB；预检不写入。';
      members.replaceChildren();
    }
    input.addEventListener('change', reset);
    register.addEventListener('change', reset);
    reset();
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (controls.disabled) return;
      const confirmed = digest;
      const params = new URLSearchParams({register:register.checked, ...(confirmed ? {confirmed} : {})});
      controls.disabled = true;
      try {
        const response = await api(emojiMaterialsEndpoint + '/import?' + params, 'POST', input.files[0],
          {'Content-Type':'application/json'});
        if (!response.ok) { reset(); status.textContent = errorText(response); return; }
        const data = response.data;
        status.textContent = `${confirmed ? '已完成' : '预检'}：新增 ${data.added} 张 · 登记 ${data.registered} 张 · 重复 ${data.skipped} 张 · 超额 ${data.capacity_blocked} 张 · 缺描述 ${data.missing_description} 张`;
        members.replaceChildren(...data.members.map(item => el('li', {text:
          `${item.description || '未填写描述'} · ${item.bytes} 字节 · ${item.digest} · 来源及许可：${item.source_license}`})));
        if (!confirmed) { digest = data.digest; submit.textContent = '确认导入'; }
        else { digest = null; submit.textContent = '预检'; toast('素材集已处理'); await refresh(); }
      } finally { controls.disabled = false; }
    });
    openEditor('导入 Aureus 表情素材集', form);
  }
  async function emojiThumbnailStatus() {
    const status = el('p', {role:'status', text:'正在读取缩略图缓存状态…'});
    detail('表情缩略图缓存', status);
    const result = await api('/api/resources/emoji/thumbnail-cache');
    if (status.isConnected) status.textContent = result.ok
      ? '本项目不生成持久缩略图缓存，当前 0 条，无需预热或清理。原图和目录状态不受影响。'
      : errorText(result);
  }
  function mediaView(panel, kind) {
    const emoji = kind === 'emoji';
    const select = (label, name, options) => el('label', {}, label + ' ', el('select', {name},
      ...Object.entries(options).map(([value, text]) => el('option', {value, text}))));
    const status = emoji ? select('表情状态', 'status',
      {'':'全部', registered:'已登记', unregistered:'未登记', banned:'已禁用'}) : null;
    const review = emoji ? select('人工审批', 'review', {'':'全部', ...emojiReviewLabels}) : null;
    const format = emoji ? select('表情格式', 'format', {'':'全部', png:'PNG', jpeg:'JPEG', gif:'GIF', webp:'WebP'}) : null;
    const sort = emoji ? select('表情排序', 'sort',
      {recent:'最近收集', most_used:'使用最多', most_collected:'收集最多', last_used:'最近使用'}) : null;
    const label = kind === 'emoji' ? '搜索表情包' : '搜索迁入图片';
    const removed = kind === 'emoji' ? el('label', {}, el('input', {name:'removed', type:'checkbox'}), ' 显示已移除') : null;
    const form = searchForm(label, label, status, review, format, sort, removed);
    const counts = emoji ? el('div', {class:'muted', id:'emoji-library-counts'}) : null;
    const selectedCount = emoji ? el('span', {class:'muted resource-selection-count', role:'status', 'aria-live':'polite'}) : null;
    const exportMaterials = emoji ? action('导出所选素材', () => emojiMaterialExport(batch.keys())) : null;
    const batch = kind === 'emoji' ? batchControls(item => item.entry_key, '/api/resources/emoji/batch',
      (entry_keys, removed) => ({entry_keys, removed}), () => pager.read(), count => {
        selectedCount.textContent = count ? `已选 ${count} 张` : '';
        if (exportMaterials) exportMaterials.disabled = !count || count > 20 || removed.firstChild.checked;
      }, (count, restoring, proceed) => confirmBatch('所选表情', count, restoring, proceed)) : null;
    const upload = kind === 'emoji' ? action('上传表情', () => emojiForm(pager.read)) : null;
    upload?.classList.add('primary');
    const batchUpload = kind === 'emoji' ? action('批量上传', () => emojiBatchForm(pager.read)) : null;
    const importMaterials = emoji ? action('导入素材集', () => emojiMaterialImport(pager.read)) : null;
    const thumbnail = emoji ? action('缩略图缓存', emojiThumbnailStatus) : null;
    let checkingIntegrity = false;
    let emojiEnabled = false;
    const pendingReviews = new Set();
    const openReview = item => emojiReviewForm(item, pager.read, pendingReviews);
    const integrity = emoji ? action('完整性预览', async event => {
      if (checkingIntegrity || !emojiEnabled) return;
      const button = event.currentTarget;
      checkingIntegrity = button.disabled = true;
      try { await emojiIntegrityPreview(); }
      finally { checkingIntegrity = false; button.disabled = !emojiEnabled; }
    }) : null;
    function syncEmojiActions(enabled) {
      emojiEnabled = enabled;
      if (upload) upload.disabled = !enabled;
      if (batchUpload) batchUpload.disabled = !enabled;
      if (importMaterials) importMaterials.disabled = !enabled;
      if (thumbnail) thumbnail.disabled = !enabled;
      if (integrity) integrity.disabled = !enabled || checkingIntegrity;
    }
    syncEmojiActions(false);
    panel.append(form);
    if (emoji) panel.append(actions('表情操作', batchUpload, integrity, importMaterials, thumbnail, upload));
    if (batch) panel.append(actions('所选表情', selectedCount, batch.remove, batch.restore, exportMaterials));
    if (counts) panel.append(counts);
    const pager = searchable({panel, grid:true, id:kind === 'emoji' ? 'learning-emoji' : 'archived-images',
      render:item => renderMedia(item, pager.read, batch, openReview),
      empty:() => emoji ? '还没有符合条件的表情。点「上传表情」或「导入素材集」添加。' : '还没有迁入的图片。',
      prepare:() => {
        batch?.clear();
        syncEmojiActions(false);
      },
      loaded:data => {
        syncEmojiActions(data.enabled);
        if (counts) {
          const total = data.counts;
          counts.textContent = total ? `全库 ${total.total} 条 · 已登记 ${total.registered} · 未登记 ${total.unregistered} · 已禁用 ${total.banned} · 已移除 ${total.removed}` : '';
          if (data.capacity) counts.textContent += ` · 活跃登记 ${data.capacity.active} / ${data.capacity.limit}`;
        }
      }},
      form, '/api/resources/media', fields => ({kind, q:fields.q.value, ...(emoji ? {
        status:fields.status.value, review:fields.review.value, format:fields.format.value, sort:fields.sort.value,
        removed:fields.removed.checked,
      } : {})}));
    return pager;
  }

  const selection = $('memory-select-all'), correct = $('memory-correct');
  const toggleRemoved = $('memory-toggle-removed');
  const selectedCount = $('memory-selected-count');
  let removedView = false;
  function syncSelection() {
    const boxes = [...memory.list.querySelectorAll('input[type=checkbox]')];
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
  function lifecycleActions(item) {
    const relation = item.source.table === 'relations';
    const operations = item.status !== 'active' ? [] : item.is_removed ? ['restore'] : relation ? [
      item.is_frozen ? 'unfreeze' : 'freeze', item.is_protected ? 'unprotect' : 'protect',
      ...(!item.is_frozen && !item.is_protected ? [item.is_enabled ? 'disable' : 'enable', 'remove'] : []),
    ] : ['correct', item.is_enabled ? 'disable' : 'enable', 'remove'];
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
  function renderMemory(item) {
    const type = labels[item.labels?.[0] ?? item.source?.table ?? 'live'] || item.source?.table;
    if (item.kind === 'summary') return renderSummaryFact(item);
    const relation = item.source?.table === 'relations';
    const guided = ['fact_claims', 'relations', 'episodes'].includes(item.source?.table);
    const box = guided ? null : el('input', {type:'checkbox', 'aria-label':'选择' + type});
    box?.addEventListener('change', () => {
      if (box.checked) selected.set(box, item); else selected.delete(box);
      syncSelection();
    });
    const status = item.is_removed ? '已移除' : item.is_recallable ? '' : '未参与召回';
    const meta = [type, item.recorded_at && fmtTime(item.recorded_at, true), status].filter(Boolean).join(' · ');
    return record({text:item.text.slice(0, 100) || '空文本', meta},
      () => detail(type || '文字记忆', field('正文', item.text), field('原件', item.original_text),
        field('人物', item.actor_label), field('时间', item.recorded_at && fmtTime(item.recorded_at, true)),
        field('召回状态', item.is_recallable ? '参与召回' : '不参与召回'),
        relation && field('关系状态', item.is_frozen ? '已冻结' : item.is_protected ? '受保护' : '正常'),
        field('来源', sourceText(item.source)),
        field('证据', item.evidence?.map(entry => [term(entry.kind),
          sourceText(entry.source), term(entry.status)].filter(Boolean).join(' · ')).join('\n')),
        guided ? lifecycleActions(item) : !item.is_removed && el('div', {class:'row'}, action('修正', () => editMemories([item])),
          action(item.is_enabled ? '停用召回' : '恢复召回', async event => {
            const button = event.currentTarget;
            if (await change('/api/resources/memory', 'PATCH', {...memoryTarget(item), is_enabled:!item.is_enabled},
              memory.read, button) && button.isConnected) $('editor-dialog').close();
          }))), box);
  }
  function renderSummaryFact(item) {
    const title = item.text || (item.status === 'superseded' ? '已更正的事实' : '没有证据的事实');
    const meta = ['会话总结事实', item.status === 'superseded' ? '已更正' : term(item.status)].filter(Boolean).join(' · ');
    return record({text:title, meta}, async () => {
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
      detail('会话总结事实', field('事实', fact.text), field('事实来源', sourceText(fact.source)),
        field('状态', fact.status === 'superseded' ? '已更正' : term(fact.status)),
        field('证据', fact.evidence.map(entry => [term(entry.kind),
          sourceText(entry.source)].filter(Boolean).join(' · ')).join('\n') || '没有可用证据'),
        lifecycleActions({...item, ...fact}));
    });
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
    for (const box of memory.list.querySelectorAll('input[type=checkbox]')) {
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
  function qualityView(panel) {
    const endpoint = '/api/resources/learning/quality';
    const statuses = {passed:'模型通过', rejected:'模型拒绝', error:'质检错误'};
    const reasons = {matched:'说法符合情境且可复用', mismatch:'说法与情境不匹配',
      too_specific:'说法过于特指', no_route:'没有可用的质检器官', blocked:'预算或安全网关拒绝',
      failed:'器官调用失败', invalid:'返回格式不符合要求'};
    const changes = {rescued:'人工挽回', withdrawn:'挽回已撤回'};
    const source = el('select', {name:'scope', 'aria-label':'质检来源'});
    const counts = el('p', {class:'muted resource-counts', role:'status'});
    const form = el('form', {class:'row resource-filters'}, source,
      el('select', {name:'status', 'aria-label':'质检状态'}, el('option', {value:'', text:'全部状态'}),
        ...Object.entries(statuses).map(([value, text]) => el('option', {value, text}))),
      el('label', {}, '开始日期（UTC）', el('input', {type:'date', name:'start', 'aria-label':'开始日期（UTC）'})),
      el('label', {}, '结束日期（UTC）', el('input', {type:'date', name:'end', 'aria-label':'结束日期（UTC）'})),
      action('刷新', () => pager.read()), el('button', {class:'btn small primary', text:'查询'}));
    panel.append(el('p', {class:'muted', text:'挽回的条目仅限原会话，无需人工批准。查看记录不会调用模型。'}), form, counts);
    const dialog = $('editor-dialog');
    let revision = 0;
    function invalidate() {
      revision++;
      counts.textContent = '';
      if ($('editor-body').querySelector('[data-quality-detail]') && dialog.open) dialog.close();
    }
    function show(item) {
      const actionType = item.actions.length === 0 ? 'rescued' : 'withdrawn';
      const title = actionType === 'rescued' ? '挽回' : '撤回挽回';
      const body = el('div', {class:'stack resource-detail', 'data-quality-detail':''},
        field('质检状态', statuses[item.status]), field('质检理由', reasons[item.reason]),
        field('质检时间', fmtTime(item.recorded_at, true)), field('来源', item.source_label),
        field('候选说法', item.text), field('情境', item.detail),
        ...item.actions.map(([operation, time]) => field(changes[operation], fmtTime(time, true))));
      const buttons = item.status === 'rejected' && item.actions.length < 2 ? [action('预览' + title, preview)] : [];
      openEditor('表达质检记录', body, false, buttons);
      const current = ++revision;
      const path = endpoint + '/' + item.id;
      const isCurrent = () => current === revision && dialog.open && body.isConnected;
      async function preview() {
        buttons[0].disabled = true;
        const response = await api(path, 'POST', {action:actionType});
        if (!isCurrent()) return;
        buttons[0].disabled = false;
        if (!response.ok) return fail(response);
        const data = response.data;
        body.replaceChildren(field('来源', item.source_label), field('候选说法', data.text), field('情境', data.detail),
          el('p', {class:'muted', text:actionType === 'rescued' ? '不改原来的模型判断。新条目仅此来源会话可用。' : '撤回后不会重新加入回复候选。'}));
        const confirm = action('确认' + title, async () => {
          confirm.disabled = true;
          const result = await api(path, 'POST', {action:actionType, version:data.version});
          if (!isCurrent()) return;
          confirm.disabled = false;
          if (!result.ok) return fail(result);
          dialog.close();
          toast(title + '已记录');
          return pager.read();
        });
        openEditor('确认' + title, body, false, [action('取消', () => dialog.close()), confirm]);
        confirm.focus();
      }
    }
    const pager = createResourcePager({el, panel, id:'learning-quality', error:fail, prepare:invalidate,
      request:({offset, limit, signal}) => {
        const params = new URLSearchParams({offset, limit});
        for (const [name, value] of new FormData(form)) if (value) params.set(name, value);
        return api(endpoint + '?' + params, 'GET', undefined, {}, signal);
      },
      empty:() => Object.values(form.elements).some(control => control.value)
        ? '没有符合条件的质检记录，试试清空筛选条件。'
        : '还没有质检记录。表达在回复时经过质检器官后才会出现在这里。',
      loaded:data => {
        const value = source.value;
        source.replaceChildren(el('option', {value:'', text:'全部获准来源'}),
          ...data.sources.map(entry => el('option', {value:entry.key, text:entry.label})));
        source.value = data.sources.some(entry => entry.key === value) ? value : '';
        counts.textContent = data.enabled ? `共 ${data.total} 条质检记录` : '';
      },
      render:item => record({text:item.text, meta:`${statuses[item.status]} · ${item.source_label}`}, () => show(item)),
    });
    form.addEventListener('submit', event => { event.preventDefault(); counts.textContent = ''; pager.reset(); });
    return {show:() => pager.show(), hide:() => { pager.hide(); source.replaceChildren(); pager.list.replaceChildren(); }};
  }
  function vectorView(panel) {
    const endpoint = '/api/resources/learning/vector-spaces';
    const select = el('select', {'aria-label':'选择要补建的向量空间'});
    const offers = el('div', {class:'stack'});
    const status = el('p', {class:'muted', role:'status'});
    const cost = el('p', {class:'muted'});
    const states = {running:'正在补建', writing:'正在保存，已无法取消', unchanged:'本次没有新增向量',
      failed:'补建失败，未新增向量', cancelled:'补建已取消，未新增向量'};
    let selection = '', currentJob = null, generation = 0, timer, visible = false, pending = false;
    const start = action('补建一批', () => mutate('/backfill', 'POST', {space:select.value}));
    const cancel = action('取消补建', () => mutate('/backfill/' + currentJob.id, 'DELETE'));
    panel.append(cost, offers,
      el('div', {class:'row resource-filters'}, select, start, cancel, action('刷新', refresh)), status);

    function controls() {
      const busy = ['running', 'writing'].includes(currentJob?.state);
      start.disabled = pending || busy || !select.value;
      cancel.disabled = pending || currentJob?.state !== 'running';
    }
    select.addEventListener('change', () => { selection = select.value; controls(); });

    async function refresh() {
      clearTimeout(timer);
      const request = ++generation;
      const response = await api(endpoint);
      if (!visible || request !== generation) return;
      if (!response.ok) {
        offers.replaceChildren();
        select.replaceChildren();
        currentJob = null;
        status.textContent = '暂时无法读取表达向量空间。';
        controls();
        return;
      }
      const {items, batch_limit, job} = response.data;
      currentJob = job;
      cost.textContent = `仅使用当前免费的本地嵌入路线，每次最多补建 ${batch_limit} 条缺项；会占用本地算力，提交前可取消。`;
      offers.replaceChildren(...items.map((item, index) => el('div', {class:'resource-record'},
        el('span', {text:`空间 ${index + 1} · ${item.available ? '可补建' : '已保存'}`}),
        el('span', {class:'muted', text:`待补建 ${item.eligible} 条 · 已写入 ${item.established} 条`}))));
      select.replaceChildren(el('option', {value:'', text:'请选择可用空间'}), ...items.flatMap((item, index) =>
        item.available ? [el('option', {value:item.space, text:`空间 ${index + 1}`})] : []));
      select.value = selection;
      status.textContent = job ? (job.state === 'written' ? `本次写入 ${job.written} 条` : states[job.state]) :
        (items.length ? '' : '当前没有可用或已保存的空间。表达积累并启用向量后，这里会出现可补建的空间。');
      controls();
      if (['running', 'writing'].includes(job?.state)) timer = setTimeout(refresh, 1000);
    }

    async function mutate(path, method, body) {
      if (pending) return;
      pending = true;
      controls();
      const response = await api(endpoint + path, method, body);
      pending = false;
      if (!visible) return;
      if (!response.ok) fail(response);
      await refresh();
    }

    return {show:() => { visible = true; controls(); return refresh(); }, hide:() => {
      visible = false;
      generation++;
      clearTimeout(timer);
      select.replaceChildren();
      offers.replaceChildren();
      status.textContent = '';
      currentJob = null;
      selection = '';
      controls();
    }};
  }
  function clusterView(panel) {
    const endpoint = '/api/resources/learning/clusters';
    const count = el('span', {class:'muted', role:'status', style:'min-height:1.5em'});
    const dialog = $('editor-dialog');
    const memberLimit = 3;
    let members;
    panel.append(el('div', {class:'row resource-filters'}, action('刷新', () => pager.reset()), count));

    function invalidate() {
      count.textContent = '';
      members?.hide();
      members = null;
      if ($('editor-body').querySelector('[data-cluster-detail]')) {
        if (dialog.open) dialog.close();
        $('editor-body').replaceChildren();
      }
    }

    function open(item) {
      const body = el('div', {class:'stack resource-detail', 'data-cluster-detail':'', 'aria-live':'polite'});
      members?.hide();
      members = createPagedResource({el, list:body, label:'成员分页', measure:() => memberLimit,
        isVisible:() => dialog.open && body.isConnected,
        request:async ({offset, limit, signal}) => {
          const params = new URLSearchParams({location:item.location, offset, limit});
          const response = await api(endpoint + '/detail?' + params, 'GET', undefined, {}, signal);
          if (response.ok) return {...response, data:{...response.data, items:response.data.members, enabled:true}};
          return response;
        },
        render:member => el('div', {class:'stack'}, field('表达方式', member.text), field('使用情景', member.detail)),
        loaded:data => body.prepend(el('p', {class:'muted', text:`共 ${data.total} 条可见表达`})),
        error:response => {
          fail(response);
          if (response.status === 404) pager.reset();
        },
      });
      openEditor('表达簇成员', body, false, [members.navigation]);
      return members.show();
    }

    const pager = createResourcePager({el, panel, id:'learning-clusters', error:fail, prepare:invalidate, clearOnError:true,
      request:async ({offset, limit, signal}) => {
        const response = await api(endpoint + '?' + new URLSearchParams({offset, limit}), 'GET', undefined, {}, signal);
        if (response.ok) response.data.enabled = true;
        return response;
      },
      empty:() => '当前没有可见的表达簇。表达积累后会自动聚类。',
      loaded:data => { count.textContent = `共 ${data.total} 个可见表达簇`; },
      render:item => record({text:item.title || '未命名表达簇', meta:`${item.count} 条`}, () => open(item)),
    });
    return {
      show:() => pager.show(),
      hide:() => { pager.hide(); pager.list.replaceChildren(); },
    };
  }
  const learning = {jargon:learningView('jargon'), expression:learningView('expression'),
    quality:qualityView($('learning-panel-quality')),
    vectors:vectorView($('learning-panel-vectors')),
    clusters:clusterView($('learning-panel-clusters')), emoji:mediaView($('learning-panel-emoji'), 'emoji')};
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
    };
  }
  const graph = graphView($('memory-panel-graph'));
  function prepareLearning(kind = learningKind) {
    activeLearning?.hide();
    learningKind = kind;
    activate('data-learning-tab', kind);
    switchPanel({jargon:'learning-panel-jargon', expression:'learning-panel-expression',
      emoji:'learning-panel-emoji'}, kind);
    if (kind === 'expression') {
      activate('data-expression-view', expressionView);
      switchPanel({list:'learning-expression-list', quality:'learning-panel-quality',
        vectors:'learning-panel-vectors',
        clusters:'learning-panel-clusters'}, expressionView);
    }
    const view = kind === 'expression' && expressionView !== 'list' ? learning[expressionView] : learning[kind];
    activeLearning = view;
    return () => view.show();
  }
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
  bindTabs('data-learning-tab', button => prepareLearning(button.dataset.learningTab)());
  bindTabs('data-expression-view', button => {
    expressionView = button.dataset.expressionView;
    return prepareLearning('expression')();
  });
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
  $('memory-inspect-text').addEventListener('click', () => jumpToQuery('text', $('memory-search')));
  $('memory-inspect-profiles').addEventListener('click', () => jumpToQuery('profiles', profileForm));
  $('memory-removal-query').addEventListener('click', () => jumpToQuery('text', $('memory-search')));
  const workbenchHost = $('learning-workbench-host');
  const workbenchFrame = $('learning-workbench-frame');
  const workbenchOpen = $('learning-workbench-open');
  const legacyLearning = $('page-learning').querySelector('.page-body');
  let workbenchVisible = false;
  workbenchOpen.addEventListener('click', () => {
    workbenchVisible = true;
    activeLearning?.hide();
    workbenchHost.classList.remove('hidden');
    legacyLearning.classList.add('hidden');
    workbenchOpen.classList.add('hidden');
    if (!workbenchFrame.getAttribute('src')) workbenchFrame.src = '/learning-workbench/';
  });
  $('learning-workbench-close').addEventListener('click', () => {
    workbenchVisible = false;
    workbenchHost.classList.add('hidden');
    legacyLearning.classList.remove('hidden');
    workbenchOpen.classList.remove('hidden');
    prepareLearning()();
  });
  workbenchFrame.addEventListener('load', () => {
    workbenchFrame.contentWindow.postMessage({type:'aureus-theme', theme:document.documentElement.dataset.theme}, location.origin);
  });
  window.addEventListener('message', event => {
    if (event.origin === location.origin && event.source === workbenchFrame.contentWindow
      && event.data?.type === 'aureus-login-required') location.reload();
  });
  PAGES.learning = {prepare:() => workbenchVisible ? () => {} : prepareLearning(), leave:() => activeLearning?.hide()};
  PAGES.memory = {prepare:prepareMemory, leave:() => activeMemory?.hide()};
  return showMemoryTab;
}
