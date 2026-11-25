'use strict';

window.createMemoryBundlePanel = () => {
  const path = '/api/resources/memory/bundles';
  const labels = {paragraphs:'段落', entities:'实体', relations:'关系', episodes:'情景',
    paragraph_entities:'段落与实体关联', paragraph_relations:'段落与关系关联',
    episode_paragraphs:'情景与段落关联', fact_claims:'事实', fact_evidence:'事实证据',
    fact_transitions:'事实状态转换', external_memory_refs:'外部引用',
    person_profile_snapshots:'人物画像', person_profile_overrides:'画像正文覆盖',
    person_profile_alias_overrides:'画像别名覆盖', image_assets:'图片资产', image_occurrences:'图片出现',
    image_observations:'图片观察', image_memory_links:'图片链接'};
  const levels = {knowledge:'知识包', full:'完整包'};
  const upload = el('input', {type:'file', accept:'.amembundle'});
  const preview = el('div', {class:'stack', 'aria-live':'polite'});
  const message = el('p', {role:'status'});
  const list = el('div', {class:'stack'});
  const listStatus = el('p', {role:'status'});
  const refresh = el('button', {class:'btn small', type:'button', text:'刷新安装记录'});
  const more = el('button', {class:'btn small', type:'button', text:'加载更多', hidden:true});
  let pending, offset = null;

  function exportPanel() {
    const choices = el('div', {class:'stack'});
    const result = el('div', {class:'stack', 'aria-live':'polite'});
    const level = el('select', {}, ...Object.entries(levels).map(([value, text]) => el('option', {value, text})));
    const images = el('input', {type:'checkbox'});
    const review = el('button', {class:'btn small', type:'button', text:'预检生成范围', disabled:true});
    let settings = 0, inputs = [];
    const reset = () => {
      settings++;
      review.disabled = !inputs.some(input => input.checked);
      result.replaceChildren();
    };
    level.addEventListener('change', reset);
    images.addEventListener('change', reset);
    async function sources() {
      inputs = [];
      reset();
      const generation = settings;
      const response = await api(path + '/sources');
      if (generation !== settings) return;
      if (!response.ok) { choices.textContent = errorText(response); return; }
      choices.replaceChildren(...response.data.items.map(item => {
        const input = el('input', {type:'checkbox', value:item.source_id});
        inputs.push(input);
        input.addEventListener('change', reset);
        return el('label', {class:'row'}, input, item.instance);
      }));
      if (!inputs.length) choices.textContent = '没有可选择的可信迁入批次。先导入或迁移资料后再生成。';
    }
    review.addEventListener('click', async () => {
      const last = ++settings;
      const payload = {source_ids:inputs.filter(input => input.checked).map(input => input.value),
        content_level:level.value, include_images:images.checked};
      review.disabled = true;
      result.textContent = '正在核对选定资料…';
      const previewed = await api(path + '/export-preview', 'POST', payload);
      if (last !== settings) return;
      review.disabled = false;
      if (!previewed.ok) { result.textContent = errorText(previewed); return; }
      const data = previewed.data;
      const download = el('button', {class:'btn small', type:'button', text:'确认生成并下载', disabled:!data.can_export,
        onclick:async () => {
          download.disabled = true;
          try {
            const response = await fetch(path + '/export', {method:'POST', credentials:'same-origin',
              headers:{'Content-Type':'application/json'}, body:JSON.stringify({...payload, preview_digest:data.preview_digest})});
            if (last !== settings) return;
            if (!response.ok) { result.textContent = errorText({status:response.status, data:await response.json()}); return; }
            const blob = await response.blob();
            if (last !== settings) return;
            const href = URL.createObjectURL(blob);
            el('a', {href, download:'aureus-memory.amembundle'}).click();
            setTimeout(() => URL.revokeObjectURL(href), 1000);
          } catch { if (last === settings) result.textContent = '下载没有完成，请重新预检。'; }
          finally { download.disabled = false; }
        }});
      result.replaceChildren(
        el('p', {text:Object.entries(data.counts).map(([key, count]) => `${labels[key] || key} ${count} 条`).join(' · ')}),
        ...Object.entries(data.excluded).map(([reason, count]) => el('p', {text:`排除 ${count} 条：${reason}`})),
        ...data.limitations.map(text => el('p', {class:'muted', text})), download);
    });
    const refreshSources = el('button', {class:'btn small', type:'button', text:'刷新可信来源', onclick:sources});
    sources();
    return el('div', {class:'stack'}, el('h4', {text:'从本库新生成'}),
      el('label', {class:'row'}, '内容层级', level),
      el('label', {class:'row'}, images, '包含获准的归档图片'), refreshSources, choices, review, result);
  }

  async function load(append = false) {
    refresh.disabled = more.disabled = true;
    const response = await api(path + '?offset=' + (append ? offset : 0));
    refresh.disabled = more.disabled = false;
    if (!response.ok) {
      listStatus.textContent = errorText(response);
      return;
    }
    const {items, next_offset: next} = response.data;
    offset = next;
    more.hidden = offset === null;
    listStatus.textContent = !append && items.length === 0 ? '没有安装记录。导入或生成一个记忆包后会显示在这里。' : '';
    if (!append) list.replaceChildren();
    list.append(...items.map(installation));
  }

  function installation(item) {
    const endpoint = path + '/' + encodeURIComponent(item.installation_id);
    const action = item.installed ? 'uninstall' : 'restore';
    const label = item.installed ? '卸载' : '恢复';
    const button = el('button', {class:'btn small', type:'button', text:label, onclick:async () => {
      if (!window.confirm(item.installed ? '卸载只停用该包独占资料，原件保留。继续？' : '恢复此包？')) return;
      button.disabled = true;
      const response = await api(endpoint + '/' + action, 'POST');
      button.disabled = false;
      if (response.ok) {
        await load();
        message.textContent = label + '完成。';
      } else message.textContent = errorText(response);
    }});
    return el('div', {class:'resource-record stack'},
      el('p', {text:`${item.name} · ${levels[item.content_level]} · ${item.installed ? '已安装' : '已卸载'}`}),
      el('div', {class:'row'}, button,
        el('a', {class:'btn small', href:endpoint + '/download', download:true, text:'下载原包'})));
  }

  function cancel() {
    pending?.abort();
    pending = null;
    preview.replaceChildren();
  }

  upload.addEventListener('change', async () => {
    cancel();
    const file = upload.files[0];
    message.textContent = '';
    if (!file) return;
    const controller = new AbortController();
    pending = controller;
    message.textContent = '正在预检 MaiBot 记忆包…';
    const response = await api(path + '/preview', 'POST', file, {}, controller.signal);
    if (pending !== controller || upload.files[0] !== file) return;
    if (!response.ok) {
      message.textContent = '未通过预检：' + errorText(response);
      return;
    }
    const info = response.data;
    const install = (mode, text) => el('button', {class:'btn small', type:'button', text, onclick:async () => {
      if (pending !== controller || upload.files[0] !== file) return;
      cancel();
      upload.disabled = true;
      message.textContent = '正在安装…';
      const result = await api(path + '?mode=' + mode, 'POST', file);
      message.textContent = result.ok
        ? (result.data.already_installed ? '此包已经安装。' : `安装成功，新增 ${result.data.added} 条原件及关联。`)
        : '安装未完成：' + errorText(result);
      upload.value = '';
      upload.disabled = false;
      if (result.ok) await load();
    }});
    message.textContent = info.status === 'new' ? '预检通过，请选择安装方式。' :
      (info.status === 'installed' ? '此包已安装。' : '此包已卸载，可从安装记录恢复。');
    preview.replaceChildren(
      el('p', {text:`${info.name} · ${levels[info.content_level]}`}),
      el('p', {text:Object.entries(info.counts).map(([key, count]) => `${labels[key] || key} ${count} 条`).join(' · ')}),
      ...(info.components || []).map(item => el('p', {text:`${item.status === 'skip' ? '跳过' : '将安装'} ${item.name}${item.reason ? '：' + item.reason : ''}`})),
      el('p', {class:'muted', text:info.restore_blocked || '当前可以恢复到空记忆库，提交时会再次检查。'}),
      el('div', {class:'row'}, info.status === 'new' && install('merge', '确认合并'),
        info.status === 'new' && !info.restore_blocked && install('restore', '确认空库恢复'),
        el('button', {class:'btn small', type:'button', text:'取消', onclick:() => {
          cancel(); upload.value = ''; message.textContent = '已取消，未安装。';
        }})));
  });
  refresh.addEventListener('click', () => load());
  more.addEventListener('click', () => load(true));
  load();
  return el('div', {class:'card stack memory-transfer-card'}, el('h4', {text:'MaiBot 知识包与完整包'}),
    el('p', {text:'支持兼容的 .amembundle；预检会列出将安装和跳过的成员，确认后才写入。'}),
    exportPanel(),
    el('label', {class:'stack'}, '选择 MaiBot 记忆包', upload), preview, message,
    el('h4', {text:'安装记录'}), refresh, listStatus, list, more);
};
