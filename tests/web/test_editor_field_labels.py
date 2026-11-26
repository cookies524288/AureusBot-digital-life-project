"""用合成字段检查设置和人设编辑器的原生标签关联。"""

import pytest

from tests.web.test_inbox_refresh import isolate_resource_forms
from tests.web.test_page_script import run_page_script

pytestmark = pytest.mark.usefixtures("isolate_resource_forms")
__all__ = ["isolate_resource_forms"]


def test_editor_labels_follow_fields_after_caller_sets_id() -> None:
    run_page_script("""
    const name = editorField('名称', '示例', {required: true});
    const kind = editorField('类型', 'b', {values: ['a', 'b']});
    el('div', {}, name.node, kind.node);
    for (const [field, title, id, tag] of [[name, '名称', 'demo-name', 'INPUT'],
                                            [kind, '类型', 'demo-kind', 'SELECT']]) {
      field.input.id = id;
      const label = field.node.children.find(node => node.tagName === 'LABEL');
      const [caption, control] = label.children;
      assert.equal(caption.tagName, 'SPAN');
      assert.ok(caption.classList.contains('field-label'));
      assert.equal(caption.textContent, title);
      assert.equal(control, field.input);
      assert.equal(field.input.getAttribute('aria-label'), title);
      assert.equal(field.input.tagName, tag);
      assert.ok(label.children.includes(field.input) || label.getAttribute('for') === id,
                'visible label must reference its own control');
    }
    assert.equal(name.input.value, '示例');
    assert.equal(kind.input.value, 'b');
    """)


def test_layer_headings_label_their_own_textarea_and_preserve_save() -> None:
    run_page_script("""
    const cards = ['one', 'two'].map(key => personaLayerCard(key, '层' + key, key, []));
    const buttons = [];
    for (const [index, key] of ['one', 'two'].entries()) {
      const [head, area, actions] = cards[index].children;
      const heading = head.children[0].children[0];
      const label = heading.children.find(node => node.tagName === 'LABEL');
      assert.ok(label, 'layer heading must label the textarea');
      assert.equal(label.textContent, '层' + key);
      assert.equal(area.getAttribute('id'), 'ps-' + key);
      assert.equal(label.getAttribute('for'), area.getAttribute('id'));
      buttons.push(actions.children[0]);
      assert.equal(buttons[index].disabled, true);
    }
    let saved;
    confirmPersonaChange = (_, accept) => accept();
    savePersona = payload => { saved = payload; };
    const area = cards[1].children[1];
    area.value = '更新';
    area.listeners.input[0]();
    assert.equal(buttons[0].disabled, true);
    assert.equal(buttons[1].disabled, false);
    buttons[1].click();
    assert.deepEqual(saved.text, {two: '更新'});
    """)
