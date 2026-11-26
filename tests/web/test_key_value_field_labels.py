"""用空合成字段验证密钥编辑器切换控件时仍保留原生标签。"""

import pytest

from tests.web.test_inbox_refresh import isolate_resource_forms
from tests.web.test_page_script import run_page_script

pytestmark = pytest.mark.usefixtures("isolate_resource_forms")
__all__ = ["isolate_resource_forms"]


SETUP = """
let writes = 0;
fetch = async (_, {method = 'GET'} = {}) => {
  if (method !== 'GET' && method !== 'HEAD') writes++;
  return reply(200, {});
};
function descendants(node) {
  return node.children.flatMap(child => [child, ...descendants(child)]);
}
function labeledControl(title, tag) {
  const matches = descendants($('editor-body')).filter(
    node => node.getAttribute('aria-label') === title);
  assert.equal(matches.length, 1, title + '应只有一个具名控件');
  const [control] = matches;
  assert.equal(control.tagName, tag);
  const field = descendants(control.closest('.field'));
  const controls = field.filter(node => ['INPUT', 'TEXTAREA', 'SELECT'].includes(node.tagName));
  assert.equal(controls.length, 1, title + '不应留下旧控件');
  const labels = field.filter(node => node.tagName === 'LABEL');
  assert.equal(labels.length, 1, title + '应有独立可见的标签');
  const [label] = labels;
  const caption = label.children.find(node => node.classList.contains('field-label'));
  assert.ok(caption && caption.textContent === title, title + '应有独立的标签文字');
  assert.ok(descendants(label).includes(control), title + '的标签应关联当前控件');
  return control;
}
function checkValueField(title, tag) {
  const control = labeledControl(title, tag);
  assert.ok(control.value === '', '合成字段应为空');
  assert.equal(control.getAttribute('data-secret'), '');
  assert.equal(control.getAttribute('required'), '');
  assert.equal(control.getAttribute('maxlength'), '4096');
  assert.equal(control.getAttribute('autocomplete'), 'off');
  assert.equal(control.getAttribute('spellcheck'), 'false');
  assert.equal(control.classList.contains('secret-input'), tag === 'INPUT');
  if (tag === 'INPUT') assert.equal(control.getAttribute('type'), 'text');
  else assert.equal(Number(control.getAttribute('rows')), 5);
}
"""


def test_existing_other_key_opens_labeled_textarea_without_writing() -> None:
    run_page_script(SETUP + """
    applyKeysPage({entries:[{id:'sample', name:'合成条目', kind:'other'}]});
    const edit = descendants($('ky-list')).find(node => node.tagName === 'BUTTON' &&
                                                 node.getAttribute('aria-label') === '修改密钥值');
    edit.click();
    assert.equal($('editor-dialog').open, true);
    checkValueField('新密钥值', 'TEXTAREA');
    await new Promise(setImmediate);
    assert.equal(writes, 0);
    """)


def test_new_key_keeps_one_labeled_control_across_type_changes_without_writing() -> None:
    run_page_script(SETUP + """
    $('ky-add').click();
    assert.equal($('editor-dialog').open, true);
    const kind = labeledControl('类型', 'SELECT');
    assert.equal(kind.value, 'api_key');
    checkValueField('密钥原文', 'INPUT');
    for (const [value, tag] of [['other', 'TEXTAREA'], ['api_key', 'INPUT']]) {
      kind.value = value;
      for (const listener of kind.listeners.change) listener({target:kind, currentTarget:kind});
      checkValueField('密钥原文', tag);
    }
    await new Promise(setImmediate);
    assert.equal(writes, 0);
    """)
