'use strict';

function createResourcePager({el, panel, id, request, render, grid = false, prepare, loaded, empty, error, clearOnError = false}) {
  const list = el('div', {id:id + '-result', class:'resource-list' + (grid ? ' resource-grid' : ''), 'aria-live':'polite'});
  const previous = el('button', {type:'button', class:'btn small', text:'上一页', disabled:true});
  const next = el('button', {type:'button', class:'btn small', text:'下一页', disabled:true});
  const status = el('span', {class:'muted', role:'status'});
  const navigation = el('div', {class:'row resource-pagination', role:'group', 'aria-label':'分页'}, previous, status, next);
  panel.append(list, navigation);
  let page = 0, pages = null, serial = 0, limit = 0, active = false, busy = false, cursors = [null];

  function measure() {
    const style = getComputedStyle(list);
    const gap = parseFloat(style.rowGap);
    const row = parseFloat(style.getPropertyValue(grid ? '--resource-tile' : '--resource-row'));
    const rows = Math.max(1, Math.floor((list.clientHeight + gap) / (row + gap)));
    const columns = grid ? Math.max(1, Math.floor((list.clientWidth + gap) / (row + gap))) : 1;
    return Math.min(100, rows * columns);
  }
  function sync() {
    previous.disabled = !active || busy || page === 0;
    next.disabled = !active || busy || cursors.length <= page + 1 || (pages != null && page + 1 >= pages);
    navigation.style.visibility = !busy && pages == null ? 'hidden' : '';
    status.textContent = busy ? '读取中…' : pages == null ? '' : `第 ${pages ? page + 1 : 0} / ${pages} 页`;
    list.setAttribute('aria-busy', busy);
  }
  function message(text) {
    list.replaceChildren(el('p', {class:'muted resource-empty', text}));
  }
  async function read(target = page) {
    if (!active) return;
    const generation = ++serial;
    busy = true;
    sync();
    prepare?.();
    message('读取中…');
    const response = await request({offset:target * limit, before:cursors[target], limit});
    if (!active || generation !== serial) return;
    busy = false;
    if (!response.ok) {
      if (clearOnError) {
        page = 0;
        pages = null;
        cursors = [null];
      }
      sync();
      list.replaceChildren(el('div', {class:'row resource-empty'}, el('span', {text:'读取失败'}),
        el('button', {class:'btn small', type:'button', text:'重试', onclick:() => read(clearOnError ? 0 : target)})));
      error(response);
      return;
    }
    const total = response.data.total;
    pages = Number.isInteger(total) && total >= 0 ? Math.ceil(total / limit) : null;
    if (target > 0 && ((pages != null && target >= pages) || !response.data.items.length)) {
      return read(pages == null ? target - 1 : Math.min(target - 1, Math.max(0, pages - 1)));
    }
    page = target;
    const following = response.data.next_offset ?? response.data.next_before;
    cursors = cursors.slice(0, page + 1);
    if (following != null) cursors.push(following);
    sync();
    if (response.data.items.length) list.replaceChildren(...response.data.items.map(render));
    else message(response.data.enabled ? (empty?.(response.data) ?? '没有符合条件的记录') : '此功能当前关闭');
    loaded?.(response.data);
  }
  function reset(newLimit = measure()) {
    limit = newLimit;
    page = 0;
    pages = null;
    cursors = [null];
    return read();
  }
  function resize() {
    if (!active || !list.clientWidth) return;
    const capacity = measure();
    if (capacity !== limit) reset(capacity);
  }
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(resize).observe(list);
  previous.addEventListener('click', () => { if (!previous.disabled) return read(page - 1); });
  next.addEventListener('click', () => { if (!next.disabled) return read(page + 1); });
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
        resize();
      }
    }));
    sizes.append(...buttons);
    panel.insertBefore(sizes, list);
  }
  return {
    list, reset, read,
    show() { active = true; const capacity = measure(); return capacity === limit ? read() : reset(capacity); },
    hide() { active = busy = false; serial++; sync(); prepare?.(); },
  };
}

function installResourcePages({$, api, el, fail, fmtTime, openEditor, toast, PAGES}) {
  const labels = {paragraphs:'段落', relations:'关系', fact_claims:'事实',
    episodes:'情景记忆', messages:'消息', experiences:'其他经历', mai_messages:'消息'};
  const learningLabels = {jargon:'黑话', expression:'表达方式'};
  const reviewLabels = {pending:'待审', approved:'通过', rejected:'拒绝'};
  const reviewEndpoint = '/api/resources/learning/reviews';
  const scopeEndpoint = '/api/resources/learning/scope';
  let learningKind = 'jargon', memoryTab = 'query', queryKind = 'text';
  let activeLearning, activeMemory;
  const selected = new Map();

  function activate(attribute, value) {
    for (const button of document.querySelectorAll('[' + attribute + ']')) {
      const active = button.getAttribute(attribute) === value;
      button.classList.toggle('primary', active);
      button.setAttribute('aria-pressed', active);
    }
  }
  function switchPanel(ids, value) {
    for (const [name, id] of Object.entries(ids)) $(id).classList.toggle('hidden', name !== value);
  }
  function action(label, onclick) {
    return el('button', {class:'btn small', type:'button', text:label, onclick});
  }
  function field(label, value) {
    return value == null || value === '' ? null : el('p', {}, el('strong', {text:label + '：'}), String(readable(value)));
  }
  const terms = {...labels, active:'有效', inactive:'已停用', conflicted:'有冲突', superseded:'已替代',
    retracted:'已撤回', deleted:'已删除', missing:'缺失', image_assets:'图片资产', message:'消息',
    attachment_archived:'归档附件', media_type:'媒体类型', source_key:'原记录编号', reference:'引用',
    evidence:'证据', name:'名称', id:'编号', kind:'类别', status:'状态', profile:'人物画像'};
  const term = key => terms[key] ?? key;
  function readable(value) {
    if (typeof value === 'boolean') return value ? '是' : '否';
    if (Array.isArray(value)) return value.map(readable).join('、');
    if (value && typeof value === 'object') return Object.entries(value)
      .filter(([, content]) => content != null && content !== '').map(([key, content]) => term(key) + '：' + readable(content)).join('\n');
    return value;
  }
  function sourceId(value) {
    if (typeof value === 'string' && value.startsWith('[')) {
      try { return readable(JSON.parse(value)); } catch { return value; }
    }
    return readable(value);
  }
  function sourceText(source) {
    if (!source || typeof source !== 'object') return source;
    return [['实例', source.instance], ['数据库', source.role], ['记录类型', term(source.table ?? source.kind)],
      ['原记录编号', sourceId(source.source_key ?? source.key)]]
      .filter(([, value]) => value != null && value !== '').map(([label, value]) => label + '：' + value).join('；') || readable(source);
  }
  function detail(title, ...content) {
    openEditor(title, el('div', {class:'stack resource-detail'}, ...content));
  }
  function record(title, open, box) {
    return el('div', {class:'resource-record'}, box,
      el('button', {class:'btn small', type:'button', onclick:open}, el('span', {text:title})));
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
    const pager = createResourcePager({...options, el, error:fail, request:({offset, limit}) => {
      params ??= getParams(form.elements);
      return api(endpoint + '?' + new URLSearchParams({...params, offset, limit}));
    }});
    form.addEventListener('submit', event => { event.preventDefault(); apply(); });
    form.addEventListener('change', event => { if (event.target.name !== 'q') apply(); });
    return Object.assign(pager, {apply});
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
    const description = textControl(kind === 'jargon' ? '释义' : '使用场景', item?.detail ?? '', 160, true);
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
  function batchControls(keys, endpoint, payload, refresh, selectionChanged) {
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
      busy = true;
      sync();
      try { await change(endpoint, 'PATCH', payload(entries, !restoring), refresh, button); }
      finally { busy = false; sync(); }
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
  function reviewOptions(placeholder) {
    return Object.entries({'':placeholder, ...reviewLabels}).map(([value, text]) => el('option', {value, text}));
  }
  function expressionReviewForm(entryKeys, refresh, initial = '') {
    const status = el('select', {required:true}, ...reviewOptions('选择审核结论'));
    status.value = initial;
    const reason = textControl('审核理由（拒绝时必填）', '', 500, initial === 'rejected');
    status.addEventListener('change', () => { reason.input.required = status.value === 'rejected'; });
    const submit = el('button', {type:'submit', class:'btn primary small', text:'保存审核'});
    const controls = el('fieldset', {class:'stack'}, el('label', {class:'stack'}, '审核结论', status), reason.field,
      el('p', {class:'muted', text:'拒绝会排除该表达，但不会移除原件。通过不会启用停用、已移除或待映射的表达。'}), submit);
    const form = el('form', {class:'stack'}, controls);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (controls.disabled) return;
      controls.disabled = true;
      try {
        const response = await api(reviewEndpoint, 'PATCH', {
          entry_keys:entryKeys, status:status.value, reason:reason.input.value,
        });
        if (!response.ok) { fail(response); return; }
        if (form.isConnected) $('editor-dialog').close();
        toast(`已审核 ${response.data.changed} 条表达`);
        await refresh();
      } finally { controls.disabled = false; }
    });
    return form;
  }
  function expressionReviewHistory(item) {
    const body = el('div', {class:'stack'});
    const history = el('details', {}, el('summary', {text:'审核历史'}), body);
    let loaded = false;
    history.addEventListener('toggle', async () => {
      if (!history.open || loaded) return;
      const loading = el('p', {text:'读取中…'});
      body.replaceChildren(loading);
      const response = await api(reviewEndpoint + '?' + new URLSearchParams({entry_key:item.entry_key}));
      if (!loading.isConnected) return;
      if (!response.ok) { body.replaceChildren(el('p', {text:'读取失败，可收起后重新打开。'})); return; }
      loaded = true;
      body.replaceChildren(...(response.data.items.length ? response.data.items.map(review =>
        field(fmtTime(review.recorded_at, true), reviewLabels[review.status] + (review.reason ? ' · ' + review.reason : ''))
      ) : [el('p', {text:'暂无审核记录'})]));
    });
    return history;
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
            el('p', {text:'这里只设置授权范围，不改变停用、移除或审核状态。'}));
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
    const panel = $('learning-panel-' + kind);
    const disabled = el('label', {}, el('input', {name:'disabled', type:'checkbox'}), ' 显示停用和待映射');
    const removed = el('label', {}, el('input', {name:'removed', type:'checkbox'}), ' 显示已移除');
    const reviewFilter = kind === 'expression' ? el('select', {name:'review', 'aria-label':'审核状态'}, ...reviewOptions('全部')) : null;
    const scopeDescription = '会话筛选按学习来源或导入包授权浏览，不改变实际召回范围。';
    const scopeHint = el('p', {class:'muted', id:'learning-scope-hint-' + kind,
      'aria-live':'polite', text:scopeDescription});
    const all = el('option', {value:'', text:'全部资料'});
    const scope = el('select', {name:'conversation_key', 'aria-label':'浏览会话',
      'aria-describedby':scopeHint.id, disabled:true}, all);
    const toolbar = searchForm('搜索' + learningLabels[kind], '搜索词、释义或例句', disabled, removed,
      reviewFilter && el('label', {}, '审核状态 ', reviewFilter), scope);
    const add = action('添加' + learningLabels[kind], () => editLearning(kind, pager.read));
    const upload = action('导入', () => importLearning(kind, pager.read));
    const download = action('导出', () => {
      const link = el('a', {href:'/api/resources/learning/package/' + kind, download:''});
      document.body.append(link); link.click(); link.remove();
    });
    const queue = reviewFilter && action('快速审核', () => {
      toolbar.elements.review.value = 'pending';
      toolbar.elements.disabled.checked = true;
      toolbar.elements.removed.checked = false;
      toolbar.elements.q.value = '';
      return pager.apply();
    });
    const reviewBatch = reviewFilter && action('审核所选', () => {
      const keys = batch.keys();
      detail(`审核 ${keys.length} 条所选表达`, expressionReviewForm(keys, pager.read));
    });
    const batch = batchControls(item => item.entry_key, '/api/resources/learning/batch',
      (entry_keys, removed) => ({kind, entry_keys, removed}), () => pager.read(),
      count => { if (reviewBatch) reviewBatch.disabled = !count; });
    const tools = [add, upload, download, ...(queue ? [queue] : [])];
    for (const tool of tools) tool.disabled = true;
    toolbar.append(...tools, batch.remove, batch.restore);
    if (reviewBatch) toolbar.append(reviewBatch);
    panel.append(toolbar, scopeHint);
    function render(item) {
      const title = item.text + (item.is_removed ? ' · 已移除' : item.pending_scope ? ' · 待映射' : item.is_enabled ? '' : ' · 已停用')
        + (item.review_status ? ' · ' + reviewLabels[item.review_status] : '');
      const open = () => detail(title, field(kind === 'jargon' ? '释义' : '使用场景', item.detail),
        field('例句', item.example), field('证据数', item.count), field('发言者', item.speakers.join('、')),
        field('时间线证据', item.evidence_ids.join('、')), field('迁入来源', item.source_refs.map(sourceId).join('；')),
        item.package_source != null && field('包内来源会话', item.package_source || '未提供名称'),
        item.package_source != null && field('已授权会话数', item.scope_keys.length),
        item.original && field('原件', item.original.filter(Boolean).join('\n')),
        item.review_status && field('审核状态', reviewLabels[item.review_status]),
        item.review_status && expressionReviewHistory(item),
        item.review_status && expressionReviewForm([item.entry_key], pager.read,
          item.review_status === 'pending' ? '' : item.review_status),
        el('div', {class:'row'}, action('编辑', () => editLearning(kind, pager.read, item)),
          item.package_source != null && action('设置生效会话', () => editLearningScope(kind, item, pager.read)),
          !item.pending_scope && !item.is_removed && action(item.is_enabled ? '停用' : '恢复', async event => {
            const button = event.currentTarget;
            if (await change('/api/resources/learning', 'PATCH', {kind, entry_key:item.entry_key,
              is_enabled:!item.is_enabled}, pager.read, button) && button.isConnected) $('editor-dialog').close();
          })));
      return record(title, open, batch.checkbox(item));
    }
    const pager = searchable({panel, id:'learning-' + kind, render,
      empty:() => scope.value ? '该会话没有符合条件的资料' : '没有符合条件的记录', prepare:() => {
      batch.clear(); for (const tool of tools) tool.disabled = true;
    }, loaded:data => { for (const tool of tools) tool.disabled = !data.enabled; }}, toolbar, '/api/resources/learning',
    fields => ({kind, q:fields.q.value, recall:fields.disabled.checked || fields.removed.checked ? '' : 'enabled',
      removed:fields.removed.checked, conversation_key:scope.value,
      ...(fields.review?.value ? {review:fields.review.value} : {})}));
    let scopeGeneration = 0;
    async function loadScopes() {
      const generation = ++scopeGeneration;
      scope.disabled = true;
      scope.replaceChildren(all);
      scopeHint.textContent = '正在确认会话列表；当前浏览全部资料。' + scopeDescription;
      pager.apply();
      const response = await api(scopeEndpoint);
      if (generation !== scopeGeneration) return;
      if (!response.ok) {
        scopeHint.textContent = '会话列表读取失败；当前仅可浏览全部资料。' + scopeDescription;
        return;
      }
      scope.replaceChildren(all, ...response.data.items.map(target =>
        el('option', {value:target.key, text:target.label})));
      scope.disabled = !response.data.items.length;
      scopeHint.textContent = (scope.disabled ? '暂无已知会话；当前仅可浏览全部资料。' : '')
        + scopeDescription;
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
  function emojiForm(refresh, item) {
    const submit = el('button', {type:'submit', class:'btn primary small', text:item ? '保存' : '上传'});
    const file = el('input', {type:'file', accept:'image/png,image/jpeg,image/gif,image/webp', required:true});
    const description = textControl('表情描述', item?.text ?? '', 500, item?.is_registered);
    const tags = textControl('标签（每行一个）', item?.tags?.join('\n') ?? '', 819);
    const controls = el('fieldset', {class:'stack'}, item ? [description.field, tags.field] : [
      el('label', {class:'stack'}, '图片文件', file),
      el('p', {class:'muted', text:'支持 PNG、JPEG、GIF 和 WebP，最多 5 MB。上传后可填写描述并登记。'}),
    ], submit);
    const form = el('form', {class:'stack'}, controls);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (controls.disabled) return;
      const values = tags.input.value.split('\n').map(tag => tag.trim()).filter(Boolean);
      if (values.length > 20 || values.some(tag => [...tag].length > 40)) {
        toast('最多填写 20 个标签，每个不超过 40 字。', 'error');
        return;
      }
      const endpoint = '/api/resources/emoji' + (item ? '/' + encodeURIComponent(item.digest) : '');
      controls.disabled = true;
      try {
        const response = await api(endpoint, item ? 'PATCH' : 'POST', item ? {
          description:description.input.value, tags:values,
        } : file.files[0]);
        if (!response.ok) { fail(response); return; }
        if (form.isConnected) $('editor-dialog').close();
        toast(item ? '已保存' : response.data.created ? '已上传' : '已存在，保留原有记录');
        await refresh();
      } finally { controls.disabled = false; }
    });
    openEditor(item ? '编辑表情' : '上传表情', form);
  }
  function emojiBatchForm(refresh) {
    const file = el('input', {type:'file', accept:'image/png,image/jpeg,image/gif,image/webp', multiple:true, required:true});
    const summary = el('p', {class:'muted', 'aria-live':'polite', text:'尚未选择图片'});
    const results = el('div', {class:'stack', 'aria-live':'polite'});
    const submit = el('button', {type:'submit', class:'btn primary small', text:'上传所选图片', disabled:true});
    const controls = el('fieldset', {class:'stack'},
      el('label', {class:'stack'}, '图片文件', file), summary, results, submit);
    const form = el('form', {class:'stack'}, controls);
    file.addEventListener('change', () => {
      summary.textContent = `已选择 ${file.files.length} 张图片。每张最多 5 MB。`;
      results.replaceChildren();
      submit.disabled = !file.files.length;
    });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (controls.disabled || !file.files.length) return;
      const files = [...file.files];
      controls.disabled = true;
      results.replaceChildren();
      try {
        for (const image of files) {
          const response = await api('/api/resources/emoji', 'POST', image);
          const outcome = response.ok ? response.data?.created ? '已上传' : '已存在，保留原有记录'
            : errorText(response);
          results.append(el('p', {text:image.name + '：' + outcome}));
        }
        await refresh();
      } finally { controls.disabled = false; submit.disabled = !file.files.length; }
    });
    openEditor('批量上传表情', form);
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
    actions.append(update(item.is_banned ? '恢复' : '禁用', {is_banned:!item.is_banned}));
    return actions;
  }
  function renderMedia(item, refresh, batch) {
    const emoji = item.media_kind === 'emoji';
    const status = emoji ? (item.is_removed ? '已移除' : item.is_banned ? '已禁用' : item.is_registered ? '已登记' : '未登记') : item.status;
    const title = item.text || '未填写描述';
    const url = item.digest ? '/api/resources/media/' + encodeURIComponent(item.digest) + (emoji ? '?kind=emoji' : '') : null;
    const tile = mediaTile(emoji ? status + ' · ' + title : title, url, '原图不可用', () => detail(emoji ? '表情详情' : '图片详情', fullImage(url),
      field('描述', title), field('状态', term(status)), field('来源', sourceText(item.source)),
      emoji && field('格式', item.media_type?.split('/')[1]?.toUpperCase()),
      emoji && field('标签', item.tags?.join('、')),
      emoji && field('收集次数', item.collection_count), emoji && field('使用次数', item.use_count),
      !url && field('原图', '缺少原件'), emoji && url && !item.is_removed && emojiActions(item, refresh)));
    return batch && item.entry_key ? el('div', {class:'resource-tile-selection'}, tile, batch.checkbox(item)) : tile;
  }
  function mediaView(panel, kind) {
    const emoji = kind === 'emoji';
    const select = (label, name, options) => el('label', {}, label + ' ', el('select', {name},
      ...Object.entries(options).map(([value, text]) => el('option', {value, text}))));
    const status = emoji ? select('表情状态', 'status',
      {'':'全部', registered:'已登记', unregistered:'未登记', banned:'已禁用'}) : null;
    const format = emoji ? select('表情格式', 'format', {'':'全部', png:'PNG', jpeg:'JPEG', gif:'GIF', webp:'WebP'}) : null;
    const sort = emoji ? select('表情排序', 'sort',
      {recent:'最近收集', most_used:'使用最多', most_collected:'收集最多', last_used:'最近使用'}) : null;
    const label = kind === 'emoji' ? '搜索表情包' : '搜索迁入图片';
    const removed = kind === 'emoji' ? el('label', {}, el('input', {name:'removed', type:'checkbox'}), ' 显示已移除') : null;
    const form = searchForm(label, label, status, format, sort, removed);
    const counts = emoji ? el('div', {class:'muted', id:'emoji-library-counts'}) : null;
    const batch = kind === 'emoji' ? batchControls(item => item.entry_key, '/api/resources/emoji/batch',
      (entry_keys, removed) => ({entry_keys, removed}), () => pager.read()) : null;
    const upload = kind === 'emoji' ? action('上传表情', () => emojiForm(pager.read)) : null;
    const batchUpload = kind === 'emoji' ? action('批量上传', () => emojiBatchForm(pager.read)) : null;
    if (upload) { upload.disabled = true; form.append(upload); }
    if (batchUpload) { batchUpload.disabled = true; form.append(batchUpload); }
    if (batch) form.append(batch.remove, batch.restore);
    panel.append(form);
    if (counts) panel.append(counts);
    const pager = searchable({panel, grid:true, id:kind === 'emoji' ? 'learning-emoji' : 'archived-images',
      render:item => renderMedia(item, pager.read, batch), prepare:() => {
        batch?.clear(); if (upload) upload.disabled = true;
      },
      loaded:data => {
        if (upload) upload.disabled = !data.enabled;
        if (batchUpload) batchUpload.disabled = !data.enabled;
        if (counts) {
          const total = data.counts;
          counts.textContent = total ? `全库 ${total.total} 条 · 已登记 ${total.registered} · 未登记 ${total.unregistered} · 已禁用 ${total.banned} · 已移除 ${total.removed}` : '';
        }
      }},
      form, '/api/resources/media', fields => ({kind, q:fields.q.value, ...(emoji ? {
        status:fields.status.value, format:fields.format.value, sort:fields.sort.value,
        removed:fields.removed.checked,
      } : {})}));
    return pager;
  }

  const selection = $('memory-select-all'), correct = $('memory-correct');
  const toggleRemoved = $('memory-toggle-removed');
  let removedView = false;
  function syncSelection() {
    const boxes = [...memory.list.querySelectorAll('input[type=checkbox]')];
    selection.disabled = !boxes.length;
    selection.checked = boxes.length > 0 && boxes.every(box => box.checked);
    selection.indeterminate = selected.size > 0 && !selection.checked;
    correct.disabled = removedView || !selected.size;
    toggleRemoved.disabled = !selected.size;
  }
  function memoryTarget(item) {
    return {kind:item.kind ?? (item.source ? 'history' : 'live'), entry_id:item.entry_id ?? item.record_id};
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
    const box = el('input', {type:'checkbox', 'aria-label':'选择' + type});
    box.addEventListener('change', () => {
      if (box.checked) selected.set(box, item); else selected.delete(box);
      syncSelection();
    });
    const title = (item.is_removed ? '已移除 · ' : item.is_recallable ? '' : '未参与召回 · ') + (item.text.slice(0, 100) || '空文本');
    return record(title, () => detail(type || '文字记忆', field('正文', item.text), field('原件', item.original_text),
      field('人物', item.actor_label), field('时间', item.recorded_at && fmtTime(item.recorded_at, true)),
      field('召回状态', item.is_recallable ? '参与召回' : '不参与召回'), field('原始状态', term(item.status)),
      field('来源', sourceText(item.source)), field('时间线编号', item.record_id),
      field('证据', item.evidence?.map(entry => [term(entry.kind) + '：' + entry.reference,
        sourceText(entry.source), term(entry.status)].filter(Boolean).join(' · ')).join('\n')),
      !item.is_removed && el('div', {class:'row'}, action('修正', () => editMemories([item])),
        action(item.is_enabled ? '停用召回' : '恢复召回', async event => {
          const button = event.currentTarget;
          if (await change('/api/resources/memory', 'PATCH', {...memoryTarget(item), is_enabled:!item.is_enabled},
            memory.read, button) && button.isConnected) $('editor-dialog').close();
        }))), box);
  }
  const counts = $('memory-counts');
  const memory = searchable({panel:$('memory-query-text'), id:'memory', render:renderMemory, prepare:() => {
    selected.clear();
    selection.checked = selection.indeterminate = false;
    selection.disabled = correct.disabled = toggleRemoved.disabled = true;
  }, loaded:data => {
    if (data.counts) counts.textContent = Object.entries(data.counts).filter(([kind]) => kind in labels)
      .map(([kind, count]) => labels[kind] + ' ' + count).join('　');
    syncSelection();
  }}, $('memory-search'), '/api/resources/memory', fields => {
    counts.textContent = '';
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
  const profileForm = searchForm('搜索人物画像', '搜索人物、画像或来源 ID');
  profilePanel.append(profileForm);
  const profiles = searchable({panel:profilePanel, id:'profiles', render:item => {
    if (item.entry_type === 'person') {
      const title = item.name || `人物 #${item.person_id}`;
      return record(title + ' · 人物档案', () => detail('人物档案', field('人物编号', item.person_id),
        field('当前称呼', item.name), field('首次记录时间', fmtTime(item.first_seen, true))));
    }
    const profile = item.record;
    const title = item.name || profile.name || profile.nickname || profile.subject_key || '待关联人物';
    return record(title, () => detail(title, field('画像', profile.summary || profile.impression),
      field('人物', item.person_id), field('别名', profile.aliases?.join('、')),
      field('关联状态', item.pending_reason), field('证据', readable(profile.evidence)), field('来源', sourceText(profile))));
  }}, profileForm, '/api/resources/profiles', fields => ({q:fields.q.value}));

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
    request:({offset, limit}) => similarTo == null
      ? api('/api/resources/pictures?' + new URLSearchParams({...pictureParams, offset, limit}))
      : api('/api/resources/images?' + new URLSearchParams({similar_to:similarTo, limit})),
    empty:() => similarTo == null ? '没有符合条件的图片' : '没有找到相似图片',
    render:item => {
      const title = item.text || item.conversation || '未填写描述';
      const url = item.digest ? '/api/resources/media/' + encodeURIComponent(item.digest) : null;
      return mediaTile(title, url, '原图未归档', () => detail(title, fullImage(url),
        field('时间', item.timestamp && fmtTime(item.timestamp, true)), field('来源', sourceText(item.source)),
        field('附件', item.attachment), field('消息编号', item.message_id), field('状态', term(item.status)),
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
  function clusterView(panel) {
    const endpoint = '/api/resources/learning/clusters';
    const count = el('span', {class:'muted', role:'status', style:'min-height:1.5em'});
    const dialog = $('editor-dialog');
    const memberLimit = 3;
    panel.append(el('div', {class:'row resource-filters'}, action('刷新', () => pager.reset()), count));

    function invalidate() {
      count.textContent = '';
      if ($('editor-body').querySelector('[data-cluster-detail]')) {
        if (dialog.open) dialog.close();
        $('editor-body').replaceChildren();
      }
    }

    function open(item) {
      const body = el('div', {class:'stack resource-detail', 'data-cluster-detail':'', 'aria-live':'polite'});
      const status = el('span', {class:'muted', role:'status'});
      let offset = 0, nextOffset = null;
      const previous = action('上一页', () => read(Math.max(0, offset - memberLimit)));
      const next = action('下一页', () => read(nextOffset));
      const navigation = el('div', {class:'row resource-pagination', role:'group', 'aria-label':'成员分页'},
        previous, status, next);

      async function read(target) {
        previous.disabled = next.disabled = true;
        status.textContent = '读取中…';
        body.replaceChildren();
        const params = new URLSearchParams({location:item.location, offset:target, limit:memberLimit});
        const response = await api(endpoint + '/detail?' + params);
        if (!dialog.open || !body.isConnected) return;
        if (!response.ok) {
          fail(response);
          return pager.reset();
        }
        const {members, total, next_offset} = response.data;
        if (!members.length && target > 0) {
          return read(Math.max(0, Math.ceil(total / memberLimit) - 1) * memberLimit);
        }
        offset = target;
        nextOffset = next_offset;
        previous.disabled = offset === 0;
        next.disabled = nextOffset == null;
        status.textContent = `第 ${Math.floor(offset / memberLimit) + 1} / ${Math.ceil(total / memberLimit)} 页`;
        body.replaceChildren(el('p', {class:'muted', text:`共 ${total} 条可见表达`}),
          ...members.map(member => el('div', {class:'stack'},
            field('表达方式', member.text), field('使用情景', member.detail))));
      }

      openEditor('表达簇成员', body, false, [navigation]);
      return read(0);
    }

    const pager = createResourcePager({el, panel, id:'learning-clusters', error:fail, prepare:invalidate, clearOnError:true,
      request:async ({offset, limit}) => {
        const response = await api(endpoint + '?' + new URLSearchParams({offset, limit}));
        if (response.ok) response.data.enabled = true;
        return response;
      },
      empty:() => '当前没有可见的表达簇',
      loaded:data => { count.textContent = `共 ${data.total} 个可见表达簇`; },
      render:item => record(`簇 ${item.number} · ${item.count} 条 · ${item.title}`, () => open(item)),
    });
    return {
      show:() => pager.show(),
      hide:() => { pager.hide(); pager.list.replaceChildren(); },
    };
  }
  const learning = {jargon:learningView('jargon'), expression:learningView('expression'),
    clusters:clusterView($('learning-panel-clusters')), emoji:mediaView($('learning-panel-emoji'), 'emoji')};
  const operations = createResourcePager({el, panel:$('memory-panel-operations'), id:'memory-operations', error:fail,
    request:({offset, limit}) => api('/api/resources/memory/operations?' + new URLSearchParams({offset, limit})),
    empty:() => '没有可见的移除或恢复记录',
    render:item => el('div', {class:'resource-record'},
      el('span', {text:`${item.kind === 'remove' ? '移除' : '恢复'} ${item.changed} 条`}),
      el('time', {datetime:item.occurred_at, class:'muted',
        text:`${new Date(item.occurred_at).getFullYear()}年${fmtTime(item.occurred_at, true)}`})),
  });
  function graphView(panel) {
    const endpoint = '/api/resources/memory/graph';
    const refresh = action('刷新', () => pager.reset());
    const unanchor = action('返回全部图谱', () => browse(null));
    const form = searchForm('搜索关系图', '实体名或关系谓词', refresh, unanchor);
    const count = el('span', {class:'muted', role:'status', style:'min-height:1.5em'});
    let generation = 0, anchor = null, query = '';
    panel.append(form, count);
    unanchor.classList.add('hidden');

    function invalidate() {
      generation++;
      count.textContent = '';
      const dialog = $('editor-dialog');
      if (dialog.open && $('editor-body').querySelector('[data-graph-detail]')) dialog.close();
    }
    function browse(source) {
      anchor = source;
      unanchor.classList.toggle('hidden', !anchor);
      query = form.elements.q.value = '';
      return pager.reset();
    }
    function label(item) {
      return item.kind === 'entity' ? `实体 · ${item.name}`
        : `${item.subject.name} → ${item.predicate} → ${item.object.name}`;
    }
    async function open(source) {
      const current = ++generation;
      const response = await api(endpoint + '/detail?' + new URLSearchParams({source}));
      if (current !== generation) return;
      if (!response.ok) {
        fail(response);
        return pager.reset();
      }
      const item = response.data;
      const evidence = item.evidence.map(key => field('证据来源', sourceId(key)));
      const nodes = item.kind === 'entity' ? [item] : [item.subject, item.object];
      openEditor('关系图详情', el('div', {class:'stack resource-detail', 'data-graph-detail':''},
        field('资料', label(item)), field('来源', sourceId(item.source)),
        field('置信度', item.confidence), ...evidence,
        ...nodes.map(node => action(`浏览 ${node.name} 的一跳关系`, () => browse(node.source)))));
    }
    const pager = createResourcePager({el, panel, id:'memory-graph', error:fail, prepare:invalidate, clearOnError:true,
      request:async ({offset, limit}) => {
        const params = new URLSearchParams({q:query, offset, limit});
        if (anchor) params.set('neighbor_of', anchor);
        const response = await api(endpoint + '?' + params);
        if (response.ok) response.data.enabled = true;
        return response;
      },
      empty:data => data.covered ? '没有符合条件的图谱资料' : '当前没有获准的图谱资料',
      loaded:data => { if (data.covered) count.textContent = `共 ${data.total} 项可见资料`; },
      render:item => record(label(item), () => open(item.source)),
    });
    form.addEventListener('submit', event => {
      event.preventDefault();
      query = form.elements.q.value;
      return pager.reset();
    });
    return {
      show:() => pager.show(),
      hide:() => { pager.hide(); pager.list.replaceChildren(); },
    };
  }
  const graph = graphView($('memory-panel-graph'));
  function showLearning(kind) {
    activeLearning?.hide();
    learningKind = kind;
    activate('data-learning-tab', kind);
    switchPanel({jargon:'learning-panel-jargon', expression:'learning-panel-expression',
      clusters:'learning-panel-clusters', emoji:'learning-panel-emoji'}, kind);
    activeLearning = learning[kind];
    return activeLearning.show();
  }
  function showMemoryTab(tab) {
    activeMemory?.hide();
    activeMemory = null;
    memoryTab = tab;
    activate('data-memory-tab', tab);
    switchPanel({query:'memory-panel-query', images:'memory-panel-images', flow:'memory-panel-flow',
      operations:'memory-panel-operations', transfer:'memory-panel-transfer', graph:'memory-panel-graph'}, tab);
    if (tab === 'query') {
      activate('data-memory-query', queryKind);
      switchPanel({text:'memory-query-text', profiles:'memory-query-profiles'}, queryKind);
      activeMemory = queryKind === 'text' ? memory : profiles;
    } else if (tab === 'images') activeMemory = pictures;
    else if (tab === 'flow') activeMemory = window.getMemoryFlow?.();
    else if (tab === 'operations') activeMemory = operations;
    else if (tab === 'graph') activeMemory = graph;
    else window.loadMemoryTransfer?.();
    return activeMemory?.show();
  }
  for (const button of document.querySelectorAll('[data-learning-tab]')) button.addEventListener('click', () => showLearning(button.dataset.learningTab));
  for (const button of document.querySelectorAll('[data-memory-tab]')) button.addEventListener('click', () => showMemoryTab(button.dataset.memoryTab));
  for (const button of document.querySelectorAll('[data-memory-query]')) button.addEventListener('click', () => {
    queryKind = button.dataset.memoryQuery;
    return showMemoryTab('query');
  });
  PAGES.learning = {load:() => showLearning(learningKind), leave:() => activeLearning?.hide()};
  PAGES.memory = {load:() => showMemoryTab(memoryTab), leave:() => activeMemory?.hide()};
  return showMemoryTab;
}
