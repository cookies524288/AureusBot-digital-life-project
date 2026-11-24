'use strict';

window.createMemoryBundlePanel = () => {
  const path = '/api/resources/memory/bundles';
  const labels = {paragraphs:'段落', entities:'实体', relations:'关系', episodes:'情景',
    paragraph_entities:'段落与实体关联', paragraph_relations:'段落与关系关联',
    episode_paragraphs:'情景与段落关联'};
  const levels = {knowledge:'知识包', full:'完整包'};
  const upload = el('input', {type:'file', accept:'.amembundle'});
  const preview = el('div', {class:'stack', 'aria-live':'polite'});
  const message = el('p', {role:'status'});
  const list = el('div', {class:'stack'});
  const listStatus = el('p', {role:'status'});
  const refresh = el('button', {class:'btn small', type:'button', text:'刷新安装记录'});
  const more = el('button', {class:'btn small', type:'button', text:'加载更多', hidden:true});
  let pending, offset = null;

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
    listStatus.textContent = !append && items.length === 0 ? '没有安装记录。' : '';
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
      el('p', {text:`${item.name} · ${item.package_id} · ${item.version} · ${levels[item.content_level]} · ${item.installed ? '已安装' : '已卸载'}`}),
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
      el('p', {text:`${info.name} · ${info.package_id} · ${info.version} · ${levels[info.content_level]}`}),
      el('p', {text:Object.entries(info.counts).map(([key, count]) => `${labels[key]} ${count} 条`).join(' · ')}),
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
  return el('div', {class:'card stack'}, el('h3', {text:'MaiBot 知识包与完整包'}),
    el('p', {text:'仅支持兼容的 .amembundle 文字包。不支持的图片、向量或状态会在预检时提示；上传后须确认才写入。'}),
    el('label', {class:'stack'}, '选择 MaiBot 记忆包', upload), preview, message,
    el('h4', {text:'安装记录'}), refresh, listStatus, list, more);
};
