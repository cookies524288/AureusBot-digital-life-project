function installResourcePages({$, api, el, fail, fmtTime, openEditor, toast, PAGES, loadImages}) {
  const labels = {paragraphs:'段落', relations:'关系', fact_claims:'事实',
    episodes:'情景记忆', mai_messages:'迁入消息', live:'本地经历'};
  let learningKind = 'jargon', memoryTab = 'query', queryKind = 'text', imageSource = 'archive';
  const selected = new Map();
  function activate(attribute, value) {
    for (const button of document.querySelectorAll('[' + attribute + ']')) {
      const active = button.getAttribute(attribute) === value;
      button.classList.toggle('primary', active);
      button.setAttribute('aria-pressed', active);
    }
  }
  function showMemoryTab(tab) {
    memoryTab = tab;
    activate('data-memory-tab', tab);
    for (const panel of ['query', 'images', 'flow', 'transfer']) {
      $('memory-panel-' + panel).classList.toggle('hidden', tab !== panel);
    }
    if (tab === 'query') return memory.read();
    if (tab === 'images') return showImageSource(imageSource);
    if (tab === 'flow') return window.loadMemoryFlow?.();
    if (tab === 'transfer') return window.loadMemoryTransfer?.();
  }
  function pager(id, getQuery, render, prepare, loaded) {
    const form = $(id + '-search'), list = $(id + '-result'), more = $(id + '-more');
    let next = null, serial = 0, query;
    async function read(append = false) {
      if (append && (next === null || more.disabled)) return;
      const request = ++serial;
      if (!append) {
        query = getQuery(form.elements);
        next = null;
        prepare?.();
        list.textContent = '读取中…';
        more.classList.add('hidden');
      }
      more.disabled = true;
      const res = await api(query.endpoint + '?' + new URLSearchParams({...query.params, offset:next ?? 0}));
      if (request !== serial) return;
      more.disabled = false;
      if (!res.ok) { if (!append) list.textContent = '读取失败'; fail(res); return; }
      if (!append) list.replaceChildren();
      if (!append && !res.data.items.length) {
        list.textContent = res.data.enabled ? '没有符合条件的记录' : '此功能当前关闭';
      }
      for (const item of res.data.items) list.append(render(item));
      next = res.data.next_offset;
      more.classList.toggle('hidden', next == null);
      loaded?.(res.data, append);
    }
    form.addEventListener('submit', event => { event.preventDefault(); read(); });
    form.addEventListener('change', event => { if (event.target.name !== 'q') read(); });
    more.addEventListener('click', () => read(true));
    return {read};
  }
  function field(label, value) {
    return value == null || value === '' ? null : el('p', {text:label + '：' + value, style:'white-space:pre-wrap'});
  }
  function detailCard(title, body, extra) {
    return el('details', {class:'card', style:'overflow-wrap:anywhere;flex:1;min-width:0'},
      el('summary', {text:title}), body, extra);
  }
  function action(label, onclick) {
    return el('button', {class:'btn small', type:'button', text:label, onclick});
  }
  function textControl(label, value, max, required = false) {
    const input = el('textarea', {maxlength:max, rows:max > 2000 ? 6 : 2, required});
    input.value = value;
    return {input, field:el('label', {class:'stack'}, label, input)};
  }
  async function change(endpoint, method, payload, refresh, button) {
    button.disabled = true;
    try {
      const res = await api(endpoint, method, payload);
      if (!res.ok) { fail(res); return false; }
      toast('已保存');
      await refresh();
      return true;
    } finally { button.disabled = false; }
  }
  function editLearning(item) {
    const kind = learningKind;
    const title = kind === 'jargon' ? '黑话' : '表达方式';
    const text = textControl(title, item?.text ?? '', 80, true);
    const detail = textControl(kind === 'jargon' ? '释义' : '使用场景', item?.detail ?? '', 160, true);
    const example = textControl('例句', item?.example ?? '', 2000);
    const save = el('button', {class:'btn primary small', text:'保存'});
    const form = el('form', {class:'stack'}, text.field, detail.field, example.field, save);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (await change('/api/resources/learning', item ? 'PATCH' : 'POST', {
        kind, entry_key:item?.entry_key,
        content:{text:text.input.value, detail:detail.input.value, example:example.input.value}
      }, learning.read, save) && form.isConnected) $('editor-dialog').close();
    });
    openEditor((item ? '编辑' : '新增') + title, form);
  }
  function renderLearning(item) {
    if (learningKind === 'emoji') return renderMedia(item, item.is_banned ? '已禁用' : item.is_registered ? '已登记' : '未登记');
    const kind = learningKind;
    return detailCard(item.text + (item.is_enabled ? '' : ' · 已停用'),
      el('div', {class:'stack'}, field(kind === 'jargon' ? '释义' : '使用场景', item.detail),
        field('例句', item.example), field('证据数', item.count), field('发言者', item.speakers.join('、')),
        field('时间线证据', item.evidence_ids.join('、')), field('迁入来源', item.source_refs.join('、')),
        item.original && field('原件', item.original.filter(Boolean).join('\n'))),
      el('div', {class:'row'}, action('编辑', () => editLearning(item)),
        action(item.is_enabled ? '停用' : '恢复', event => change('/api/resources/learning', 'PATCH',
          {kind, entry_key:item.entry_key, is_enabled:!item.is_enabled}, learning.read, event.currentTarget))));
  }
  const learning = pager('learning', fields => ({
    endpoint:learningKind === 'emoji' ? '/api/resources/media' : '/api/resources/learning',
    params:learningKind === 'emoji'
      ? {kind:learningKind, q:fields.q.value, status:fields.emoji_status.value}
      : {kind:learningKind, q:fields.q.value, recall:fields.disabled.checked ? '' : 'enabled'}
  }), renderLearning, null, data => { $('learning-add').disabled = !data.enabled || learningKind === 'emoji'; });
  const archivedImages = pager('archived-images', fields => ({
    endpoint:'/api/resources/media', params:{kind:'image_assets', q:fields.q.value}
  }), renderMedia);
  function showImageSource(source) {
    imageSource = source;
    activate('data-image-source', source);
    $('image-source-archive').classList.toggle('hidden', source !== 'archive');
    $('image-source-live').classList.toggle('hidden', source !== 'live');
    return source === 'archive' ? archivedImages.read() : loadImages();
  }
  for (const button of document.querySelectorAll('[data-image-source]')) button.addEventListener('click', () => showImageSource(button.dataset.imageSource));
  function syncSelection() {
    const boxes = [...$('memory-result').querySelectorAll('input[type=checkbox]')];
    $('memory-select-all').disabled = !boxes.length;
    $('memory-select-all').checked = boxes.length > 0 && boxes.every(box => box.checked);
    $('memory-select-all').indeterminate = selected.size > 0 && !$('memory-select-all').checked;
    $('memory-correct').disabled = !selected.size;
  }
  function memoryTarget(item) {
    return {kind:item.source ? 'history' : 'live', entry_id:item.entry_id ?? item.record_id};
  }
  function editMemories(items) {
    const inputs = items.map(item => ({item, ...textControl(labels[item.source?.table ?? 'live'] || '记忆正文', item.text, 20000, true)}));
    const save = el('button', {class:'btn primary small', text:'保存修正'});
    const form = el('form', {class:'stack'}, inputs.map(row => row.field), save);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      save.disabled = true;
      for (const {item, input} of inputs) {
        if (item.text === input.value) continue;
        const res = await api('/api/resources/memory', 'PATCH', {...memoryTarget(item), text:input.value});
        if (!res.ok) { fail(res); save.disabled = false; return; }
      }
      if (form.isConnected) $('editor-dialog').close();
      toast('已保存');
      await memory.read();
    });
    openEditor('修正记忆', form);
  }
  function renderMemory(item) {
    if (queryKind === 'profiles') {
      const record = item.record;
      return detailCard(record.name || record.nickname || record.subject_key || record.source_key,
        el('div', {class:'stack'}, field('画像', record.summary || record.impression),
          field('人物', item.person_id), field('别名', record.aliases?.join('、')),
          field('关联状态', item.pending_reason), field('证据', record.evidence && JSON.stringify(record.evidence)),
          field('来源', [record.instance, record.kind, record.source_key].join(' / '))));
    }
    const type = labels[item.source?.table ?? 'live'] || item.source.table;
    const box = el('input', {type:'checkbox', 'aria-label':'选择' + type});
    box.addEventListener('change', () => {
      if (box.checked) selected.set(box, item); else selected.delete(box);
      syncSelection();
    });
    const row = detailCard((item.text.slice(0, 100) || '空文本') + (item.is_recallable ? '' : ' · 未参与召回'),
      el('div', {class:'stack'}, field('类型', type), field('正文', item.text), field('原件', item.original_text),
        field('人物', item.actor_label), field('时间', item.recorded_at && fmtTime(item.recorded_at, true)),
        field('原始状态', item.status), field('来源', item.source && JSON.stringify(item.source)),
        field('时间线编号', item.record_id), field('证据', item.evidence?.map(e => e.kind + ' ' + e.reference).join('、'))),
      el('div', {class:'row'}, action('修正', () => editMemories([item])),
        action(item.is_enabled ? '停用召回' : '恢复召回', event => change('/api/resources/memory', 'PATCH',
          {...memoryTarget(item), is_enabled:!item.is_enabled}, memory.read, event.currentTarget))));
    return el('div', {class:'row', style:'align-items:flex-start'}, box, row);
  }
  const memory = pager('memory', fields => ({
    endpoint:queryKind === 'profiles' ? '/api/resources/profiles' : '/api/resources/memory',
    params:queryKind === 'profiles' ? {q:fields.q.value} : {
      q:fields.q.value, kind:fields.type.value === 'live' ? 'live' : 'history',
      source_table:fields.type.value === 'live' ? '' : (fields.type.value || 'text'),
      recall:fields.disabled.checked ? '' : 'enabled'
    }
  }), renderMemory, () => {
    selected.clear();
    $('memory-counts').textContent = '';
    $('memory-select-all').disabled = true;
    $('memory-correct').disabled = true;
  }, (data, append) => {
    if (!append) $('memory-counts').textContent = Object.entries(data.counts || {})
      .filter(([kind]) => kind in labels).map(([kind, count]) => labels[kind] + ' ' + count).join('　');
    syncSelection();
  });
  $('learning-add').addEventListener('click', () => editLearning());
  $('memory-refresh').addEventListener('click', () => memory.read());
  $('memory-correct').addEventListener('click', () => editMemories([...selected.values()]));
  $('memory-select-all').addEventListener('change', event => {
    for (const box of $('memory-result').querySelectorAll('input[type=checkbox]')) {
      box.checked = event.target.checked;
      box.dispatchEvent(new Event('change'));
    }
  });
  for (const button of document.querySelectorAll('[data-learning-tab]')) button.addEventListener('click', () => {
    learningKind = button.dataset.learningTab;
    activate('data-learning-tab', learningKind);
    $('learning-add').classList.toggle('hidden', learningKind === 'emoji');
    $('learning-search').elements.disabled.closest('label').classList.toggle('hidden', learningKind === 'emoji');
    $('learning-search').elements.emoji_status.closest('label').classList.toggle('hidden', learningKind !== 'emoji');
    learning.read();
  });
  for (const button of document.querySelectorAll('[data-memory-tab]')) button.addEventListener('click', () => showMemoryTab(button.dataset.memoryTab));
  for (const button of document.querySelectorAll('[data-memory-query]')) button.addEventListener('click', () => {
    queryKind = button.dataset.memoryQuery;
    activate('data-memory-query', queryKind);
    const profiles = queryKind === 'profiles';
    $('memory-search').elements.type.classList.toggle('hidden', profiles);
    $('memory-show-disabled').classList.toggle('hidden', profiles);
    $('memory-selection').classList.toggle('hidden', profiles);
    memory.read();
  });
  PAGES.learning = {load:learning.read};
  PAGES.memory = {load:() => showMemoryTab(memoryTab)};
  return showMemoryTab;
}

function renderMedia(item, category) {
  const card = document.createElement('details');
  card.className = 'card stack';
  const title = document.createElement('summary');
  title.textContent = (category ? category + ' · ' : '') + (item.text || '未填写描述');
  card.append(title);
  if (item.digest && (item.media_kind !== 'emoji' || item.has_file)) {
    const image = document.createElement('img');
    image.src = '/api/resources/media/' + item.digest + (item.media_kind === 'emoji' ? '?kind=emoji' : '');
    image.alt = item.text || '迁入表情素材';
    image.loading = 'lazy';
    image.style.cssText = 'max-width:140px;max-height:140px;object-fit:contain';
    card.append(image);
  }
  const source = document.createElement('small');
  source.textContent = '来源：' + JSON.stringify(item.source);
  card.append(source);
  if (item.media_kind === 'emoji') {
    const usage = document.createElement('small');
    usage.textContent = `收集 ${item.collection_count} 次 · 使用 ${item.use_count} 次`;
    if (!item.has_file) usage.textContent += ' · 缺少原件';
    card.append(usage);
  }
  return card;
}
