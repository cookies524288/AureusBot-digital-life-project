"""人设历史的只读全文与回退入口。"""

from tests.web.test_inbox_refresh import isolate_resource_forms as isolate_resource_forms
from tests.web.test_page_script import run_page_script


def test_versions_preserve_full_text_without_applying_it() -> None:
    run_page_script(
        r"""
        const prefix = '共同'.repeat(30);
        const texts = [prefix + '\n\n  结尾甲', prefix + '\n\n  结尾乙', '短版本也能完整阅读'];
        const history = texts.map((content, id) => ({
          id, content, source:'x'.repeat(1024), time:'2026-01-01'}));
        let confirmed = null;
        const writes = [];
        confirmPersonaChange = (message, action) => { confirmed = action; };
        api = async (...args) => { writes.push(args); return {ok:true}; };
        loadPersona = async () => {};
        toast = () => {};
        const card = personaLayerCard('expression', '测试层', '当前内容', history);
        const rows = card.children.filter(node => node.classList.contains('version-row'));
        assert.equal(rows.length, texts.length);
        for (const [index, row] of rows.entries()) {
          const details = row.children.find(node => node.tagName === 'DETAILS');
          assert.ok(details, '版本应提供原生全文折叠控件');
          const rollback = row.children.find(node => node.tagName === 'BUTTON');
          assert.ok(row.children.indexOf(details) < row.children.indexOf(rollback));
          const [summary, body] = details.children;
          assert.equal(summary.tagName, 'SUMMARY');
          assert.ok(summary.textContent.includes(index < 2 ? prefix : texts[index]));
          assert.equal(details.getAttribute('open'), null);
          assert.equal(body.tagName, 'P');
          assert.equal(body.textContent, texts[index]);
          summary.click(); summary.click();
        }
        assert.equal(confirmed, null);
        assert.equal(writes.length, 0);
        rows[1].children.find(node => node.tagName === 'BUTTON').click();
        assert.equal(typeof confirmed, 'function');
        assert.equal(writes.length, 0);
        await confirmed();
        assert.equal(writes.length, 1);
        assert.deepEqual(writes[0],
          ['/api/settings/rollback', 'POST', {layer:'expression', version_id:1}]);
        """
    )
