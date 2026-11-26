"""时间线和观察页共用记录卡的正文阅读回归。"""

import pytest

from tests.web.test_inbox_refresh import isolate_resource_forms as isolate_resource_forms
from tests.web.test_page_script import run_page_script


@pytest.mark.parametrize("metadata", ["{}", "{actions:[]}", "{actions:[{reason:'行动依据'}]}"])
def test_timeline_reading(metadata: str) -> None:
    """没有行动时直接读正文；有行动时仍可从详情读到原文。"""
    run_page_script(r"""
        const metadata = METADATA;
        const make = content => timelineItem({id:1, time:'2026-11-27T01:02:03Z',
          event_type:'reply_reference', actor:'me', content, metadata}, false);
        const full = '第一段\n\n第二段\n' + '字'.repeat(1024) + '完整末尾';
        const item = make(full);
        const body = item.children.find(n => n.classList.contains('tl-content'));
        if (metadata.actions?.length) {
          assert.equal(body.textContent, '本轮行动记录');
          const descendants = n => [n, ...(n.children || []).flatMap(descendants)];
          const details = item.children.find(n => n.tagName === 'DETAILS');
          assert.equal(details.children[0].tagName, 'SUMMARY');
          assert.ok(descendants(details).some(n => n.textContent === full));
          assert.ok(descendants(item).some(n => n.textContent.includes('行动依据')));
          return;
        }
        const short = make('两行\n短文');
        const shortBody = short.children.find(n => n.classList.contains('tl-content'));
        assert.equal(shortBody.textContent, '两行\n短文');
        assert.ok(!shortBody.classList.contains('clamp'));
        assert.ok(!short.children.some(n => n.tagName === 'BUTTON'));
        const toggle = item.children.find(n => n.tagName === 'BUTTON');
        assert.equal(toggle.getAttribute('aria-expanded'), 'false');
        assert.match(toggle.textContent, /展开/);
        const preview = body.textContent;
        assert.ok(!preview.includes('完整末尾'));
        toggle.click();
        assert.equal(toggle.getAttribute('aria-expanded'), 'true');
        assert.match(toggle.textContent, /收起/);
        assert.equal(body.textContent, full);
        body.click();
        assert.equal(toggle.getAttribute('aria-expanded'), 'true');
        toggle.click();
        assert.equal(body.textContent, preview);
        assert.equal(toggle.getAttribute('aria-expanded'), 'false');
    """.replace('METADATA', metadata))
