"""资源页验收：旧人物按账号显示，记忆正文不显示来源序号（D130）。"""

import json
from collections.abc import Iterator
from typing import Any

import pytest

from tests.web import test_resource_browser as harness
from tests.web.test_resource_browser import Browser
from tests.web.test_resource_browser import browser as browser_fixture  # noqa: F401

STAMP = harness.STAMP
PREFIX = "statement:" + "a" * 32 + ":"
ORIGINAL_PREFIX = "paragraph:" + "b" * 32 + ":"


@pytest.fixture(scope="module")
def browser(request: pytest.FixtureRequest) -> Browser:
    return request.getfixturevalue("browser_fixture")


@pytest.fixture
def prefixed_items(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    original = harness._items

    def items(path: str, kind: str) -> list[dict[str, object]]:
        result = original(path, kind)
        if path.endswith("/profiles"):
            result.insert(
                0,
                {
                    "entry_type": "person",
                    "person_id": 999,
                    "name": "",
                    "first_seen": STAMP,
                    "accounts": [["qq", "12345"]],
                },
            )
        elif path.endswith("/memory"):
            result.insert(
                0,
                {
                    "entry_id": "900",
                    "kind": "live",
                    "source": {
                        "table": "paragraphs",
                        "source_key": "900",
                        "instance": "browser-fixture",
                    },
                    "labels": ["paragraphs"],
                    "text": PREFIX + "合成前缀正文",
                    "original_text": ORIGINAL_PREFIX + "合成前缀原件",
                    "is_enabled": True,
                    "is_recallable": True,
                    "recorded_at": STAMP,
                    "evidence": [],
                },
            )
        elif path.endswith("/flow"):
            result.insert(
                0,
                {
                    "summary": PREFIX + "合成前缀事件",
                    "event_type": "profile_created",
                    "recorded_at": STAMP,
                    "conversations": [],
                },
            )
        return result

    monkeypatch.setattr(harness, "_items", items)
    yield


@pytest.fixture
def memory_patches(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    patches: list[dict[str, Any]] = []
    original = harness.ResourceFixture.do_PATCH

    def do_patch(self: harness.ResourceFixture) -> None:
        if self.path == "/api/resources/memory":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            patches.append(body)
            self._send_json({"ok": True})
            return
        original(self)

    monkeypatch.setattr(harness.ResourceFixture, "do_PATCH", do_patch)
    return patches


def test_an_old_person_without_a_name_shows_the_account(
    browser: Browser, prefixed_items: None
) -> None:
    browser.open("memory", 390, 740)
    harness._run(
        browser,
        r"""
      await waitFor(() => find('[data-memory-query="profiles"]'), '长期记忆页已打开');
      click('[data-memory-query="profiles"]');
      const search = find('#memory-query-profiles input[type="search"]');
      search.value = '999';
      search.form.requestSubmit();
      await waitFor(() => text('QQ 12345'), '旧空名字显示账号');
      check(!text('未命名人物'), '列表不再出现未命名人物');
      press('QQ 12345');
      await waitFor(() => document.querySelector('#editor-dialog').open, '打开人物详情');
      const dialog = document.querySelector('#editor-dialog');
      check(dialog.innerText.includes('称呼：QQ 12345'), '详情称呼显示账号');
      check(!dialog.innerText.includes('未命名人物'), '详情不再出现未命名人物');
      document.querySelector('#editor-close').click();
      const card = find('#profiles-result .memory-card');
      const light = getComputedStyle(card).backgroundColor;
      document.documentElement.dataset.theme = 'dark';
      await new Promise(resolve => requestAnimationFrame(resolve));
      check(getComputedStyle(card).backgroundColor !== light, '深色主题改变卡片颜色');
      const activePage = document.querySelector('.page.active');
      check(activePage.scrollWidth <= activePage.clientWidth, '窄屏不横向溢出');
      delete document.documentElement.dataset.theme;
    """,
    )


def test_memory_prefix_is_hidden_and_batch_correction_submits_visible_text(
    browser: Browser, prefixed_items: None, memory_patches: list[dict[str, Any]]
) -> None:
    browser.open("memory", 390, 740)
    harness._run(
        browser,
        r"""
      await waitFor(() => find('#memory-search input[type="search"]'), '长期记忆页已打开');
      const search = find('#memory-search input[type="search"]');
      search.value = '前缀';
      search.form.requestSubmit();
      await waitFor(() => text('合成前缀正文'), '前缀记忆卡片');
      check(!text('statement:') && !text('paragraph:'), '卡片不显示来源序号');
      find('#memory-result .memory-card-title').click();
      await waitFor(() => document.querySelector('#editor-dialog').open, '打开记忆详情');
      const dialog = document.querySelector('#editor-dialog');
      check(dialog.innerText.includes('合成前缀正文'), '详情显示正文');
      check(dialog.innerText.includes('合成前缀原件'), '详情显示原件');
      check(!dialog.innerText.includes('statement:') && !dialog.innerText.includes('paragraph:'),
        '详情不显示来源序号');
      document.querySelector('#editor-close').click();
      click('#memory-result input[type=checkbox]');
      click('#memory-correct');
      await waitFor(() => document.querySelector('#editor-dialog').open, '打开修正表单');
      const input = document.querySelector('#editor-dialog textarea');
      check(input.value === '合成前缀正文', '修正框预填去前缀正文');
      input.value = '合成前缀正文（已改）';
      document.querySelector('#editor-dialog form').requestSubmit();
      await waitFor(() => !document.querySelector('#editor-dialog').open, '保存后关闭');
    """,
    )
    assert memory_patches == [
        {"kind": "live", "entry_id": "900", "text": "合成前缀正文（已改）"}
    ]


def test_memory_flow_hides_the_prefix(browser: Browser, prefixed_items: None) -> None:
    browser.open("memory", 390, 740)
    harness._run(
        browser,
        r"""
      await waitFor(() => find('[data-memory-tab="flow"]'), '长期记忆页已打开');
      click('[data-memory-tab="flow"]');
      await waitFor(() => text('合成前缀事件'), '记忆流事件');
      check(!text('statement:') && !text('paragraph:'), '记忆流不显示来源序号');
      find('#memory-flow-result button').click();
      await waitFor(() => document.querySelector('#editor-dialog').open, '打开记忆流详情');
      const dialog = document.querySelector('#editor-dialog');
      check(dialog.innerText.includes('合成前缀事件'), '详情显示事件摘要');
      check(!dialog.innerText.includes('statement:') && !dialog.innerText.includes('paragraph:'),
        '记忆流详情不显示来源序号');
    """,
    )
