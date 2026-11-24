"""用独立 Chrome 实例和编造的 API 数据验收资源页的浏览交互。"""

import base64
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from functools import partial
from hashlib import sha256
from http.client import HTTPConnection
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image
from websockets.sync.client import ClientConnection, connect

STATIC = Path(__file__).resolve().parents[2] / "aureus" / "web" / "static"
STAMP = "2026-11-23T00:00:00Z"
TOTAL = 320
MEMORY_BATCH_TOTAL = 15
REVIEW_ENDPOINT = "/api/resources/learning/reviews"
SCOPE_ENDPOINT = "/api/resources/learning/scope"


def _items(path: str, kind: str) -> list[dict[str, object]]:
    labels = {"jargon": "黑话", "expression": "表达", "emoji": "表情", "image_assets": "图片"}
    return [_item(path, kind, labels.get(kind, "记忆"), number) for number in range(TOTAL)]


def _item(path: str, kind: str, label: str, number: int) -> dict[str, object]:
    text = f"合成{label}{number:03}"
    source = {"table": "paragraphs", "source_key": str(number), "instance": "browser-fixture"}
    if path.endswith("/learning"):
        return {
            "entry_key": f"{kind}-{number}",
            "text": text,
            "detail": "合成释义",
            "example": "例句",
            "count": 2,
            "speakers": [],
            "source_refs": [],
            "evidence_ids": [],
            "is_enabled": True,
        }
    if path.endswith("/media"):
        digest = sha256(f"browser-emoji-{number}".encode()).hexdigest()
        return {
            "text": text,
            "description": text,
            "tags": [],
            "source": source,
            "media_kind": kind,
            "digest": digest,
            "is_banned": False,
            "has_file": True,
            "is_registered": True,
            "media_type": "image/gif" if number % 16 == 2 else "image/png",
            "use_count": number,
            "collection_count": 2,
        }
    if path.endswith("/profiles"):
        return {"record": {"name": f"合成人物{number:03}", **source}, "person_id": number}
    if path.endswith("/flow"):
        return {
            "summary": f"合成事件{number:03}",
            "event_type": "profile_created",
            "recorded_at": STAMP,
            "conversations": ["合成会话"],
        }
    if path.endswith("/pictures"):
        archived = kind == "archived" or (not kind and number % 2 == 0)
        return {
            "kind": "archive" if archived else "inbox",
            "id": TOTAL - number,
            "text": f"合成{'图片' if archived else '收件箱'}{number:03}",
            "conversation": "合成会话",
            "timestamp": STAMP,
            "source": source if archived else None,
            "digest": f"{number:064x}" if archived else None,
            "attachment": f"sample-{number}.png",
        }
    if path.endswith("/images"):
        return {
            "id": TOTAL - number,
            "conversation": f"合成收件箱{number:03}",
            "timestamp": STAMP,
            "attachment": f"sample-{number}.png",
        }
    return {
        "entry_id": str(number),
        "source": source,
        "text": text,
        "original_text": "合成原件",
        "is_enabled": True,
        "is_recallable": True,
        "recorded_at": STAMP,
        "evidence": [],
    }


class ResourceServer(ThreadingHTTPServer):
    emoji_uploads: dict[str, dict[str, object]]
    removed_memories: set[tuple[str, int]]
    expression_reviews: dict[str, list[dict[str, str]]]
    recall_rating: str | None


class ResourceFixture(SimpleHTTPRequestHandler):
    @property
    def emoji_uploads(self) -> dict[str, dict[str, object]]:
        return cast(ResourceServer, self.server).emoji_uploads

    @property
    def removed_memories(self) -> set[tuple[str, int]]:
        return cast(ResourceServer, self.server).removed_memories

    @property
    def expression_reviews(self) -> dict[str, list[dict[str, str]]]:
        return cast(ResourceServer, self.server).expression_reviews

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path.startswith("/assets/"):
            self.path = url.path.removeprefix("/assets")
            super().do_GET()
        elif url.path == "/":
            super().do_GET()
        elif url.path == "/api/status":
            self._send_json({"must_change_password": False})
        elif url.path == "/api/organs/vlm/prompt-preview":
            self._send_json(
                {
                    "scenes": [
                        {
                            "id": "image_description",
                            "label": "普通图片描述",
                            "note": "实际请求会附上来信图片；预览不读取或发送图片。",
                            "messages": [
                                {"role": "system", "content": "合成系统提示"},
                                {
                                    "role": "user",
                                    "content": [
                                        {"type": "text", "text": "合成图片描述指令\n只返回文字"},
                                        {"type": "image", "label": "〔来信图片〕"},
                                    ],
                                },
                            ],
                        },
                        {
                            "id": "emoji_gif",
                            "label": "GIF QQ 表情识别",
                            "note": "预览不读取或发送图片。",
                            "messages": [
                                {"role": "system", "content": "合成系统提示"},
                                {
                                    "role": "user",
                                    "content": [
                                        {"type": "text", "text": "输入类型：GIF\n返回表情标签"},
                                        {"type": "image", "label": "〔GIF 表情拼图〕"},
                                    ],
                                },
                            ],
                        },
                    ]
                }
            )
        elif url.path == "/api/timeline":
            records = [
                {"id": 41, "content": "没有依据的旧回复"},
                {"id": 42, "content": "可评价的合成回复", "recall_feedback_available": True},
            ]
            self._send_json(
                {
                    "records": [
                        {
                            "time": STAMP,
                            "actor": "me",
                            "event_type": "message",
                            "visibility": "user_only",
                            "metadata": {},
                            **record,
                        }
                        for record in records
                    ],
                    "context": {"boundary_id": None},
                }
            )
        elif url.path == "/api/timeline/42/recall-feedback":
            rating = cast(ResourceServer, self.server).recall_rating
            data = [{"key": "a" * 64, "label": "时间线记录 #40", "rating": rating}]
            self._send("application/json; charset=utf-8", json.dumps(data).encode())
        elif url.path == "/ws":
            self.send_error(404)
        elif url.path.startswith("/api/resources/media/"):
            asset = b'<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"/>'
            self._send("image/svg+xml", asset)
        elif url.path == REVIEW_ENDPOINT:
            entry_key = parse_qs(url.query)["entry_key"][0]
            self._send_json({"items": self.expression_reviews.get(entry_key, [])})
        elif url.path == SCOPE_ENDPOINT:
            self._send_json(
                {
                    "items": [
                        {"key": "group-10", "label": "同名群 · 群聊 10"},
                        {"key": "group-20", "label": "同名群 · 群聊 20"},
                        {"key": "private-10", "label": "合成私聊 · 私聊 10"},
                    ]
                }
            )
        elif url.path == "/api/resources/memory/inspection":
            self._send_json(
                {
                    "coverage": "原件缺失未检查；零异常不代表全库完整。",
                    "checked": 3,
                    "checks": [
                        {"label": "维护停用", "count": 1, "detail": "不需要重建索引。"},
                        {"label": "已移除", "count": 0, "detail": "仍保留原件。"},
                        {"label": "可见标签不一致", "count": 1, "detail": "本次不重建索引。"},
                    ],
                }
            )
        elif url.path.startswith("/api/resources/"):
            self._send_page(url.path, parse_qs(url.query))
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path != "/api/resources/emoji":
            self.send_error(404)
            return
        data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        digest = sha256(data).hexdigest()
        uploads = self.emoji_uploads
        created = digest not in uploads
        uploads.setdefault(
            digest,
            {
                "text": "",
                "description": "",
                "tags": [],
                "source": None,
                "media_kind": "emoji",
                "digest": digest,
                "is_banned": False,
                "has_file": True,
                "is_registered": False,
                "use_count": 0,
                "collection_count": 0,
            },
        )
        self._send_json({"digest": digest, "created": created})

    def do_PATCH(self) -> None:
        if self.path == "/api/timeline/42/recall-feedback":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if body["evidence_key"] != "a" * 64:
                self.send_error(404)
                return
            cast(ResourceServer, self.server).recall_rating = body["rating"]
            self._send_json({"rating": body["rating"]})
            return
        if self.path == REVIEW_ENDPOINT:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            for key in payload["entry_keys"]:
                self.expression_reviews.setdefault(key, []).insert(
                    0,
                    {
                        "status": payload["status"],
                        "reason": payload["reason"],
                        "recorded_at": STAMP,
                    },
                )
            self._send_json({"changed": len(payload["entry_keys"])})
            return
        if self.path == "/api/resources/memory/batch":
            batch = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            targets = {(item["kind"], item["entry_id"]) for item in batch["targets"]}
            if batch["removed"]:
                self.removed_memories.update(targets)
            else:
                self.removed_memories.difference_update(targets)
            self._send_json({"changed": len(targets)})
            return
        prefix = "/api/resources/emoji/"
        if not self.path.startswith(prefix):
            self.send_error(404)
            return
        digest = self.path.removeprefix(prefix)
        item = self.emoji_uploads.get(digest)
        if item is None:
            self.send_error(404)
            return
        item.update(json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0")))))
        if "description" in item:
            item["text"] = item["description"]
        self._send_json({"ok": True})

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send(self, content_type: str, data: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, data: dict[str, object]) -> None:
        self._send("application/json; charset=utf-8", json.dumps(data).encode())

    def _send_page(self, path: str, params: dict[str, list[str]]) -> None:
        kind = params.get("kind", params.get("availability", [""]))[0]
        query = params.get("q", [""])[0]
        offset = int(params.get("offset", ["0"])[0])
        limit = int(params.get("limit", ["50"])[0])
        results = [item for item in _items(path, kind) if query in str(item)]
        counts = {"paragraphs": len(results)}
        if path.endswith("/learning") and (scope := params.get("conversation_key")):
            targets = {"group-10": range(24), "group-20": range(23, 25)}
            keys = {f"{kind}-{index}" for index in targets.get(scope[0], range(0))}
            results = [item for item in results if item["entry_key"] in keys]
        if path.endswith("/learning") and kind == "expression":
            for item in results:
                history = self.expression_reviews.get(str(item["entry_key"]), [])
                item["review_status"] = history[0]["status"] if history else "pending"
            if review := params.get("review"):
                results = [item for item in results if item["review_status"] == review[0]]
        if path.endswith("/memory") and query == "合成批量":
            results = self._batch_memories(params)
        if path.endswith("/media") and kind == "emoji":
            uploads = [item for item in self.emoji_uploads.values() if query in str(item)]
            results = uploads + results
            all_uploads = list(self.emoji_uploads.values())
            counts = {
                "total": TOTAL + len(all_uploads),
                "registered": TOTAL
                + sum(
                    bool(item["is_registered"] and not item["is_banned"]) for item in all_uploads
                ),
                "unregistered": sum(
                    bool(not item["is_registered"] and not item["is_banned"])
                    for item in all_uploads
                ),
                "banned": sum(bool(item["is_banned"]) for item in all_uploads),
                "removed": 0,
            }
            status = params.get("status", [""])[0]
            if status == "banned":
                results = [item for item in results if item["is_banned"]]
            elif status == "registered":
                results = [
                    item for item in results if item["is_registered"] and not item["is_banned"]
                ]
            elif status == "unregistered":
                results = [
                    item for item in results if not item["is_registered"] and not item["is_banned"]
                ]
            if image_format := params.get("format"):
                results = [
                    item for item in results if item.get("media_type") == f"image/{image_format[0]}"
                ]
            if params.get("sort") == ["most_used"]:
                results.sort(key=lambda item: int(str(item["use_count"])), reverse=True)
        if "before" in params:
            offset = TOTAL - int(params["before"][0]) + 1
        next_offset = offset + limit if offset + limit < len(results) else None
        next_before = None if next_offset is None else results[next_offset - 1].get("id")
        self._send_json(
            {
                "enabled": True,
                "items": results[offset : offset + limit],
                "total": len(results),
                "next_offset": None if path.endswith("/images") else next_offset,
                "next_before": next_before,
                "counts": counts,
                "conversations": ["合成会话"],
                "start": STAMP,
                "end": STAMP,
            }
        )

    def _batch_memories(self, params: dict[str, list[str]]) -> list[dict[str, object]]:
        removed = params.get("removed") == ["true"]
        enabled_only = params.get("recall") == ["enabled"]
        results = []
        for number in range(MEMORY_BATCH_TOTAL):
            kind = "history" if number % 2 else "live"
            entry_id = number // 2 + 1
            enabled = number != MEMORY_BATCH_TOTAL - 1
            if ((kind, entry_id) in self.removed_memories) != removed or (
                enabled_only and not enabled
            ):
                continue
            results.append(
                {
                    **_item("/memory", kind, "批量", number),
                    "kind": kind,
                    "entry_id": entry_id,
                    "is_removed": removed,
                    "is_enabled": enabled,
                    "is_recallable": enabled and not removed,
                }
            )
        return results


class Browser:
    def __init__(self, socket: ClientConnection, origin: str) -> None:
        self.socket = socket
        self.origin = origin
        self.serial = 0

    def command(self, method: str, **params: object) -> dict:
        self.serial += 1
        self.socket.send(json.dumps({"id": self.serial, "method": method, "params": params}))
        while True:
            result = json.loads(self.socket.recv(timeout=15))
            if result.get("id") == self.serial:
                assert "error" not in result, result
                return result["result"]

    def evaluate(self, expression: str) -> object:
        result = self.command(
            "Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True
        )
        assert "exceptionDetails" not in result, result.get("exceptionDetails")
        return result["result"].get("value")

    def open(self, page: str, width: int, height: int) -> None:
        self.command("Page.enable")
        self.resize(width, height)
        self.command(
            "Emulation.setEmulatedMedia",
            features=[
                {"name": "prefers-reduced-motion", "value": "reduce"},
            ],
        )
        self.command("Page.navigate", url=f"{self.origin}/?run={self.serial}#{page}")

    def resize(self, width: int, height: int) -> None:
        self.command(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=height,
            deviceScaleFactor=1,
            mobile=False,
        )


def _chrome() -> str:
    for executable in ("google-chrome", "chromium", "chrome", "msedge"):
        if path := shutil.which(executable):
            return path
    for env, filename in (
        ("ProgramFiles", "Google/Chrome/Application/chrome.exe"),
        ("ProgramFiles(x86)", "Microsoft/Edge/Application/msedge.exe"),
    ):
        if parent := os.environ.get(env):
            path = Path(parent) / filename
            if path.is_file():
                return str(path)
    pytest.skip("资源页布局验收需要 Chrome、Chromium 或 Edge")


def _debug_socket(profile: Path) -> str:
    port_file = profile / "DevToolsActivePort"
    deadline = monotonic() + 10
    while not port_file.exists():
        assert monotonic() < deadline, "独立浏览器未启动调试端口"
        sleep(0.05)
    port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/json/list")
        return next(
            tab["webSocketDebuggerUrl"]
            for tab in json.load(connection.getresponse())
            if tab["type"] == "page"
        )
    finally:
        connection.close()


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with TemporaryDirectory(prefix="aureus-resource-chrome-") as directory:
        yield from _open_browser(Path(directory))


def _open_browser(profile: Path) -> Iterator[Browser]:
    chrome = _chrome()
    server = ResourceServer(("127.0.0.1", 0), partial(ResourceFixture, directory=STATIC))
    server.emoji_uploads = {}
    server.removed_memories = set()
    server.expression_reviews = {}
    server.recall_rating = None
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    process = subprocess.Popen(
        [
            chrome,
            "--headless",
            "--disable-background-networking",
            "--disable-component-update",
            "--no-first-run",
            "--no-proxy-server",
            "--remote-debugging-port=0",
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with connect(_debug_socket(profile), proxy=None) as socket:
            yield Browser(socket, f"http://127.0.0.1:{server.server_port}")
    finally:
        process.terminate()
        process.wait(timeout=10)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


HARNESS = """
async (run) => {
  const root = () => document.querySelector('.page.active');
  const visible = node => Boolean(node?.getClientRects().length);
  const nodes = selector => [...(root()?.querySelectorAll(selector) ?? [])].filter(visible);
  const find = selector => nodes(selector)[0];
  const button = label => nodes('button').find(node => node.textContent.trim() === label);
  const text = value => root()?.innerText.includes(value);
  const check = (condition, message) => { if (!condition) throw Error(message); };
  const waitFor = async (predicate, message) => {
    const until = performance.now() + 3500;
    while (performance.now() < until) {
      if (predicate()) return;
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    throw Error(message + '\\n' + (root()?.innerText ?? document.body?.innerText.slice(0, 1000)));
  };
  const click = selector => find(selector).click();
  const press = label => button(label).click();
  const page = () => find('.resource-pagination span')?.textContent.trim();
  return run({root, find, visible, button, text, check, waitFor, click, press, page});
}
"""


def _run(browser: Browser, script: str) -> None:
    browser.evaluate(
        f"({HARNESS})(async ({{root, find, visible, button, text, check, "
        f"waitFor, click, press, page}}) => {{{script}}})"
    )


@pytest.mark.parametrize(("width", "height"), [(1280, 900), (390, 740)])
def test_learning_pagination_and_category_switch(browser: Browser, width: int, height: int) -> None:
    browser.open("learning", width, height)
    _run(
        browser,
        r"""
      await waitFor(() => text('合成黑话000') && button('下一页'), '黑话首页与分页按钮');
      const expected = Math.ceil(320 / find('#learning-jargon-result').childElementCount);
      check(page() === `第 1 / ${expected} 页`, '总页数按当前视口容量计算');
      check(button('上一页').disabled && !button('下一页').disabled, '黑话首页的按钮状态');
      const footer = button('下一页').getBoundingClientRect();
      check(footer.bottom <= innerHeight && footer.right <= innerWidth, '窄屏时页脚在可视区');
      press('下一页');
      await waitFor(() => page() === `第 2 / ${expected} 页` && text('合成黑话') &&
        !text('合成黑话000'), '翻页替换而不是追加');
      press('上一页');
      await waitFor(() => text('合成黑话000'), '上一页恢复首页');
      click('[data-learning-tab="expression"]');
      await waitFor(() => text('合成表达000') && !text('合成黑话000'), '切换到表达结果');
      check(button('上一页').disabled, '表达不沿用黑话的页数');
      const search = find('input[type="search"]');
      search.value = '合成表达001';
      search.form.requestSubmit();
      await waitFor(() => text('合成表达001') && !text('合成表达000'), '表达筛选只显示命中项');
      check(page() === '第 1 / 1 页', '筛选后重新计算总页数');
      check(button('上一页').disabled && button('下一页').disabled, '末页禁止继续翻页');
      search.value = '无匹配内容';
      search.form.requestSubmit();
      await waitFor(() => page() === '第 0 / 0 页', '空列表没有可翻的页');
      check(button('上一页').disabled && button('下一页').disabled, '空列表禁用翻页');
      click('[data-learning-tab="jargon"]');
      await waitFor(() => text('合成黑话000'), '黑话不沿用表达查询');
      check(!find('input[type="search"]').value, '黑话搜索栏独立');
    """,
    )


def test_learning_conversation_filter_replaces_pages_and_ignores_stale_responses(
    browser: Browser,
) -> None:
    browser.open("learning", 390, 740)
    _run(
        browser,
        """
      const scope = () => find('select[name="conversation_key"]');
      const result = () => find('#learning-jargon-result');
      function select(key) {
        scope().value = key;
        scope().dispatchEvent(new Event('change', {bubbles:true}));
      }
      await waitFor(() => scope() && !scope().disabled && button('合成黑话000'), '会话就绪');
      const capacity = result().childElementCount;
      const allPages = Math.ceil(320 / capacity);
      const groupPages = Math.ceil(24 / capacity);
      check(scope().value === '' && scope().selectedOptions[0].textContent === '全部资料',
        '默认全部');
      check([...scope().options].slice(1, 3).map(option => option.textContent).join('|') ===
        '同名群 · 群聊 10|同名群 · 群聊 20', '同名群可区分');
      check(text('不改变实际召回范围'), '说明浏览与召回的区别');
      press('下一页');
      await waitFor(() => page() === `第 2 / ${allPages} 页`, '先翻到全部资料第二页');
      select('group-10');
      await waitFor(() => page() === `第 1 / ${groupPages} 页` && button('合成黑话000'),
        '群筛选回首页');
      const nativeFetch = window.fetch;
      let delayed, finished = false;
      window.fetch = (url, options) => {
        const parsed = new URL(url, location.href);
        if (parsed.pathname === '/api/resources/learning' &&
            parsed.searchParams.get('conversation_key') === 'group-20') {
          return new Promise(resolve => {
            delayed = () => nativeFetch(url, options).then(response => {
              resolve(response); finished = true;
            });
          });
        }
        return nativeFetch(url, options);
      };
      try {
        select('group-20');
        await waitFor(() => delayed, '拦住旧会话请求');
        select('private-10');
        await waitFor(() => page() === '第 0 / 0 页' &&
          text('该会话没有符合条件的资料'), '空结果');
        delayed();
        await waitFor(() => finished, '旧请求返回');
        await new Promise(resolve => setTimeout(resolve, 150));
        check(page() === '第 0 / 0 页' && !button('合成黑话024'), '旧请求不能覆盖私聊选择');
      } finally { window.fetch = nativeFetch; }
      select('group-20');
      await waitFor(() => button('合成黑话023') && button('合成黑话024'), '另一个同名群可独立筛选');
      select('');
      await waitFor(() => page() === `第 1 / ${allPages} 页` && button('合成黑话000'),
        '清空恢复原首页');
      click('[data-learning-tab="expression"]');
      await waitFor(() => scope() && !scope().disabled && text('合成表达000'), '表达会话就绪');
      check(scope().value === '', '表达默认全部');
      select('group-20');
      await waitFor(() => page() === '第 1 / 1 页' && text('合成表达024') &&
        !text('合成表达000'), '表达筛选');
    """,
    )


@pytest.mark.parametrize("outcome", ["failure", "stale"])
def test_learning_scope_reload_disables_unconfirmed_choices(browser: Browser, outcome: str) -> None:
    browser.open("learning", 390, 740)
    _run(
        browser,
        """
      const scope = () => find('select[name="conversation_key"]');
      const hint = () => find('#learning-scope-hint-jargon');
      const select = key => {
        scope().value = key;
        scope().dispatchEvent(new Event('change', {bubbles:true}));
      };
      await waitFor(() => scope() && !scope().disabled && button('合成黑话000'), '首次会话就绪');
      select('group-20');
      await waitFor(() => button('合成黑话024') && !button('合成黑话000'), '首次会话筛选');
      const nativeFetch = window.fetch;
      const pending = [];
      const queries = [];
      window.fetch = (url, options) => {
        const parsed = new URL(url, location.href);
        if (parsed.pathname === '/api/resources/learning/scope') {
          return new Promise(resolve => pending.push(resolve));
        }
        if (parsed.pathname === '/api/resources/learning' &&
            parsed.searchParams.get('kind') === 'jargon') {
          queries.push(parsed.searchParams.get('conversation_key'));
        }
        return nativeFetch(url, options);
      };
      try {
        click('[data-learning-tab="expression"]');
        await waitFor(() => pending.length === 1, '表达页请求已拦截');
        click('[data-learning-tab="jargon"]');
        await waitFor(() => pending.length === 2, '黑话再次确认会话');
        check(scope().disabled, '第二次读取完成前旧会话选项不可操作');
        check(scope().value === '' && scope().options.length === 1, '只保留全部资料');
        await waitFor(() => button('合成黑话000') && !button('合成黑话024'), '仍可浏览全部');
        check(queries.length > 0 && queries.every(key => !key), '不查询未经确认的旧会话');
        if (OUTCOME === 'failure') {
          pending[1](new Response('{}', {status:503}));
          await waitFor(() => hint().textContent.includes('会话列表读取失败'), '失败有明确提示');
          check(scope().disabled && scope().options.length === 1, '失败后旧选项不可用');
          press('下一页');
          await waitFor(() => page().startsWith('第 2 /'), '失败不妨碍全部资料翻页');
        } else {
          click('[data-learning-tab="expression"]');
          await waitFor(() => pending.length === 3, '再次离开');
          click('[data-learning-tab="jargon"]');
          await waitFor(() => pending.length === 4, '新一轮确认');
          pending[3](new Response(JSON.stringify({items:[
            {key:'group-10', label:'最新群 · 群聊 10'}
          ]})));
          await waitFor(() => !scope().disabled && scope().options.length === 2, '新列表生效');
          select('group-10');
          await waitFor(() => button('合成黑话000'), '新选择的结果');
          const currentPage = page();
          pending[1](new Response(JSON.stringify({items:[
            {key:'group-20', label:'过期群 · 群聊 20'}
          ]})));
          await new Promise(resolve => setTimeout(resolve, 150));
          check(scope().value === 'group-10' && scope().options[1].textContent.includes('最新群'),
            '迟到会话列表不能覆盖新选项');
          check(page() === currentPage && !button('合成黑话024'), '迟到列表不能覆盖新结果');
        }
      } finally {
        window.fetch = nativeFetch;
        for (const resolve of pending) resolve(new Response('{"items":[]}'));
      }
    """.replace("OUTCOME", json.dumps(outcome)),
    )


def test_expression_quick_queue_filters_and_preserves_review_history(browser: Browser) -> None:
    browser.open("learning", 1280, 900)
    _run(
        browser,
        """
      await waitFor(() => text('合成黑话000'), '黑话页已加载');
      check(!button('快速审核'), '黑话没有审核入口');
      click('[data-learning-tab="expression"]');
      await waitFor(() => button('快速审核') && !button('快速审核').disabled, '表达队列就绪');
      press('快速审核');
      const filter = find('select[name="review"]');
      const dialog = document.querySelector('#editor-dialog');
      function submit(status, reason) {
        const form = dialog.querySelector('form');
        const select = form.querySelector('select');
        select.value = status;
        select.dispatchEvent(new Event('change'));
        form.querySelector('textarea').value = reason;
        form.requestSubmit();
      }
      function filterBy(status) {
        filter.value = status;
        filter.dispatchEvent(new Event('change', {bubbles:true}));
      }
      check(filter.value === 'pending', '快速审核进入待审队列');
      await waitFor(() => button('合成表达000 · 待审'), '待审记录可操作');
      press('合成表达000 · 待审');
      submit('rejected', '');
      check(dialog.open && dialog.querySelector('textarea').required, '拒绝理由必填');
      submit('rejected', '合成拒绝理由');
      await waitFor(() => !dialog.open && button('合成表达001 · 待审') &&
        !button('合成表达000 · 待审'), '拒绝后退出待审队列');
      click('input[aria-label="选择 合成表达001"]');
      click('input[aria-label="选择 合成表达002"]');
      press('审核所选');
      check(dialog.innerText.includes('审核 2 条所选表达'), '批量审核只操作选中记录');
      submit('approved', '合成通过理由');
      await waitFor(() => !dialog.open && button('合成表达003 · 待审') &&
        !button('合成表达001 · 待审'), '批量通过后刷新队列');
      filterBy('approved');
      await waitFor(() => button('合成表达001 · 通过') && button('合成表达002 · 通过'), '通过筛选');
      check(find('#learning-expression-result').childElementCount === 2, '只批准了两条');
      filterBy('rejected');
      await waitFor(() => button('合成表达000 · 拒绝'), '拒绝项仍保留');
      press('合成表达000 · 拒绝');
      dialog.querySelector('summary').click();
      await waitFor(() => dialog.innerText.includes('合成拒绝理由'), '详情显示审核历史与理由');
      document.querySelector('#editor-close').click();
    """,
    )


def test_learning_scope_shows_preview_and_cancel_never_confirms(browser: Browser) -> None:
    browser.open("learning", 1280, 900)
    _run(
        browser,
        """
      const nativeFetch = window.fetch;
      await waitFor(() => find('#learning-panel-jargon input[name="disabled"]'), '黑话页已就绪');
      const calls = [];
      const response = value => Promise.resolve(new Response(JSON.stringify(value)));
      window.fetch = (url, options) => {
        const path = String(url);
        if (path.includes('/api/resources/learning?')) {
          return response({enabled:true, total:1, items:[{
            entry_key:'package:one', text:'合成词', detail:'合成释义', example:'', count:2,
            speakers:[], source_refs:[], evidence_ids:[], pending_scope:true,
            scope_keys:[], package_source:'原群', is_enabled:false,
          }]});
        }
        if (path.endsWith('/api/resources/learning/scope')) {
          if (options.method !== 'POST') return response({items:[{key:'known', label:'合成甲群'}]});
          const payload = JSON.parse(options.body);
          calls.push(payload);
          return response({before:[], after:['合成甲群'], confirmed:payload.confirm});
        }
        return nativeFetch(url, options);
      };
      const disabled = find('#learning-panel-jargon input[name="disabled"]');
      disabled.click();
      await waitFor(() => button('合成词 · 待映射'), '待映射条目可见');
      press('合成词 · 待映射');
      const dialog = document.querySelector('#editor-dialog');
      const dialogButton = label => [...dialog.querySelectorAll('button')]
        .find(node => node.textContent.trim() === label);
      check(dialogButton('设置生效会话'), '待映射详情提供范围维护：' + dialog.innerText);
      dialogButton('设置生效会话').click();
      await waitFor(() => dialogButton('预览范围'), '会话选项载入');
      dialog.querySelector('input[value="known"]').click();
      dialogButton('预览范围').click();
      await waitFor(() => dialogButton('确认范围') &&
        dialog.innerText.includes('确认后授权：合成甲群'), '确认前展示影响范围');
      dialogButton('取消').click();
      check(!dialog.open && calls.length === 1 && calls[0].confirm === false &&
        calls[0].conversation_keys.join() === 'known', '取消只提交过预览');
    """,
    )


def test_learning_package_entry_previews_without_writing(browser: Browser) -> None:
    browser.open("learning", 1280, 900)
    _run(
        browser,
        """
      await waitFor(() => button('导入') && !button('导入').disabled, '黑话包入口就绪');
      check(!button('导出').disabled, '黑话包导出可用');
      const nativeFetch = window.fetch;
      const calls = [];
      window.fetch = (url, options) => {
        if (!String(url).startsWith('/api/resources/learning/package/')) {
          return nativeFetch(url, options);
        }
        calls.push([url, options.method, options.body instanceof File]);
        return Promise.resolve(new Response(JSON.stringify({
          digest: 'a'.repeat(64), can_save: 2, can_recall: 0,
        })));
      };
      press('导入');
      const dialog = document.querySelector('#editor-dialog');
      const file = dialog.querySelector('input[type="file"]');
      const transfer = new DataTransfer();
      transfer.items.add(new File(['{}'], 'jargon.json', {type:'application/json'}));
      file.files = transfer.files;
      file.dispatchEvent(new Event('change', {bubbles:true}));
      dialog.querySelector('form').requestSubmit();
      await waitFor(() => dialog.innerText.includes('可导入保存 2 条，可参与召回 0 条'), '仅预览');
      document.querySelector('#editor-close').click();
      check(!dialog.open && calls.length === 1 &&
        calls[0][0].endsWith('/jargon') && calls[0][1] === 'POST' && calls[0][2],
        '取消不发确认请求');
      window.fetch = nativeFetch;
      click('[data-learning-tab="expression"]');
      await waitFor(() => button('导入') && !button('导入').disabled, '表达包入口就绪');
      press('导入');
      check(dialog.innerText.includes('导入表达方式'), '表达和黑话使用独立入口');
      document.querySelector('#editor-close').click();
    """,
    )


def test_emoji_density_switches_visible_thumbnail_size(browser: Browser) -> None:
    browser.open("learning", 390, 740)
    _run(
        browser,
        """
      await waitFor(() => find('[data-learning-tab="emoji"]'), '学习页可切换到表情');
      click('[data-learning-tab="emoji"]');
      const tile = () => button('已登记 · 合成表情000');
      const picture = () => tile()?.querySelector('img');
      await waitFor(() => visible(picture()) && picture().naturalWidth && button('小'),
        '显示表情缩略图和大小按钮');
      const middle = picture().getBoundingClientRect().width;
      const mediumPage = page();
      check(button('中').getAttribute('aria-pressed') === 'true', '默认选中中号');
      press('小');
      await waitFor(() => visible(picture()) && picture().getBoundingClientRect().width < middle,
        '小号切换确实缩小图片');
      check(page() !== mediumPage, '缩略图容量改变后重新计算总页数');
      press('大');
      await waitFor(() => visible(picture()) && picture().getBoundingClientRect().width > middle,
        '大号切换确实放大图片');
      check(!button('添加黑话') && !button('添加表达方式'), '表情页没有添加文字按钮');
      tile().click();
      const dialog = document.querySelector('#editor-dialog');
      await waitFor(() => dialog.open && dialog.querySelector('img')?.naturalWidth,
        '表情详情显示实际原图');
      check(dialog.innerText.includes('已登记') && dialog.innerText.includes('使用次数'),
        '表情详情');
      document.querySelector('#editor-close').click();
      check(button('下一页').getBoundingClientRect().bottom <= innerHeight, '表情页脚不被挤出');
    """,
    )


def test_emoji_filters_sort_and_keep_global_counts_on_narrow_screen(browser: Browser) -> None:
    browser.open("learning", 390, 740)
    _run(
        browser,
        """
      await waitFor(() => find('[data-learning-tab="emoji"]'), '学习页可切换到表情');
      click('[data-learning-tab="emoji"]');
      const panel = document.querySelector('#learning-panel-emoji');
      const select = (name, value) => {
        const input = panel.querySelector(`select[name="${name}"]`);
        input.value = value;
        input.dispatchEvent(new Event('change', {bubbles:true}));
      };
      const counts = () => panel.querySelector('#emoji-library-counts').textContent;
      await waitFor(() => button('已登记 · 合成表情000') && counts().includes('全库 320 条'),
        '默认顺序与全库统计');
      const initialCounts = counts();
      const initialPage = page();
      select('format', 'gif');
      await waitFor(() => button('已登记 · 合成表情002') && !button('已登记 · 合成表情000'),
        '仅显示 GIF');
      check(page() !== initialPage && counts() === initialCounts,
        '格式只改变结果页，不改变全库统计');
      select('sort', 'most_used');
      await waitFor(() => button('已登记 · 合成表情306') && !button('已登记 · 合成表情002'),
        '使用次数排序从首屏显示较常用的 GIF');
      button('已登记 · 合成表情306').click();
      check(document.querySelector('#editor-dialog').innerText.includes('GIF'), '详情显示实际格式');
      document.querySelector('#editor-close').click();
      check(counts() === initialCounts, '切换排序后继续显示全库统计');
      check(button('下一页').getBoundingClientRect().bottom <= innerHeight, '窄屏分页仍可见');
    """,
    )


def test_emoji_upload_edit_search_and_status_maintenance(browser: Browser) -> None:
    browser.open("learning", 1280, 900)
    with BytesIO() as stream:
        Image.new("RGB", (12, 12), "purple").save(stream, format="PNG")
        image_data = base64.b64encode(stream.getvalue()).decode("ascii")
    _run(
        browser,
        f"""
      await waitFor(() => find('[data-learning-tab="emoji"]'), '学习页可切换到表情');
      click('[data-learning-tab="emoji"]');
      await waitFor(() => button('上传表情') && !button('上传表情').disabled, '表情列表加载');
      press('上传表情');
      const dialog = document.querySelector('#editor-dialog');
      const dialogButton = label => [...dialog.querySelectorAll('button')]
        .find(node => node.textContent.trim() === label);
      const input = dialog.querySelector('input[type="file"]');
      const file = new File([Uint8Array.from(atob('{image_data}'), byte => byte.charCodeAt(0))],
        'fixture.png', {{type:'image/png'}});
      const transfer = new DataTransfer(); transfer.items.add(file); input.files = transfer.files;
      input.dispatchEvent(new Event('change', {{bubbles:true}}));
      const nativeFetch = window.fetch; let failOnce = true;
      window.fetch = async (url, options) => {{
        if (failOnce && String(url).startsWith('/api/resources/emoji') &&
          options.method === 'POST') {{
          failOnce = false;
          return new Response(JSON.stringify({{detail:'合成失败'}}),
            {{status:422, headers:{{'Content-Type':'application/json'}}}});
        }}
        return nativeFetch(url, options);
      }};
      dialog.querySelector('form').requestSubmit();
      await waitFor(() => document.querySelector('#toasts')?.innerText.includes('合成失败'),
        '上传错误可见');
      check(input.files.length === 1 && !dialog.querySelector('form button').disabled,
        '失败保留图片并允许重试');
      dialog.querySelector('form').requestSubmit();
      await waitFor(() => !dialog.open && button('未登记 · 未填写描述'), '上传重试成功');
      window.fetch = nativeFetch;

      button('未登记 · 未填写描述').click();
      await waitFor(() => dialog.open && dialogButton('编辑'), '打开新增表情详情');
      dialogButton('编辑').click();
      const editDialog = document.querySelector('#editor-dialog');
      editDialog.querySelector('textarea').value = '月兔握手';
      editDialog.querySelectorAll('textarea')[1].value = '握手\\n庆祝';
      editDialog.querySelector('form').requestSubmit();
      await waitFor(() => !editDialog.open && button('未登记 · 月兔握手'), '编辑描述与标签');

      const search = find('input[type="search"]');
      search.value = '庆祝'; search.form.requestSubmit();
      await waitFor(() => button('未登记 · 月兔握手'), '新标签可用于搜索');
      button('未登记 · 月兔握手').click();
      await waitFor(() => dialog.open && dialogButton('登记'), '查看未登记表情');
      dialogButton('登记').click();
      await waitFor(() => !dialog.open && button('已登记 · 月兔握手'), '登记状态立即刷新');
      button('已登记 · 月兔握手').click();
      await waitFor(() => dialog.open && dialogButton('禁用'), '打开已登记表情详情');
      dialogButton('禁用').click();
      await waitFor(() => !dialog.open && button('已禁用 · 月兔握手'), '禁用后列表仍可见');
      const status = find('select[name="status"]'); status.value = 'banned';
      status.dispatchEvent(new Event('change', {{bubbles:true}}));
      await waitFor(() => button('已禁用 · 月兔握手'), '禁用筛选');
      button('已禁用 · 月兔握手').click();
      await waitFor(() => dialog.open && dialogButton('恢复'), '查看禁用表情');
      dialogButton('恢复').click();
      await waitFor(() => !dialog.open && !button('已禁用 · 月兔握手'), '恢复立即退出禁用筛选');
      status.value = 'registered'; status.dispatchEvent(new Event('change', {{bubbles:true}}));
      await waitFor(() => button('已登记 · 月兔握手'), '恢复保留原登记状态');
    """,
    )


def test_emoji_batch_upload_cancel_mixed_results_and_filter_persistence(browser: Browser) -> None:
    browser.open("learning", 390, 740)
    with BytesIO() as stream:
        Image.new("RGB", (8, 8), "orange").save(stream, format="PNG")
        image_data = base64.b64encode(stream.getvalue()).decode("ascii")
    _run(
        browser,
        f"""
      await waitFor(() => find('[data-learning-tab="emoji"]'), '学习页可切换到表情');
      click('[data-learning-tab="emoji"]');
      await waitFor(() => button('批量上传') && !button('批量上传').disabled, '表情列表加载');
      const panel = document.querySelector('#learning-panel-emoji');
      const countValue = () => Number(panel.querySelector('#emoji-library-counts')
        ?.innerText.match(/全库 (\\d+) 条/)?.[1]);
      await waitFor(() => Number.isFinite(countValue()), '初始全库统计可见');
      const beforeCount = countValue();
      const search = panel.querySelector('input[type="search"]');
      search.value = '批次筛选'; search.form.requestSubmit();
      const nativeFetch = window.fetch;
      const requests = [];
      let release;
      const gate = new Promise(resolve => release = resolve);
      window.fetch = async (url, options) => {{
        if (url === '/api/resources/emoji' && options.method === 'POST') {{
          requests.push(options.body.name);
          if (requests.length === 1) await gate;
          if (options.body.name === 'broken.svg')
            return new Response(JSON.stringify({{detail:'图片格式不支持'}}),
              {{status:422, headers:{{'Content-Type':'application/json'}}}});
        }}
        return nativeFetch(url, options);
      }};
      const image = new File([Uint8Array.from(atob('{image_data}'), byte => byte.charCodeAt(0))],
        'first.png', {{type:'image/png'}});
      const imageBytes = Uint8Array.from(atob('{image_data}'), byte => byte.charCodeAt(0));
      const files = [image, new File([imageBytes], 'again.png', {{type:'image/png'}}),
        new File(['invalid'], 'broken.svg', {{type:'image/svg+xml'}})];
      const choose = () => {{
        const input = document.querySelector('#editor-dialog input[type="file"]');
        const transfer = new DataTransfer(); files.forEach(file => transfer.items.add(file));
        input.files = transfer.files; input.dispatchEvent(new Event('change', {{bubbles:true}}));
      }};
      press('批量上传');
      let dialog = document.querySelector('#editor-dialog');
      const uploadButton = () => [...dialog.querySelectorAll('button')]
        .find(node => node.textContent.trim() === '上传所选图片');
      check(uploadButton().disabled, '尚未选择文件时不可提交');
      choose();
      check(!uploadButton().disabled, '选择文件后可提交');
      const input = dialog.querySelector('input[type="file"]');
      input.value = ''; input.dispatchEvent(new Event('change', {{bubbles:true}}));
      check(uploadButton().disabled, '清空选择后再次不可提交');
      choose();
      check(dialog.innerText.includes('已选择 3 张'), '确认前显示文件数');
      document.querySelector('#editor-close').click();
      check(requests.length === 0, '取消不发上传请求');
      press('批量上传'); choose();
      dialog = document.querySelector('#editor-dialog');
      dialog.querySelector('form').requestSubmit();
      await waitFor(() => requests.length === 1 && dialog.querySelector('fieldset').disabled,
        '第一张上传中禁用操作');
      dialog.querySelector('form').requestSubmit();
      check(requests.length === 1, '重复确认不增加请求');
      release();
      await waitFor(() => dialog.innerText.includes('first.png：已上传') &&
        dialog.innerText.includes('again.png：已存在，保留原有记录') &&
        dialog.innerText.includes('broken.svg：图片格式不支持') &&
        !dialog.querySelector('fieldset').disabled, '逐张保留结果');
      check(requests.length === 3, '每个文件仅上传一次');
      await waitFor(() => countValue() === beforeCount + 1,
        '刷新全库统计');
      check(search.value === '批次筛选' && !button('未登记 · 未填写描述'),
        '筛选保留且当前页不插入未命中图片');
      window.fetch = nativeFetch;
    """,
    )


def test_memory_pagination_details_and_button_states(browser: Browser) -> None:
    browser.open("memory", 390, 740)
    _run(
        browser,
        r"""
      await waitFor(() => text('合成记忆000') && button('下一页'), '文字记忆有查询结果');
      const expected = Math.ceil(320 / find('#memory-result').childElementCount);
      check(button('上一页').disabled && !button('下一页').disabled, '记忆首页的按钮状态');
      press('合成记忆000');
      const dialog = document.querySelector('#editor-dialog');
      check(dialog.open && dialog.innerText.includes('合成原件'), '文字记录细节显示原件');
      document.querySelector('#editor-close').click();
      check(find('#memory-correct').disabled, '未勾选不可修正');
      click('#memory-select-all');
      check(!find('#memory-correct').disabled, '选中文字记忆可以修正');
      press('下一页');
      await waitFor(() => page() === `第 2 / ${expected} 页` && text('合成记忆') &&
        !text('合成记忆000'), '文字记忆翻页替换列表');
      check(find('#memory-correct').disabled, '换页不带入上一页选中项');
      click('[data-memory-query="profiles"]');
      await waitFor(() => text('合成人物000') && !text('合成记忆000'), '人物画像独立显示');
      check(button('上一页').disabled && !visible(find('#memory-correct')), '画像无文字维护按钮');
      click('[data-memory-tab="images"]');
      await waitFor(() => text('合成图片000') && !text('合成人物000'), '图片记忆切换迁入素材');
      check(button('下一页').getBoundingClientRect().bottom <= innerHeight, '图片页窄屏可分页');
    """,
    )


def test_memory_inspection_shows_counts_and_limits(browser: Browser) -> None:
    browser.open("memory", 390, 740)
    _run(
        browser,
        r"""
      await waitFor(() => text('合成记忆000'), '文字记忆有查询结果');
      press('只读检修');
      const dialog = document.querySelector('#editor-dialog');
      await waitFor(() => dialog.open && dialog.innerText.includes('维护停用：1 条'), '检修分项');
      check(dialog.innerText.includes('已检查 3 条') && dialog.innerText.includes('已移除：0 条') &&
        dialog.innerText.includes('可见标签不一致：1 条'), '显示已检查与各项计数');
      check(dialog.innerText.includes('原件缺失未检查') &&
        dialog.innerText.includes('零异常不代表全库完整'), '显示报告边界');
      check(!dialog.innerText.includes('合成记忆000'), '报告不显示原件正文');
      document.querySelector('#editor-close').click();
      check(!dialog.open && text('合成记忆000'), '关闭报告保留原来的结果');
    """,
    )


def test_memory_batch_remove_and_restore(browser: Browser) -> None:
    browser.open("memory", 390, 740)
    _run(
        browser,
        r"""
      const list = () => find('#memory-result');
      const boxes = () => [...list().querySelectorAll('input[type="checkbox"]')];
      const toggle = () => find('#memory-toggle-removed');
      const writes = [];
      const fetchOriginal = window.fetch;
      window.fetch = (url, options) => {
        if (url === '/api/resources/memory/batch' && options?.method === 'PATCH') {
          writes.push(JSON.parse(options.body));
        }
        return fetchOriginal(url, options);
      };
      await waitFor(() => text('合成记忆000'), '文字记忆初始查询');
      const dialog = document.querySelector('#editor-dialog');
      const dialogButton = label => [...dialog.querySelectorAll('button')]
        .find(node => node.textContent.trim() === label);
      find('#memory-search [name="q"]').value = '合成批量';
      click('#memory-search [name="disabled"]');
      await waitFor(() => text('合成批量000') && !button('下一页').disabled, '筛出多页的批量用例');
      const pages = Number(page().match(/\/ (\d+)/)[1]);
      check(pages > 1, '批量场景有多页');
      boxes()[0].click();
      check(!toggle().disabled, '本页勾选后可批量移除');
      for (let target = 2; target <= pages; target++) {
        press('下一页');
        await waitFor(() => page() === `第 ${target} / ${pages} 页`, '到达下一页');
        check(toggle().disabled && boxes().every(box => !box.checked), '翻页清空上一页的选择');
      }
      const selected = boxes().map(box => box.parentElement.innerText.trim());
      const targets = selected.map(title => {
        const number = Number(title.slice(-3));
        return {kind: number % 2 ? 'history' : 'live', entry_id: Math.floor(number / 2) + 1};
      });
      click('#memory-select-all');
      toggle().click();
      check(dialog.open && dialog.innerText.includes(`本页选中的 ${selected.length} 条文字记忆`),
        '二次确认展示本页影响范围和实际条数');
      dialogButton('取消').click();
      check(!dialog.open && writes.length === 0 && boxes().every(box => box.checked), '取消不写入');
      toggle().click();
      dialogButton('确认移除').click();
      await waitFor(() => page() === `第 ${pages - 1} / ${pages - 1} 页` && !dialog.open,
        '整个末页被移除，退回新末页、重新计算页数');
      check(JSON.stringify(writes) === JSON.stringify([{removed:true, targets}]),
        '仅提交本页选中类型及编号');
      check(!selected.some(title => text(title)), '移除记录不在普通文字列表');
      click('#memory-search [name="removed"]');
      await waitFor(() => text('已移除 · 合成批量014') && page() === '第 1 / 1 页', '查看已移除');
      check(boxes().length === selected.length && find('#memory-correct').disabled,
        '只显示移除的记录');
      click('#memory-select-all');
      toggle().click();
      check(dialog.innerText.includes('不会启用原已停用'), '恢复确认说明原停用状态不变');
      dialogButton('确认恢复').click();
      await waitFor(() => page() === '第 0 / 0 页' && !dialog.open, '回收列表清空');
      check(JSON.stringify(writes[1]) === JSON.stringify({removed:false, targets}),
        '恢复同一批原件');
      click('#memory-search [name="removed"]');
      await waitFor(() => page() === `第 1 / ${pages} 页`, '恢复后总页数还原');
      for (let target = 2; target <= pages; target++) {
        press('下一页');
        await waitFor(() => page() === `第 ${target} / ${pages} 页`, '返回恢复过的末页');
      }
      press('未参与召回 · 合成批量014');
      check(dialog.open && dialog.innerText.includes('恢复召回'), '恢复移除不启用原停用记录');
    """,
    )


def test_memory_operations_show_states_and_paginate_at_390px(browser: Browser) -> None:
    browser.open("memory", 390, 740)
    _run(
        browser,
        r"""
      await waitFor(() => text('合成记忆000'), '文字记忆初始查询');
      let mode = 'empty', pageSize = 0;
      const realFetch = window.fetch;
      window.fetch = (resource, options) => {
        const url = new URL(resource, location.origin);
        if (url.pathname !== '/api/resources/memory/operations') {
          return realFetch(resource, options);
        }
        const offset = Number(url.searchParams.get('offset'));
        const limit = Number(url.searchParams.get('limit'));
        if (mode === 'error') {
          return Promise.resolve(Response.json({detail:'暂时无法读取'}, {status:503}));
        }
        const total = mode === 'records' ? limit * 2 + 1 : 0;
        pageSize = limit;
        const length = Math.min(limit, Math.max(0, total - offset));
        const items = Array.from({length}, (_, index) => ({
          kind:(offset + index) % 2 ? 'remove' : 'restore', changed:offset + index + 1,
          occurred_at:'2026-11-23T00:00:00Z',
        }));
        return Promise.resolve(Response.json({enabled:mode !== 'disabled', total, items,
          next_offset:offset + limit < total ? offset + limit : null}));
      };
      press('操作记录');
      await waitFor(() => text('没有可见的移除或恢复记录') && page() === '第 0 / 0 页', '空态');
      press('记忆查询');
      mode = 'disabled';
      press('操作记录');
      await waitFor(() => text('此功能当前关闭'), '关闭状态');
      press('记忆查询');
      mode = 'error';
      press('操作记录');
      await waitFor(() => text('读取失败') && button('重试'), '读取失败不伪装成空态');
      mode = 'records';
      press('重试');
      await waitFor(() => page() === '第 1 / 3 页' && text('恢复 1 条') && text('移除 2 条'),
        '展示操作和总页数');
      const time = find('#memory-operations-result time');
      check(time.dateTime === '2026-11-23T00:00:00Z' && time.textContent.includes('2026年'),
        '显示实际时间');
      check(root().scrollWidth <= innerWidth && innerWidth === 390, '窄屏不横向溢出');
      press('下一页');
      await waitFor(() => page() === '第 2 / 3 页', '第二页');
      press('下一页');
      await waitFor(() => page() === '第 3 / 3 页' && text(`恢复 ${pageSize * 2 + 1} 条`),
        '末页实际条数');
      check(button('下一页').disabled && !button('上一页').disabled, '末页按钮状态');
      press('上一页');
      await waitFor(() => page() === '第 2 / 3 页', '返回上一页');
    """,
    )


def test_memory_graph_pages_and_discards_stale_results_at_390px(browser: Browser) -> None:
    browser.open("memory", 390, 740)
    _run(
        browser,
        r"""
      await waitFor(() => text('合成记忆000'), '文字记忆初始查询');
      const path = '/api/resources/memory/graph';
      const entity = index => ({kind:'entity', source:`["合成","memory","entities",[${index}]]`,
        name:`合成图谱实体${index}`, evidence:[]});
      const relation = {kind:'relation', source:'["合成","memory","relations",[1]]',
        subject:entity(0), object:entity(1), predicate:'观察', confidence:0.7, evidence:[]};
      let granted = true, size = 0, delay = '', resume;
      const realFetch = window.fetch;
      window.fetch = (resource, options) => {
        const url = new URL(resource, location.origin);
        if (![path, path + '/detail'].includes(url.pathname)) return realFetch(resource, options);
        const params = url.searchParams;
        let result, status = 200;
        if (url.pathname.endsWith('/detail')) {
          result = granted ? entity(0) : {detail:'找不到这条图谱资料。'};
          status = granted ? 200 : 404;
        } else {
          const offset = Number(params.get('offset'));
          size = Number(params.get('limit'));
          const neighbor = params.has('neighbor_of');
          const total = !granted || params.get('q') ? 0 : neighbor ? 1 : size * 2 + 1;
          result = {covered:granted, total,
            next_offset:offset + size < total ? offset + size : null,
            items:Array.from({length:Math.max(0, Math.min(size, total - offset))},
              (_, index) => neighbor ? relation : entity(offset + index))};
        }
        const response = Response.json(result, {status});
        if (delay === url.pathname) {
          delay = '';
          return new Promise(resolve => { resume = () => resolve(response); });
        }
        return Promise.resolve(response);
      };
      press('关系图');
      await waitFor(() => page() === '第 1 / 3 页' && text('合成图谱实体0'), '首屏图谱');
      check(text(`共 ${size * 2 + 1} 项可见资料`), '只显示可见总数');
      const panel = find('#memory-panel-graph');
      check(innerWidth === 390 && root().scrollWidth <= innerWidth, '窄屏不横向溢出');
      const height = panel.scrollHeight;
      press('下一页');
      await waitFor(() => page() === '第 2 / 3 页', '第二页');
      press('下一页');
      await waitFor(() => page() === '第 3 / 3 页', '末页');
      check(button('下一页').disabled && panel.scrollHeight <= height + 1, '末页不接成长列表');
      press('刷新');
      await waitFor(() => text('合成图谱实体0'), '刷新回到第一页');
      press('实体 · 合成图谱实体0');
      await waitFor(() => document.querySelector('#editor-dialog').open, '显示获准详情');
      document.querySelector('[data-graph-detail] button').click();
      await waitFor(() => text('合成图谱实体0 → 观察 → 合成图谱实体1'), '单跳保留方向');
      press('返回全部图谱');
      await waitFor(() => text('合成图谱实体0'), '返回实体列表');
      find('#memory-panel-graph input[type=search]').value = '没有命中';
      press('查询');
      await waitFor(() => text('没有符合条件的图谱资料') && page() === '第 0 / 0 页',
        '有覆盖零命中');
      find('#memory-panel-graph input[type=search]').value = '';
      press('查询');
      await waitFor(() => text('合成图谱实体0'), '恢复全量浏览');
      delay = path + '/detail';
      press('实体 · 合成图谱实体0');
      await waitFor(() => resume, '详情正在返回');
      const oldDetail = resume;
      resume = null;
      delay = path;
      press('刷新');
      await waitFor(() => resume, '列表正在返回');
      const oldList = resume;
      granted = false;
      press('记忆查询');
      await waitFor(() => text('合成记忆000'), '离开图谱');
      press('关系图');
      await waitFor(() => text('当前没有获准的图谱资料'), '撤权后无覆盖');
      oldDetail();
      oldList();
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      check(!panel.textContent.includes('合成图谱实体'), '迟到列表不得重现旧资料');
      check(!document.querySelector('#editor-dialog').open, '迟到详情不得重开对话框');
      check(text('当前没有获准的图谱资料') && page() === '第 0 / 0 页', '撤权后保留正确空态');
    """,
    )


def test_image_prompt_preview_displays_text_then_attachment(browser: Browser) -> None:
    browser.open("timeline", 390, 740)
    _run(
        browser,
        r"""
      await waitFor(() => typeof openPromptPreview === 'function', '加载预览入口');
      await openPromptPreview('vlm');
      const dialog = document.querySelector('#prompt-preview');
      check(dialog.open && visible(dialog), '显示提示词对话框');
      check(dialog.querySelector('option').textContent === '普通图片描述', '显示专用场景');
      check(dialog.innerText.includes('预览不读取或发送图片'), '说明预览没有读取图片');
      const [system, user] = dialog.querySelectorAll('#prompt-preview-body section');
      check(system.querySelector('h4').textContent === 'system', '显示 system 角色');
      check(system.querySelector('pre').textContent === '合成系统提示', '文本消息仍可阅读');
      const [role, textPart, imagePart] = user.children;
      check(role.textContent === 'user', '显示 user 角色');
      check(textPart.tagName === 'PRE' && textPart.textContent === '合成图片描述指令\n只返回文字',
        '先呈现完整的多行文字');
      check(imagePart.textContent === '〔来信图片〕' && visible(imagePart), '次序之后显示图片占位');
      check(!dialog.querySelector('img') && !dialog.innerText.includes('[object Object]'),
        '不加载图片，也不把图文数组显示为对象字符串');
      const select = dialog.querySelector('select');
      check(select.options[1].textContent === 'GIF QQ 表情识别', '表情和普通图片场景分开');
      select.value = 'emoji_gif';
      select.dispatchEvent(new Event('change'));
      const emojiUser = dialog.querySelectorAll('#prompt-preview-body section')[1];
      check(emojiUser.querySelector('pre').textContent === '输入类型：GIF\n返回表情标签',
        '切换后显示表情指令');
      check(emojiUser.querySelector('p').textContent === '〔GIF 表情拼图〕', '显示专用拼图占位');
      check(!dialog.querySelector('img'), '表情拼图仍为纯文字占位');
    """,
    )


def test_timeline_recall_feedback_controls(browser: Browser) -> None:
    browser.open("timeline", 390, 740)
    _run(
        browser,
        r"""
      await waitFor(() => text('没有依据的旧回复') && text('可评价的合成回复'), '显示两条回复');
      const summaries = [...root().querySelectorAll('summary')]
        .filter(node => node.textContent === '评价召回依据');
      check(summaries.length === 1, '旧回复不出现评价按钮');
      summaries[0].click();
      await waitFor(() => text('时间线记录 #40') && text('未评价'), '加载该回复的依据');
      check(button('撤销评价').disabled, '未评价时不可撤销');
      press('有用');
      await waitFor(() => text('已标有用') && button('有用').disabled, '可标为有用');
      press('无用');
      await waitFor(() => text('已标无用') && button('无用').disabled, '可改标无用');
      press('撤销评价');
      await waitFor(() => text('未评价') && button('撤销评价').disabled, '可撤销至未评价');
    """,
    )


def test_learning_reflows_when_the_available_height_changes(browser: Browser) -> None:
    browser.open("learning", 1280, 900)
    _run(
        browser,
        """
      await waitFor(() => text('合成黑话000') && !button('下一页').disabled, '高屏首页稳定');
      window.tallCount = find('#learning-jargon-result').childElementCount;
      press('下一页');
      await waitFor(() => text('合成黑话') && !text('合成黑话000'), '高屏第二页稳定');
    """,
    )
    browser.resize(390, 630)
    _run(
        browser,
        """
      const list = () => find('#learning-jargon-result');
      await waitFor(() => text('合成黑话000') && list().childElementCount < window.tallCount,
        '缩短视口后重分首页，减少每页记录');
      const expected = Math.ceil(320 / list().childElementCount);
      check(page() === `第 1 / ${expected} 页`, '视口变窄后更新总页数');
      check(button('上一页').disabled && !button('下一页').disabled, '重分页后按钮状态');
      check(list().lastElementChild.getBoundingClientRect().bottom <=
        button('下一页').getBoundingClientRect().top, '分页按钮不遮挡末条记录');
    """,
    )


def test_memory_image_bookmark_filter_and_flow(browser: Browser) -> None:
    browser.open("images", 390, 740)
    _run(
        browser,
        """
      await waitFor(() => text('合成图片000'), '旧图片书签打开长期记忆里的图片页');
      check(location.hash === '#memory', '旧书签映射到长期记忆');
      const availability = find('select[name="availability"]');
      availability.value = 'reference';
      availability.dispatchEvent(new Event('change', {bubbles:true}));
      await waitFor(() => text('合成收件箱000') && !button('下一页').disabled, '收件箱图片首页');
      press('下一页');
      await waitFor(() => text('合成收件箱') && !text('合成收件箱000'), '在仅引用的图片筛选内翻页');
      press('上一页');
      await waitFor(() => text('合成收件箱000'), '回到收件箱图片首页');
      click('[data-memory-tab="flow"]');
      await waitFor(() => text('合成事件000') && !button('下一页').disabled, '记忆流首页');
      const times = [...root().querySelectorAll('input[type="datetime-local"]')];
      const themeField = getComputedStyle(find('select'));
      check(times.length === 2 && times.every(node => {
        const style = getComputedStyle(node);
        return style.borderRadius === themeField.borderRadius &&
          style.backgroundColor === themeField.backgroundColor &&
          style.fontFamily === themeField.fontFamily;
      }), '开始和结束时间沿用表单主题');
      check([...document.querySelectorAll('input, textarea, select, form')].every(node =>
        node.getAttribute('autocomplete') === 'off'), '静态和动态表单均关闭浏览器自动填充');
      press('下一页');
      await waitFor(() => text('合成事件') && !text('合成事件000'), '记忆流分页');
      press('最近24小时');
      await waitFor(() => text('合成事件000') && button('上一页').disabled, '时间筛选回到首页');
      click('[data-memory-tab="transfer"]');
      await waitFor(() => button('追加导入'), '导入导出页可见');
      check(button('追加导入').disabled, '未选择文件不可导入');
      check(!button('下一页'), '导入导出不是分页列表');
    """,
    )
