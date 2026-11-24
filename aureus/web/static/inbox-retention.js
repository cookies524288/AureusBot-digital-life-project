'use strict';

let inboxRetentionRequest = 0;

function clearInboxRetention() {
  ++inboxRetentionRequest;
  const panel = $('inbox-retention');
  if (panel) {
    panel.replaceChildren();
    panel.classList.add('hidden');
  }
}

async function showInboxRetention(key) {
  clearInboxRetention();
  const panel = $('inbox-retention');
  if (!panel || !key || key.platform !== 'qq') return;
  const request = inboxRetentionRequest, session = state.loaded;
  const stillCurrent = () => request === inboxRetentionRequest && session === state.loaded;
  const url = `/api/inbox/history-cleanup?${inboxScope(key)}`;
  const response = await api(url);
  if (!stillCurrent() || !response.ok || !response.data.preview) return;
  const preview = response.data.preview;
  const cutoff = preview.cutoff.slice(0, 10);
  const scope = preview.conversation;
  const location = scope.kind === 'group' ? `群 ${scope.recipient_id}` : `私聊 ${scope.recipient_id}`;
  const temporary = scope.temporary_group_id ? `（临时群 ${scope.temporary_group_id}）` : '';
  const count = preview.originals + preview.timeline_only;
  const status = el('p', {role:'status', class:'muted'});
  const password = el('input', {type:'password', autocomplete:'current-password', required:'required',
    maxlength:'256', 'aria-label':'确认清理的当前网页密码'});
  const confirm = el('button', {type:'submit', class:'btn danger', text:'确认清理本地旧历史'});
  const form = el('form', {class:'row'}, password, confirm);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!stillCurrent() || !password.value || confirm.disabled) return;
    const value = password.value;
    password.value = '';
    confirm.disabled = true;
    status.textContent = '正在复核并清理…';
    const result = await api(url, 'POST', {proposal_id:preview.id, password:value});
    if (!stillCurrent()) return;
    if (!result.ok) {
      status.textContent = errorText(result);
      confirm.disabled = result.status === 409;
      return;
    }
    form.remove();
    status.textContent = '已按确认范围清理。未读、新到消息和其他会话未更改。';
    await loadInboxMessages(1);
  });
  panel.replaceChildren(
    el('h4', {text:'助手提出的旧历史清理'}),
    el('p', {text:`QQ 账号 ${scope.account_id} · ${location}${temporary} · ${cutoff} 之前（不含当天）`}),
    el('p', {text:`可删除：${preview.originals} 条本地收发原件、${preview.image_vectors} 项关联图片向量。`}),
    el('p', {text:`仅从本会话平台视图移除：${preview.timeline_only} 条旧时间线记录。时间线与迁入备份仍保留，不撤回 QQ 服务器消息。其他原件仍使用的媒体不删除。`}),
    el('p', {class:'muted', text:`会话已读水位 ${preview.read_water} · 原件水位 ${preview.inbox_water} · 旧来源水位 ${preview.timeline_water}。近期收到及只有平台时间、无法确认本地收录日期的记录不清理。`}),
    count ? form : el('p', {class:'muted', text:'没有符合范围的历史，不需要确认。'}), status,
  );
  panel.classList.remove('hidden');
}
