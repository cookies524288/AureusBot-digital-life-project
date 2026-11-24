"""用独立 Chrome 实例和编造的 API 数据验收资源页的浏览交互。"""

import base64
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
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
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from websockets.sync.client import ClientConnection, connect

from aureus.channels import WebChannel
from aureus.memory import HistoricalMemory, MemoryIndex, MemorySource
from aureus.web import create_app
from aureus.web.server import SESSION_COOKIE
from tests.web.test_server import FakeHeartbeat, Rig, set_password

STATIC = Path(__file__).resolve().parents[2] / "aureus" / "web" / "static"
STAMP = "2026-11-23T00:00:00Z"
TOTAL = 320
MEMORY_BATCH_TOTAL = 15
REVIEW_ENDPOINT = "/api/resources/learning/reviews"
SCOPE_ENDPOINT = "/api/resources/learning/scope"
ORIGINS_ENDPOINT = "/api/resources/learning/origins"
MEMORY_TRANSFER_ENDPOINT = "/api/resources/memory/transfer"


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
            "entry_key": f"asset:{digest}",
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
    emoji_reviews: dict[str, str]
    removed_memories: set[tuple[str, int]]
    expression_reviews: dict[str, list[dict[str, str]]]
    recall_rating: str | None
    memory_transfer: TestClient | None


class ResourceFixture(SimpleHTTPRequestHandler):
    @property
    def emoji_uploads(self) -> dict[str, dict[str, object]]:
        return cast(ResourceServer, self.server).emoji_uploads

    @property
    def emoji_reviews(self) -> dict[str, str]:
        return cast(ResourceServer, self.server).emoji_reviews

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
        elif url.path in (SCOPE_ENDPOINT, ORIGINS_ENDPOINT):
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
        if self.path in (MEMORY_TRANSFER_ENDPOINT, MEMORY_TRANSFER_ENDPOINT + "/preview"):
            client = cast(ResourceServer, self.server).memory_transfer
            if client is not None:
                response = client.post(
                    f"http://{self.headers['Host']}{self.path}",
                    content=self.rfile.read(int(self.headers["Content-Length"])),
                    headers={name: self.headers[name] for name in ("Origin", "Content-Type")},
                )
                self._send("application/json", response.content, status=response.status_code)
                return
        if self.path != "/api/resources/emoji":
            self.send_error(404)
            return
        data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        digest = sha256(data).hexdigest()
        uploads = self.emoji_uploads
        created = digest not in uploads
        description = self.headers.get("X-Emoji-Registration-Description")
        text = unquote(description) if description is not None else ""
        uploads.setdefault(
            digest,
            {
                "text": text,
                "description": text,
                "tags": [],
                "source": None,
                "media_kind": "emoji",
                "digest": digest,
                "is_banned": False,
                "has_file": True,
                "is_registered": description is not None,
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
        if self.path.endswith("/review"):
            digest = self.path.removeprefix(prefix).removesuffix("/review")
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            self.emoji_reviews[digest] = body["verdict"]
            self._send_json({"review": body["verdict"]})
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

    def _send(self, content_type: str, data: bytes, *, status: int = 200) -> None:
        self.send_response(status)
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
        if path.endswith("/learning"):
            origin_groups = {"group-10": range(24), "group-20": range(23, 25)}
            authorized_groups = {"group-10": (25,), "group-20": range(26, 50)}
            for field, groups in (
                ("origin_conversation_key", origin_groups),
                ("authorized_conversation_key", authorized_groups),
            ):
                if scope := params.get(field):
                    keys = {f"{kind}-{index}" for index in groups.get(scope[0], ())}
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
            for item in results:
                item["review"] = self.emoji_reviews.get(str(item["digest"]), "unreviewed")
            if review := params.get("review"):
                results = [item for item in results if item["review"] == review[0]]
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
        self.command("Emulation.setFocusEmulationEnabled", enabled=True)
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
    with (
        TemporaryDirectory(prefix="aureus-resource-chrome-") as directory,
        _open_browser(Path(directory)) as instance,
    ):
        yield instance


@contextmanager
def _open_browser(profile: Path, memory_transfer: TestClient | None = None) -> Iterator[Browser]:
    chrome = _chrome()
    server = ResourceServer(("127.0.0.1", 0), partial(ResourceFixture, directory=STATIC))
    server.emoji_uploads = {}
    server.emoji_reviews = {}
    server.removed_memories = set()
    server.expression_reviews = {}
    server.recall_rating = None
    server.memory_transfer = memory_transfer
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


def test_inbox_components_render_in_order_and_treat_poke_hints_as_text(browser: Browser) -> None:
    browser.open("timeline", 390, 740)
    _run(
        browser,
        """
      await waitFor(() => typeof renderInboxMessage === 'function', '收件箱脚本未就绪');
      const group = {account_id:'9000', kind:'group', recipient_id:'8000'};
      const bubble = message => renderInboxMessage(message).querySelector('.inbox-bubble');
      const mixed = bubble({conversation:group, sender_id:'2200', direction:'incoming',
        content:'兼容正文不可重复展示', components:[
          {kind:'text', text:'起始 '}, {kind:'at', target_account_id:'9000'},
          {kind:'reply', target_message_id:'-10', target_account_id:'2300'},
          {kind:'at', all_members:true}, {kind:'text', text:' 末尾'},
        ]});
      check([...mixed.children].map(part => part.textContent).join('|') ===
        '起始 |@QQ 9000|引用消息 -10 · 原发件人：QQ 2300|@全体成员| 末尾', '按真实组件顺序显示');
      const hint = '<img src="https://invalid.example/track">';
      const notice = bubble({conversation:group, sender_id:'2200', event_type:'poke_notice',
        components:[{kind:'poke', target_account_id:'2300', action:hint, suffix:'一下'}]});
      check(notice.textContent === `群戳：QQ 2200 → QQ 2300 · ${hint} 一下`, '群戳两方及提示');
      check(!notice.querySelector('img,a'), '提示不能加载远程图片或变成链接');
      const direct = bubble({conversation:{...group,kind:'private'}, sender_id:'9000',
        event_type:'poke_notice', components:[{kind:'poke', target_account_id:'2100'}]});
      check(direct.textContent === '私聊戳：QQ 9000 → QQ 2100', '缺提示时不补写');
      const legacy = bubble({content:'旧正文', quoted_message_id:'11', mentioned:true});
      check(legacy.textContent.includes('原发件人：未知') && legacy.textContent.includes('@未知'),
        '旧记录不猜参与者');
    """,
    )


def test_narrow_typing_bubbles_follow_only_the_open_conversation(browser: Browser) -> None:
    browser.open("inbox", 390, 740)
    _run(
        browser,
        """
      await waitFor(() => typeof followConversationTyping === 'function' && state.loggedIn,
        '网页未就绪');
      closeSocket();
      const key = {platform:'qq', account_id:'bot', kind:'group', recipient_id:'A'};
      const first = {key, name:'同名群'};
      const second = {key:{...key,recipient_id:'B'}, name:'同名群'};
      const requests = [];
      window.api = async (path, method, body, headers, signal) => {
        if (path.includes('/typing')) {
          return new Promise(resolve => requests.push({path, signal, resolve}));
        }
        if (path.includes('/operations')) return {ok:true,data:{enabled:true}};
        return {ok:true,data:[]};
      };
      await showPage('chat');
      inbox.view = 'qq';
      inbox.conversations = [first, second];
      const wrappedApi = window.api;
      window.api = (path, ...args) => {
        if (path === '/api/inbox/conversations') return Promise.resolve({ok:true,data:{
          enabled:true, conversations:[first, second],
        }});
        if (path.startsWith('/api/inbox/messages?')) {
          return Promise.resolve({ok:true,data:{messages:[]}});
        }
        return wrappedApi(path, ...args);
      };
      await showPage('inbox');
      const latest = () => requests.at(-1);
      const respond = (request, typing) => request.resolve({ok:true,data:{typing}});
      const bubble = id => document.querySelector(`#${id}-typing .typing-status`);
      await waitFor(() => latest()?.path.includes('recipient_id=A'), '无 WS 时桌面仍请求认证接口');
      const stale = latest();
      const conversations = document.querySelectorAll('#inbox-conversations button');
      conversations[1].click();
      await waitFor(() => latest()?.path.includes('recipient_id=B'), '后续请求只取 B');
      check(stale.signal.aborted, '离开 A 立即取消旧请求');
      respond(stale, true);
      respond(latest(), false);
      await new Promise(resolve => setTimeout(resolve, 50));
      check(!bubble('inbox'), '迟到的 A 不得让 B 显示气泡');
      const count = requests.length;
      await waitFor(() => requests.length > count, 'B 继续按需轮询');
      respond(latest(), true);
      await waitFor(() => bubble('inbox')?.textContent === '输入中', 'B 显示真实输入');
      check(bubble('inbox').getBoundingClientRect().left > 195, '窄屏贴平台机器人右侧');
      check(getComputedStyle(bubble('inbox').lastChild).display === 'none',
        '减少动效时只留静态文字');
      check(visible(document.querySelector('#inbox-send')), '不影响发送按钮');
      await showPage('chat');
      check(!bubble('inbox') && !bubble('chat'), '切页立即清除桌面，不显示跨会话输入');
      window.WebSocket = class {
        static OPEN = 1;
        readyState = 1;
        close() {}
      };
      openSocket();
      const connection = socket;
      connection.onopen();
      await waitFor(() => latest()?.path === '/api/chat/typing', '连上后查询当前私聊');
      respond(latest(), true);
      await waitFor(() => bubble('chat')?.textContent === '输入中', '网页私聊显示气泡');
      check(bubble('chat').getBoundingClientRect().left < 50, '网页气泡贴 bot 左侧');
      check(!document.querySelector('#chat-log .typing-status'), '不计入聊天消息');
      connection.onclose();
      check(!bubble('chat'), '断开立刻清除网页输入');
      openSocket();
      socket.onopen();
      await waitFor(() => latest()?.path === '/api/chat/typing', '重连重新查询');
      respond(latest(), false);
      closeSocket();
      check(!bubble('chat'), '重连不能恢复旧结果');
    """,
    )


def test_narrow_inbox_captions_are_independent_and_ignore_stale_requests(browser: Browser) -> None:
    browser.open("inbox", 390, 740)
    _run(
        browser,
        """
      await waitFor(() => typeof renderInboxImage === 'function' && state.loggedIn, '页面未就绪');
      const key = {platform:'qq',account_id:'bot',kind:'group',recipient_id:'A'};
      const requests = [];
      window.api = (path, method, body, headers, signal) => {
        if (path.includes('view=description')) {
          return new Promise(resolve => requests.push({path, resolve, signal}));
        }
        return Promise.resolve({ok:true,data:{messages:[],enabled:false}});
      };
      inbox.view = 'qq';
      inbox.enabled = true;
      inbox.conversations = [{key,name:'甲'}, {key:{...key,recipient_id:'B'},name:'乙'}];
      await renderInboxView();
      const message = {source:'inbox',id:20,conversation:key};
      const list = document.querySelector('#inbox-messages');
      const images = [renderInboxImage(message, 0), renderInboxImage(message, 1)];
      list.append(...images);
      const first = images[0].querySelector('button');
      const second = images[1].querySelector('button');
      first.click();
      second.click();
      check(requests[0].path.includes('/images/0?') && requests[1].path.includes('/images/1?'),
        '位置独立');
      first.click();
      check(requests[0].signal.aborted && first.textContent === '查看描述', '第一张可独立返回');
      requests[0].resolve({ok:true,data:{description:'过期图注',source:'vlm'}});
      requests[1].resolve({ok:true,data:{description:'第二张描述',source:'vlm'}});
      await waitFor(() => images[1].textContent.includes('第二张描述'), '第二张显示真实来源');
      check(images[1].textContent.includes('视觉模型') && !list.textContent.includes('过期图注'),
        '不串附件');
      first.click();
      requests[2].resolve({ok:true,data:{description:null,source:null}});
      await waitFor(() => images[0].textContent.includes('没有可核验'), '缺图注如实显示');
      first.click();
      check(!images[0].firstElementChild.classList.contains('hidden'), '缺图注仍能返回原图');
      second.click();
      second.click();
      await selectInboxConversation(inbox.conversations[1]);
      requests[3].resolve({ok:true,data:{description:'旧会话图注',source:'vlm'}});
      await new Promise(resolve => setTimeout(resolve, 50));
      check(!list.textContent.includes('旧会话图注'), '切会话丢弃迟到结果');
      check(root().scrollWidth <= root().clientWidth, '窄屏没有横向溢出');
    """,
    )


def test_narrow_qq_desktop_confirms_and_keeps_operations_in_selected_conversation(
    browser: Browser,
) -> None:
    browser.open("inbox", 390, 740)
    _run(
        browser,
        """
      await waitFor(() => typeof executeInboxOperation === 'function', '桌面脚本未就绪');
      const group = {platform:'qq',account_id:'1',kind:'group',recipient_id:'2'};
      const received = {source:'inbox',id:20,conversation:group,direction:'incoming',
        sender_id:'3',platform_message_id:'-10',content:'合成文字'};
      const posted = [];
      window.api = async (path, method, body, headers) => {
        if (method === 'POST') {
          posted.push({path, body, headers});
          const data = path.includes('/file?') ? {file_id:'fixture-file'} : {message_id:'-55'};
          return {ok:true,data};
        }
        if (path.startsWith('/api/inbox/operations?')) return {ok:true,data:{enabled:true}};
        return {ok:true,data:{messages:[received],next_before_id:null}};
      };
      const conversation = {key:group,name:'合成群'};
      const other = {key:{...group,recipient_id:'4'},name:'另一群'};
      await showPage('inbox', () => {});
      inbox.view = 'qq';
      inbox.conversations = [conversation, other];
      await renderInboxView();
      const desktopPage = document.querySelector('#page-inbox');
      check(desktopPage.scrollWidth <= desktopPage.clientWidth, '窄屏不得横向溢出');
      const messages = document.querySelector('#inbox-messages');
      await waitFor(() => !messages.querySelector('.inbox-avatar img'), '头像失败时回到像素人头');
      const fallback = messages.querySelector('.inbox-avatar svg use');
      check(fallback.getAttribute('href') === '#i-inbox-person', '缺省头像');
      const dialog = document.querySelector('#confirm-dialog');
      const textarea = document.querySelector('#inbox-text');
      messages.querySelectorAll('.inbox-row-actions button')[0].click();
      messages.querySelectorAll('.inbox-row-actions button')[1].click();
      textarea.value = '合成外发';
      document.querySelector('#inbox-send-text').requestSubmit();
      await waitFor(() => dialog.open, '发送前确认');
      check(dialog.textContent.includes('合成群') && dialog.textContent.includes('引用消息 -10'),
        '确认显示目的地与引用');
      await selectInboxConversation(other);
      await selectInboxConversation(conversation);
      dialog.close('yes');
      await waitFor(() => !inbox.operating, '旧确认结束');
      check(posted.length === 0 && textarea.value === '', '切走再切回也不能使用旧确认');
      textarea.value = '只发文字';
      document.querySelector('#inbox-send-text').requestSubmit();
      await waitFor(() => dialog.open, '再次确认');
      dialog.close('yes');
      await waitFor(() => posted.length === 1 && !inbox.operating, '等待合成消息回执');
      check(posted[0].body.quote === null && posted[0].body.text === '只发文字', '不默认引用');
      const input = document.querySelector('#inbox-file');
      const transfer = new DataTransfer();
      transfer.items.add(new File(['hello'], 'sample.txt', {type:'text/plain'}));
      input.files = transfer.files;
      document.querySelector('#inbox-send-file').requestSubmit();
      await waitFor(() => dialog.open, '必须确认文件传输');
      dialog.close('yes');
      await waitFor(() => posted.length === 2 && !inbox.operating, '等待合成文件回执');
      check(posted[1].body instanceof File && posted[1].path.includes('name=sample.txt'),
        '上传选件的实际字节');
      check(desktopPage.textContent.includes('文件 fixture-file'), '只显示收到的文件回执');
    """,
    )


@pytest.mark.parametrize("motion", ["no-preference", "reduce"])
def test_page_entrance_keeps_current_and_disabled_button_styles(
    browser: Browser, motion: str
) -> None:
    browser.open("learning", 1280, 900)
    browser.command(
        "Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": motion}]
    )
    _run(
        browser,
        r"""
      await waitFor(() => text('合成表达000'), '学习页就绪');
      const nav = name => document.querySelector(`[data-page="${name}"]`);
      const requests = [];
      api = path => new Promise(resolve => requests.push({path, resolve}));
      const original = Element.prototype.animate;
      const entrances = [];
      const staged = [];
      Element.prototype.animate = function(frames, timing) {
        if (this.matches('section.page')) {
          const key = this.id.slice(5);
          check(nav(key).getAttribute('aria-current') === 'page', '首帧前同步当前页');
          if (key === 'memory') check(
            find('[data-memory-tab="images"]').getAttribute('aria-disabled') === 'true',
            '书签对应的页内标签先选中');
          entrances.push(key);
        }
        const animation = original.call(this, frames, timing);
        if (this.closest('#page-config') && !this.matches('section.page')) {
          animation.pause();
          animation.currentTime = 0;
          staged.push(this);
        }
        return animation;
      };
      try {
        const older = showPage('images');
        const current = find('[data-memory-tab="images"]');
        const other = find('[data-memory-tab="query"]');
        check(current.getAttribute('aria-disabled') === 'true' &&
          other.getAttribute('aria-disabled') === 'false',
          '只锁当前页内标签');
        current.focus();
        const requestCount = requests.length;
        current.click();
        nav('memory').click();
        check(requests.length === requestCount && document.activeElement === current,
          '当前按钮不可再次加载但仍可聚焦');

        nav('config').click();
        const newer = pageNavigation.loading;
        const saves = ['#cf-save', '#cf-reset', '#cf-restore-all'].map(find);
        check(saves.every(control => control.disabled), '设置不可用的按钮保持原生禁用');
        const disabledStyle = control => [
          getComputedStyle(control).opacity, getComputedStyle(control).translate,
          getComputedStyle(control).boxShadow];
        const still = JSON.stringify(saves.map(disabledStyle));
        if (!reducedMotion.matches) await waitFor(() => staged.length, '设置内容开始入场');
        check(JSON.stringify(saves.map(disabledStyle)) === still,
          '入场动画不覆盖禁用按钮位移、透明度或基座');
        nav('config').focus();
        requests.at(-1).resolve({ok:false, status:0});
        await newer;
        requests[0].resolve({ok:false, status:0});
        await older;
        check(root().id === 'page-config' &&
          nav('config').getAttribute('aria-current') === 'page' &&
          document.activeElement === nav('config'), '迟到的响应不恢复旧页或焦点');
        check(JSON.stringify(entrances) ===
          JSON.stringify(reducedMotion.matches ? [] : ['memory','config']),
          '保留减少动态偏好');
      } finally { Element.prototype.animate = original; }
    """,
    )


@pytest.mark.parametrize(("width", "height"), [(1280, 900), (390, 740)])
def test_learning_pagination_and_category_switch(browser: Browser, width: int, height: int) -> None:
    browser.open("learning", width, height)
    _run(
        browser,
        r"""
      await waitFor(() => text('合成表达000'), '初始进入表达列表');
      const types = [...find('[aria-label="学习类型"]').querySelectorAll('button')];
      check(types.map(node => node.textContent).join('|') === '表达方式|黑话|表情包',
        '学习只有三个上层分类');
      check(find('[data-expression-view="clusters"]') && !types.includes(button('表达簇')),
        '表达簇只在表达内');
      click('[data-learning-tab="jargon"]');
      await waitFor(() => text('合成黑话000') && !button('下一页')?.disabled &&
        !find('select[name="origin_conversation_key"]')?.disabled &&
        !find('select[name="authorized_conversation_key"]')?.disabled, '黑话首页、范围和分页按钮');
      await document.fonts.ready;
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      await waitFor(() => text('合成黑话000') && !button('下一页')?.disabled,
        '尺寸确定后的黑话首页');
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
      search.value = '未提交的表达草稿';
      press('表达簇');
      check(find('[data-learning-tab="expression"]').getAttribute('aria-pressed') === 'true',
        '表达簇仍属于表达');
      press('表达列表');
      await waitFor(() => text('合成表达001') && !text('合成表达000'), '返回仍使用已提交的筛选');
      check(search.value === '未提交的表达草稿', '表达子页切换保留未提交草稿');
      search.value = '无匹配内容';
      search.form.requestSubmit();
      await waitFor(() => page() === '第 0 / 0 页', '空列表没有可翻的页');
      check(button('上一页').disabled && button('下一页').disabled, '空列表禁用翻页');
      click('[data-learning-tab="jargon"]');
      await waitFor(() => text('合成黑话000'), '黑话不沿用表达查询');
      check(!find('input[type="search"]').value, '黑话搜索栏独立');
      check(!button('表达簇') && !button('快速审核'), '黑话没有表达专属操作');
    """,
    )


def test_learning_conversation_filter_replaces_pages_and_ignores_stale_responses(
    browser: Browser,
) -> None:
    browser.open("learning", 390, 740)
    _run(
        browser,
        """
      const scope = () => find('select[name="origin_conversation_key"]');
      const authorized = () => find('select[name="authorized_conversation_key"]');
      const result = () => find('#learning-jargon-result');
      function select(key) {
        scope().value = key;
        scope().dispatchEvent(new Event('change', {bubbles:true}));
      }
      await waitFor(() => find('[data-learning-tab="jargon"]'), '学习页就绪');
      click('[data-learning-tab="jargon"]');
      await waitFor(() => scope() && !scope().disabled && !authorized().disabled &&
        button('合成黑话000'), '会话就绪');
      check(scope().labels[0].textContent.includes('来自会话') &&
        authorized().labels[0].textContent.includes('授权会话'), '来源和授权标签独立');
      check(document.documentElement.scrollWidth <= innerWidth && innerWidth === 390,
        '窄屏无横向溢出');
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
            parsed.searchParams.get('origin_conversation_key') === 'group-20') {
          return new Promise(resolve => {
            delayed = () => nativeFetch(url, {...options, signal:undefined}).then(response => {
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
      authorized().value = 'group-10';
      authorized().dispatchEvent(new Event('change', {bubbles:true}));
      await waitFor(() => page() === '第 0 / 0 页' && !button('合成黑话024'), '两种筛选取交集');
      check(scope().value === 'group-20', '设置授权不覆盖来源选择');
      select('');
      await waitFor(() => page() === '第 1 / 1 页' && button('合成黑话025'), '清空来源保留授权');
      authorized().value = '';
      authorized().dispatchEvent(new Event('change', {bubbles:true}));
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
      const scope = () => find('select[name="origin_conversation_key"]');
      const hint = () => find('#learning-scope-hint-jargon');
      const select = key => {
        scope().value = key;
        scope().dispatchEvent(new Event('change', {bubbles:true}));
      };
      await waitFor(() => find('[data-learning-tab="jargon"]'), '学习页就绪');
      click('[data-learning-tab="jargon"]');
      await waitFor(() => scope() && !scope().disabled && button('合成黑话000'), '首次会话就绪');
      select('group-20');
      await waitFor(() => button('合成黑话024') && !button('合成黑话000'), '首次会话筛选');
      const nativeFetch = window.fetch;
      const pending = [];
      const queries = [];
      window.fetch = (url, options) => {
        const parsed = new URL(url, location.href);
        if (parsed.pathname === '/api/resources/learning/origins') {
          return new Promise(resolve => pending.push(resolve));
        }
        if (parsed.pathname === '/api/resources/learning' &&
            parsed.searchParams.get('kind') === 'jargon') {
          queries.push(parsed.searchParams.get('origin_conversation_key'));
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
      await waitFor(() => find('[data-learning-tab="jargon"]'), '学习页就绪');
      click('[data-learning-tab="jargon"]');
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
      await waitFor(() => find('[data-learning-tab="jargon"]'), '学习页就绪');
      click('[data-learning-tab="jargon"]');
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


def test_expression_quality_jumps_pages_and_cancels_rescue(browser: Browser) -> None:
    browser.open("learning", 390, 740)
    _run(
        browser,
        r"""
      await waitFor(() => text('合成表达000'), '表达列表已就绪');
      const nativeFetch = window.fetch;
      const calls = [];
      let offset;
      const row = {id:2, text:'合成候选', detail:'庆祝', status:'rejected', reason:'mismatch',
        recorded_at:'2026-11-24T00:00:00Z', source_label:'同名群 · 群聊 10', actions:[]};
      window.fetch = (url, options) => {
        const request = new URL(url, location.href);
        if (!request.pathname.startsWith('/api/resources/learning/quality')) {
          return nativeFetch(url, options);
        }
        if (options.method === 'POST') {
          calls.push(JSON.parse(options.body));
          return Promise.resolve(new Response(JSON.stringify({...row, version:'0'.repeat(64)})));
        }
        offset = Number(request.searchParams.get('offset'));
        const payload = {enabled:true, total:320, sources:[{key:'group10', label:row.source_label}],
          items:[{...row, text:row.text + offset}]};
        return Promise.resolve(new Response(JSON.stringify(payload)));
      };
      try {
        press('质检记录');
        await waitFor(() => text('模型拒绝 · 合成候选0') && page()?.startsWith('第 1 /'),
          '质检首页');
        const jump = find('#learning-panel-quality .resource-page-jump');
        jump.value = '3';
        jump.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true}));
        await waitFor(() => page()?.startsWith('第 3 /') && offset > 0 && text('合成候选' + offset),
          '输入页码直达');
        press('模型拒绝 · 合成候选' + offset + ' · ' + row.source_label);
        const dialog = document.querySelector('#editor-dialog');
        const dialogButton = label => [...dialog.querySelectorAll('button')]
          .find(node => node.textContent.trim() === label);
        dialogButton('预览挽回').click();
        await waitFor(() => dialogButton('确认挽回'), '挽回预览');
        check(dialog.innerText.includes('仅此来源会话可用'), '说明范围');
        dialogButton('取消').click();
        check(!dialog.open && calls.length === 1 && calls[0].action === 'rescued' &&
          !('version' in calls[0]), '取消不发确认');
      } finally { window.fetch = nativeFetch; }
    """,
    )


def test_learning_package_entry_previews_without_writing(browser: Browser) -> None:
    browser.open("learning", 1280, 900)
    _run(
        browser,
        """
      await waitFor(() => find('[data-learning-tab="jargon"]'), '学习页就绪');
      click('[data-learning-tab="jargon"]');
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


def test_emoji_card_review_keeps_latest_filter_and_reports_failure(browser: Browser) -> None:
    browser.open("learning", 390, 740)
    _run(
        browser,
        """
      await waitFor(() => find('[data-learning-tab="emoji"]'), '可切换到表情');
      click('[data-learning-tab="emoji"]');
      const review = () => find('button[aria-label="表情审批 · 未人工审核 · 合成表情000"]');
      await waitFor(review, '表情卡有审批入口');
      press('小');
      await document.fonts.ready;
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      await waitFor(() => find('#learning-emoji-result')?.dataset.size === 'small' &&
        find('#learning-emoji-result')?.getAttribute('aria-busy') === 'false' && review(),
        '小号缩略图已载入');
      const frame = review().parentElement.getBoundingClientRect();
      const spot = review().getBoundingClientRect();
      check(spot.right <= frame.right && frame.right - spot.right <= 5 &&
        spot.bottom <= frame.bottom && frame.bottom - spot.bottom <= 5 &&
        document.documentElement.scrollWidth <= innerWidth, '小屏右下角内可点击');
      review().focus();
    """,
    )
    for event_type in ("keyDown", "keyUp"):
        browser.command(
            "Input.dispatchKeyEvent",
            type=event_type,
            key="Enter",
            code="Enter",
            text="\r" if event_type == "keyDown" else "",
            windowsVirtualKeyCode=13,
        )
    _run(
        browser,
        """
      const dialog = document.querySelector('#editor-dialog');
      const reject = () => [...dialog.querySelectorAll('button')]
        .find(node => node.textContent === '拒绝审批');
      await waitFor(() => dialog.open && reject(), '键盘直接打开审批而不是详情');
      const original = window.fetch;
      let save, oldPage, writes = 0;
      window.fetch = (url, options) => {
        const path = new URL(url, location.href);
        if (path.pathname.endsWith('/review') && options.method === 'PATCH') {
          check(JSON.parse(options.body).verdict === 'rejected', '发送独立审批结论');
          if (++writes === 1) {
            return Promise.resolve(Response.json({detail:'合成审批失败'}, {status:503}));
          }
          return new Promise(resolve => { save = () => resolve(original(url, options)); });
        }
        if (path.pathname === '/api/resources/media' &&
          Number(path.searchParams.get('offset')) > 0) {
          return new Promise(resolve => { oldPage = resolve; });
        }
        return original(url, options);
      };
      try {
        reject().click();
        await waitFor(() => dialog.innerText.includes('合成审批失败') &&
          !reject().matches(':disabled'),
          '失败保留审批操作，可重试');
        check(!document.querySelector('#toasts').textContent.includes('表情审批：已拒绝'),
          '失败不报告保存成功');
        reject().click();
        await waitFor(() => save && reject().matches(':disabled'), '同一份审批不能重复提交');
        document.querySelector('#editor-close').click();
        press('下一页');
        await waitFor(() => oldPage, '保存未完成时可抢先翻页');
        const filter = find('select[name="review"]');
        filter.value = 'rejected';
        filter.dispatchEvent(new Event('change', {bubbles:true}));
        await waitFor(() => page() === '第 0 / 0 页', '拒绝筛选先返回空结果');
        save();
        await waitFor(() => page() === '第 1 / 1 页' &&
          find('button[aria-label="表情审批 · 已拒绝 · 合成表情000"]'), '保存刷新当前筛选并回显');
        oldPage(Response.json({enabled:true, total:320, items:[{
          text:'迟到的合成表情', media_kind:'emoji', is_registered:true,
        }]}));
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        check(page() === '第 1 / 1 页' && button('已登记 · 合成表情000') && !text('迟到的合成表情'),
          '审批不改登记，迟到的翻页不覆盖筛选');
      } finally { window.fetch = original; }
    """,
    )


def test_emoji_material_preview_and_cancel_on_narrow_screen(browser: Browser) -> None:
    browser.open("learning", 390, 740)
    _run(
        browser,
        """
      await waitFor(() => find('[data-learning-tab="emoji"]'), '表情入口');
      const original = window.fetch;
      let writes = 0;
      window.fetch = async (url, options) => {
        if (url === '/api/resources/emoji/thumbnail-cache') {
          return new Response(JSON.stringify({persistent:false, total:0}));
        }
        if (!url.startsWith('/api/resources/emoji/materials/import?')) {
          return original(url, options);
        }
        check(options.body instanceof File && await options.body.text() === '{"format":"fixture"}',
          '提交文件原字节');
        check(options.headers['Content-Type'] === 'application/json', 'JSON 文件类型');
        if (new URL(url, location.origin).searchParams.has('confirmed')) writes++;
        return new Response(JSON.stringify({digest:'stamp', added:1, registered:0, skipped:0,
          capacity_blocked:0, missing_description:0, members:[{description:'合成图片', bytes:1,
            digest:'short', source_license:'测试 CC0'}]}));
      };
      click('[data-learning-tab="emoji"]');
      await waitFor(() => button('导入素材集') && !button('导入素材集').disabled, '表情列表可用');
      button('缩略图缓存').click();
      const dialog = document.querySelector('#editor-dialog');
      const dialogButton = label => [...dialog.querySelectorAll('button')]
        .find(item => item.textContent.trim() === label);
      await waitFor(() => dialog.innerText.includes('无需预热或清理'), '没有持久缓存');
      document.querySelector('#editor-close').click();
      button('导入素材集').click();
      const input = dialog.querySelector('input[type=file]');
      const files = new DataTransfer();
      files.items.add(new File(['{"format":"fixture"}'], 'fixture.json',
        {type:'application/json'}));
      input.files = files.files;
      input.dispatchEvent(new Event('change', {bubbles:true}));
      dialogButton('预检').click();
      await waitFor(() => dialogButton('确认导入') && dialog.innerText.includes('测试 CC0'),
        '实际成员的许可');
      check(dialog.getBoundingClientRect().right <= innerWidth, '窄屏弹窗不横向溢出');
      dialogButton('取消').click();
      check(writes === 0 && !dialog.open, '取消不提交');
    """,
    )


def test_emoji_integrity_preview_runs_only_on_click_on_narrow_screen(browser: Browser) -> None:
    browser.open("learning", 390, 740)
    _run(
        browser,
        """
      await waitFor(() => find('[data-learning-tab="emoji"]'), '学习页可切换到表情');
      const original = window.fetch;
      let calls = 0;
      let finish;
      window.fetch = (url, options) => {
        if (url !== '/api/resources/emoji/integrity-preview') return original(url, options);
        check(options.method === 'POST' && options.credentials === 'same-origin', '同源 POST 预览');
        calls++;
        return new Promise(resolve => { finish = resolve; });
      };
      click('[data-learning-tab="emoji"]');
      const preview = () => button('完整性预览');
      await waitFor(() => preview() && !preview().disabled, '完整性预览可用');
      check(calls === 0, '打开表情页不检查');
      preview().click();
      check(preview().disabled, '检查中不重复请求');
      preview().click();
      check(calls === 1, '只有一次请求');
      finish(new Response(JSON.stringify({checked:42, unavailable:1, examples:['合成缺图']})));
      const dialog = document.querySelector('#editor-dialog');
      await waitFor(() => dialog.open && dialog.innerText.includes('合成缺图'), '显示预览结果');
      for (const expected of ['检查 42 条', '原图不可用 1 条', '不修改', '只维护发送候选']) {
        check(dialog.innerText.includes(expected), `应说明：${expected}`);
      }
      check(dialog.getBoundingClientRect().right <= innerWidth, '窄屏弹窗不横向溢出');
      document.querySelector('#editor-close').click();
      check(calls === 1, '关闭不请求');
    """,
    )


@pytest.mark.parametrize("outcome", ["enabled", "disabled", "failed"])
def test_emoji_actions_wait_for_current_list_after_navigation(
    browser: Browser, outcome: str
) -> None:
    browser.open("learning", 390, 740)
    _run(
        browser,
        "const outcome = " + json.dumps(outcome) + ";"
        """
      const actions = ['上传表情', '批量上传', '完整性预览'];
      const allDisabled = () => actions.every(label => button(label)?.disabled);
      const allEnabled = () => actions.every(label => button(label)?.disabled === false);
      const go = route => document.querySelector(`.nav-item[data-page="${route}"]`).click();
      await waitFor(() => find('[data-learning-tab="emoji"]'), '学习页可切换到表情');
      click('[data-learning-tab="emoji"]');
      await waitFor(allEnabled, '表情功能开启时可以操作');
      go('memory');
      await waitFor(() => text('合成记忆000'), '相邻文字记忆加载');
      check(!button('批量上传') && button('修正所选').disabled, '相邻记忆不使用表情操作或沿用选择');

      const original = window.fetch;
      const pending = [];
      window.fetch = (url, options) => {
        if (!url.startsWith('/api/resources/media?')) return original(url, options);
        return new Promise(resolve => {
          pending.push(response => resolve(response ?? original(url, options)));
        });
      };
      try {
        go('learning');
        check(allDisabled() && button('下一页').disabled, '返回表情页的当下禁用旧操作');
        go('memory');
        go('learning');
        await waitFor(() => pending.length >= 2, '拦住旧列表和本次列表');
        const counts = {total:320, registered:320, unregistered:0, banned:0, removed:0};
        pending[0](Response.json({enabled:true, total:0, items:[], counts}));
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        check(page() === '读取中…' && allDisabled(), '旧响应不能解除本次加载的禁用');
        let response;
        if (outcome === 'disabled') {
          response = Response.json({enabled:false, total:0, items:[], counts});
        }
        if (outcome === 'failed') response = Response.json({detail:'合成读取失败'}, {status:503});
        window.fetch = original;
        pending.at(-1)(response);
        if (outcome === 'enabled') {
          await waitFor(() => text('合成表情000') && allEnabled(), '仅本次成功读取能启用表情操作');
        } else {
          const message = outcome === 'disabled' ? '此功能当前关闭' : '读取失败';
          await waitFor(() => text(message), '本次读取结束');
          check(allDisabled(), '当前列表关闭或失败时，所有表情操作保持禁用');
        }
      } finally { window.fetch = original; }
    """,
    )


def test_emoji_integrity_completion_does_not_enable_actions_during_list_reload(
    browser: Browser,
) -> None:
    browser.open("learning", 390, 740)
    _run(
        browser,
        """
      await waitFor(() => find('[data-learning-tab="emoji"]'), '学习页可切换到表情');
      click('[data-learning-tab="emoji"]');
      const preview = () => button('完整性预览');
      await waitFor(() => preview()?.disabled === false, '完整性预览可用');
      const original = window.fetch;
      let finishPreview, finishList;
      window.fetch = (url, options) => {
        const path = new URL(url, location.href).pathname;
        if (path === '/api/resources/emoji/integrity-preview') {
          return new Promise(resolve => { finishPreview = resolve; });
        }
        if (path === '/api/resources/media') {
          return new Promise(resolve => { finishList = () => resolve(original(url, options)); });
        }
        return original(url, options);
      };
      try {
        preview().click();
        const dialog = document.querySelector('#editor-dialog');
        await waitFor(() => finishPreview && dialog.open, '预览正在检查');
        document.querySelector('#editor-close').click();
        find('input[type=search]').form.requestSubmit();
        await waitFor(() => finishList, '列表正在重新读取');
        finishPreview(Response.json({checked:1, unavailable:0, examples:[]}));
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        check(page() === '读取中…' && preview().disabled, '旧检查结束不能启用尚未确认的操作');
        window.fetch = original;
        finishList();
        await waitFor(() => text('合成表情000') && !preview().disabled, '新列表读取后可再次检查');
      } finally { window.fetch = original; }
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


def test_emoji_single_upload_can_register_on_narrow_screen(browser: Browser) -> None:
    browser.open("learning", 390, 740)
    with BytesIO() as stream:
        Image.new("RGB", (8, 8), "teal").save(stream, format="PNG")
        image_data = base64.b64encode(stream.getvalue()).decode("ascii")
    _run(
        browser,
        f"""
      await waitFor(() => find('[data-learning-tab="emoji"]'), '学习页可切换到表情');
      click('[data-learning-tab="emoji"]');
      await waitFor(() => button('上传表情') && !button('上传表情').disabled, '表情列表加载');
      press('上传表情');
      const dialog = document.querySelector('#editor-dialog');
      const form = dialog.querySelector('form');
      const register = form.querySelector('input[type="checkbox"]');
      const description = form.querySelector('textarea');
      check(register && !register.checked, '单张默认不登记');
      check(!description.getClientRects().length, '默认不显示描述');
      const input = form.querySelector('input[type="file"]');
      const transfer = new DataTransfer();
      const imageBytes = Uint8Array.from(atob('{image_data}'), byte => byte.charCodeAt(0));
      transfer.items.add(new File([imageBytes], 'fixture.png', {{type:'image/png'}}));
      input.files = transfer.files; input.dispatchEvent(new Event('change', {{bubbles:true}}));
      register.click();
      check(description.getClientRects().length && description.required && !form.checkValidity(),
        '勾选后显示必填描述');
      register.click();
      check(form.checkValidity(), '取消勾选不要求描述');
      register.click();
      description.value = '海兔打招呼 🐰';
      check(form.scrollWidth <= dialog.clientWidth, '窄屏表单不横向溢出');
      form.requestSubmit();
      await waitFor(() => !dialog.open && button('已登记 · 海兔打招呼 🐰'), '直接登记并刷新');
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
      await waitFor(() => text('没有符合条件的记录') && !button('批量上传').disabled,
        '筛选完成后可批量上传');
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
      const enableRegistration = () => {{
        const checkbox = dialog.querySelector('form input[type="checkbox"]');
        check(checkbox && !checkbox.checked, '批次默认不登记');
        checkbox.click();
        dialog.querySelector('form textarea').value = '批次筛选';
      }};
      check(uploadButton().disabled, '尚未选择文件时不可提交');
      choose();
      check(!uploadButton().disabled, '选择文件后可提交');
      const input = dialog.querySelector('input[type="file"]');
      input.value = ''; input.dispatchEvent(new Event('change', {{bubbles:true}}));
      check(uploadButton().disabled, '清空选择后再次不可提交');
      choose();
      check(dialog.innerText.includes('已选择 3 张'), '确认前显示文件数');
      enableRegistration();
      document.querySelector('#editor-close').click();
      check(requests.length === 0, '取消不发上传请求');
      press('批量上传'); choose();
      dialog = document.querySelector('#editor-dialog');
      enableRegistration();
      dialog.querySelector('form').requestSubmit();
      await waitFor(() => requests.length === 1 && dialog.querySelector('fieldset').disabled,
        '第一张上传中禁用操作');
      dialog.querySelector('form').requestSubmit();
      check(requests.length === 1, '重复确认不增加请求');
      release();
      await waitFor(() => dialog.innerText.includes('first.png：已上传并登记') &&
        dialog.innerText.includes('again.png：已存在，保留原有记录') &&
        dialog.innerText.includes('broken.svg：图片格式不支持') &&
        !dialog.querySelector('fieldset').disabled, '逐张保留结果');
      check(requests.length === 3, '每个文件仅上传一次');
      await waitFor(() => countValue() === beforeCount + 1 && button('已登记 · 批次筛选'),
        '刷新全库统计');
      check(search.value === '批次筛选' && button('已登记 · 批次筛选'),
        '筛选保留且登记后匹配批次描述');
      window.fetch = nativeFetch;
    """,
    )


def test_emoji_batch_upload_paginates_per_image_descriptions(browser: Browser) -> None:
    browser.open("learning", 390, 740)
    images = []
    for color in ("green", "red", "blue", "purple"):
        with BytesIO() as stream:
            Image.new("RGB", (8, 8), color).save(stream, format="PNG")
            images.append(base64.b64encode(stream.getvalue()).decode("ascii"))
    _run(
        browser,
        f"""
      await waitFor(() => find('[data-learning-tab="emoji"]'), '可切换到表情');
      click('[data-learning-tab="emoji"]');
      await waitFor(() => button('批量上传') && !button('批量上传').disabled, '表情列表加载');
      press('批量上传');
      const dialog = document.querySelector('#editor-dialog');
      const form = dialog.querySelector('form');
      const input = form.querySelector('input[type="file"]');
      const register = form.querySelector('input[type="checkbox"]');
      const requests = [];
      const nativeFetch = window.fetch;
      window.fetch = async (url, options) => {{
        if (url === '/api/resources/emoji' && options.method === 'POST') {{
          requests.push([options.body.name, decodeURIComponent(new Headers(options.headers)
            .get('X-Emoji-Registration-Description') || '')]);
          if (options.body.name === 'failed.png')
            return new Response(JSON.stringify({{detail:'合成上传失败'}}),
              {{status:422, headers:{{'Content-Type':'application/json'}}}});
        }}
        return nativeFetch(url, options);
      }};
      const transfer = new DataTransfer();
      const names = ['same.png', 'same.png', 'failed.png', 'long-'.repeat(40) + '.png'];
      {json.dumps(images)}.forEach((data, index) => {{
        const bytes = Uint8Array.from(atob(data), byte => byte.charCodeAt(0));
        transfer.items.add(new File([bytes], names[index], {{type:'image/png'}}));
      }});
      input.files = transfer.files;
      input.dispatchEvent(new Event('change', {{bubbles:true}}));
      const nav = () => form.querySelector('[aria-label="批量上传分页"]');
      const status = () => nav()?.querySelector('[role="status"]').textContent;
      const dialogButton = label => [...form.querySelectorAll('button')]
        .find(node => node.textContent === label);
      const jump = nav().querySelector('.resource-page-jump');
      const descriptions = () => [...form.querySelectorAll('textarea')].slice(1);
      const fill = (control, text) => {{
        control.value = text;
        control.dispatchEvent(new Event('input', {{bubbles:true}}));
      }};
      check(status() === '第 1 / 2 页' && dialogButton('上一页').disabled, '选择后分页显示总页数');
      check(!register.checked && !descriptions().length, '默认不登记也不显示逐图描述');
      register.click();
      check(descriptions().length === 3, '每页最多编辑三张');
      fill(descriptions()[0], '青兔打招呼 🐰');
      fill(descriptions()[1], '红兔道谢');
      fill(descriptions()[2], '测试失败项');
      form.requestSubmit();
      check(requests.length === 0 && status() === '第 2 / 2 页', '跨页缺描述时定位且零上传');
      check(dialogButton('下一页').disabled && descriptions().length === 1 &&
        form.scrollWidth <= dialog.clientWidth, '窄屏长文件名不撑宽，末页只有一张');
      fill(form.querySelector('textarea'), '共享缺省');
      fill(jump, '1');
      jump.dispatchEvent(new KeyboardEvent('keydown', {{key:'Enter', cancelable:true}}));
      check(descriptions()[0].value === '青兔打招呼 🐰' && descriptions()[1].value === '红兔道谢',
        '跳页不提交上传表单，保留重名文件各自的草稿');
      check(requests.length === 0, '跳页不上传任何图片');
      register.click();
      check(!descriptions().length && !form.querySelector('textarea').getClientRects().length,
        '取消勾选完全隐藏输入');
      register.click();
      check(descriptions()[0].value === '青兔打招呼 🐰', '再勾选恢复草稿');
      form.requestSubmit();
      await waitFor(() => !form.querySelector('fieldset').disabled &&
        dialog.innerText.includes('合成上传失败'), '逐图显示失败原因');
      check(JSON.stringify(requests) === JSON.stringify([
        [names[0], '青兔打招呼 🐰'], [names[1], '红兔道谢'],
        [names[2], '测试失败项'], [names[3], '共享缺省'],
      ]), '描述按文件位置发送，失败不阻断后一张');
      check(dialog.innerText.includes('成功 3') && dialog.innerText.includes('失败 1'),
        '批次显示全局汇总');
      dialogButton('下一页').click();
      check(status() === '第 2 / 2 页' && dialog.innerText.includes('已上传并登记') &&
        !dialog.innerText.includes('合成上传失败'), '结果也分页显示');
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


def test_resource_pagination_applies_latest_navigation_intent(browser: Browser) -> None:
    browser.open("memory", 390, 740)
    _run(
        browser,
        r"""
      await waitFor(() => text('合成记忆000') && !button('下一页')?.disabled, '文字记忆首页');
      await document.fonts.ready;
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      await waitFor(() => text('合成记忆000') && !button('下一页')?.disabled,
        '尺寸确定后的文字记忆首页');
      const jump = find('.resource-page-jump');
      const list = find('#memory-result');
      const next = () => button('下一页');
      const previous = () => button('上一页');
      const totalPages = Number(page().match(/\/ (\d+)/)[1]);
      const pageHeight = list.clientHeight;
      const queue = [];
      const original = window.fetch;
      window.fetch = (url, options) => {
        const path = new URL(url, location.href);
        if (path.pathname !== '/api/resources/memory' || options.method !== 'GET')
          return original(url, options);
        return new Promise(resolve => {
          queue.push({params:path.searchParams, signal:options.signal, resolve});
        });
      };
      const send = (index, title, total = 320) => queue[index].resolve(Response.json({
        enabled:true, total, counts:{paragraphs:total},
        items:title ? [{text:title, is_recallable:true}] : [],
      }));
      const fail = (index, detail) => queue[index].resolve(Response.json({detail}, {status:503}));
      const settle = () => new Promise(resolve => {
        requestAnimationFrame(() => requestAnimationFrame(resolve));
      });
      const submitJump = value => {
        jump.value = String(value);
        jump.dispatchEvent(new Event('input', {bubbles:true}));
        jump.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', cancelable:true}));
      };
      const target = index => {
        const params = queue[index].params;
        return Number(params.get('offset')) / Number(params.get('limit')) + 1;
      };
      try {
        check(jump.getAttribute('aria-label')?.includes('跳转页码') && !jump.disabled,
          '有名称的可用跳页输入');
        jump.focus();
        check(document.activeElement === jump, '可以键盘聚焦');
        submitJump(totalPages + 1);
        submitJump('1.5');
        check(queue.length === 0 && !jump.validity.valid, '拒绝越界和小数页码');
        click('#memory-select-all');
        check(!find('#memory-correct').disabled, '当前页原本有选中项');
        press('下一页');
        check(target(0) === 2 && !next().disabled && !previous().disabled && !jump.disabled &&
          list.getAttribute('aria-busy') === 'true', '首个翻页开始后仍可选择下一页');
        check(find('#memory-correct').disabled && find('#memory-select-all').disabled,
          '发出新翻页意图立即撤销旧批量选择');
        press('下一页');
        check(target(1) === 3 && queue[0].signal.aborted, '第二次下一页以目标第二页为基准');
        submitJump(totalPages);
        check(target(2) === totalPages && next().disabled && !previous().disabled,
          '读取中直接跳到末页，立即应用边界');
        check(list.clientHeight === pageHeight, '读取中清除旧计数但保留列表高度');
        send(2, '最新末页');
        await waitFor(() => text('最新末页') && page() === `第 ${totalPages} / ${totalPages} 页`,
          '末页完成');
        send(0, '迟到的第二页');
        fail(1, '迟到的第三页错误');
        await settle();
        check(text('最新末页') && !text('迟到的第二页') && !text('读取失败') &&
          !document.querySelector('#toasts').textContent.includes('迟到的第三页错误'),
          '迟到成功或失败都不污染新页');
        check(!find('#memory-select-all').checked && find('#memory-correct').disabled,
          '新页不会继承批量选择');

        press('上一页');
        submitJump(1);
        check(target(3) === totalPages - 1 && target(4) === 1 && previous().disabled,
          '反向翻页后可跳回首页且不能越界');
        fail(4, '当前首页失败');
        await waitFor(() => button('重试') && text('读取失败'), '最新失败显示重试');
        send(3, '迟到的上一页');
        await settle();
        check(!text('迟到的上一页') && button('重试') && jump.disabled, '迟到成功不遮盖最新失败');
        press('重试');
        check(target(5) === 1, '重试最新失败的目标页');
        send(5, '恢复的首页');
        await waitFor(() => text('恢复的首页'), '重试首页完成');

        submitJump(totalPages);
        send(6, null, 1);
        await waitFor(() => queue[7], '资料缩减重新读取最后一个有效页');
        check(target(7) === 1, '资料缩减不留在越界页');
        send(7, '缩减后的唯一资料', 1);
        await waitFor(() => page() === '第 1 / 1 页' && text('缩减后的唯一资料'),
          '新总数与有效页相符');
        check(previous().disabled && next().disabled && jump.disabled &&
          document.documentElement.scrollWidth === 390, '单页禁用导航且窄屏不溢出');
      } finally { window.fetch = original; }
    """,
    )


@pytest.mark.parametrize(
    ("route", "endpoint", "label"),
    [("memory", "/api/resources/memory", "记忆"), ("learning", "/api/resources/media", "表情")],
)
def test_resource_pagination_clears_failed_reload_and_retries_same_page(
    browser: Browser, route: str, endpoint: str, label: str
) -> None:
    browser.open(route, 390, 740)
    args = json.dumps({"route": route, "endpoint": endpoint, "prefix": f"合成{label}"})
    _run(
        browser,
        "const {route, endpoint, prefix} = " + args + ";"
        """
      const next = () => button('下一页');
      const input = () => find('input[type=search]');
      const go = name => document.querySelector(`.nav-item[data-page="${name}"]`).click();
      const applied = prefix + '0';
      const draft = '合成草稿尚未提交';
      if (route === 'learning') {
        await waitFor(() => find('[data-learning-tab="emoji"]'), '学习页已打开');
        click('[data-learning-tab="emoji"]');
      }
      await waitFor(() => innerWidth === 390 && innerHeight === 740 && next()?.disabled === false,
        '窄屏列表加载');
      input().value = applied;
      input().form.requestSubmit();
      await waitFor(() => text(prefix + '000') && next()?.disabled === false, '已提交筛选');
      press('下一页');
      await waitFor(() => page()?.startsWith('第 2 /') && next()?.disabled === false,
        '非末尾第二页');
      input().value = draft;
      const original = window.fetch;
      const pending = [];
      window.fetch = (url, options) => {
        const parsed = new URL(url, location.href);
        if (parsed.pathname !== endpoint) return original(url, options);
        return new Promise(resolve => pending.push({params:parsed.searchParams,
          respond:response => resolve(response ?? original(url, options))}));
      };
      const reload = async index => {
        go(route === 'memory' ? 'learning' : 'memory');
        go(route);
        await waitFor(() => pending[index], '返回后重新查询');
        check(page() === '读取中…' && next().disabled && button('上一页').disabled,
          '等待时禁用分页');
        const request = pending[index];
        check(request.params.get('q') === applied && Number(request.params.get('offset')) > 0,
          '返回读取已提交筛选的第二页');
        request.respond(Response.json({detail:'合成列表错误'}, {status:503}));
        await waitFor(() => button('重试') && text('读取失败'), '明确显示读取失败和重试');
        const pager = root().querySelector('.resource-pagination');
        check(!page() && [...pager.querySelectorAll('button')].every(control => control.disabled),
          '失败后不得展示旧总页数或启用翻页');
        check(!text(prefix) && !text('没有符合条件的记录') && input().value === draft,
          '失败不装作空结果，也不改草稿');
        press('重试');
        await waitFor(() => pending[index + 1], '重试发出同一页读取');
        const retry = pending[index + 1];
        check(retry.params.toString() === request.params.toString(), '重试沿用页码及已提交筛选');
        return retry;
      };
      try {
        const retry = await reload(0);
        retry.respond();
        const expected = Math.ceil(100 / Number(retry.params.get('limit')));
        await waitFor(() => page() === `第 2 / ${expected} 页` && text(prefix) &&
          !button('上一页').disabled && !next().disabled, '重试后恢复第二页及正确总数');
        check(input().value === draft, '重试成功不改草稿');

        const late = await reload(2);
        input().value = prefix + '002';
        input().form.requestSubmit();
        await waitFor(() => pending[4], '新筛选提交后重新读取');
        check(pending[4].params.get('offset') === '0', '新筛选从第一页查询');
        input().value = '另一段合成草稿';
        pending[4].respond();
        await waitFor(() => page() === '第 1 / 1 页' && text(prefix + '002'), '新筛选的一条结果');
        late.respond(Response.json({enabled:true,total:320,items:[{
          entry_id:'old-page', text:'迟到的旧页', media_kind:'emoji', is_registered:true,
        }],next_offset:100}));
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        check(page() === '第 1 / 1 页' && text(prefix + '002') && !text('迟到的旧页') &&
          input().value === '另一段合成草稿',
          '晚到的旧页不改变当前页、资料或草稿');
      } finally { window.fetch = original; }
    """,
    )


def test_memory_inspection_shows_counts_and_limits(browser: Browser) -> None:
    browser.open("memory", 390, 740)
    _run(
        browser,
        r"""
      await waitFor(() => text('合成记忆000'), '文字记忆有查询结果');
      press('记忆检修');
      press('只读检修');
      const dialog = document.querySelector('#editor-dialog');
      await waitFor(() => dialog.open && dialog.innerText.includes('维护停用：1 条'), '检修分项');
      check(dialog.innerText.includes('已检查 3 条') && dialog.innerText.includes('已移除：0 条') &&
        dialog.innerText.includes('可见标签不一致：1 条'), '显示已检查与各项计数');
      check(dialog.innerText.includes('原件缺失未检查') &&
        dialog.innerText.includes('零异常不代表全库完整'), '显示报告边界');
      check(!dialog.innerText.includes('合成记忆000'), '报告不显示原件正文');
      document.querySelector('#editor-close').click();
      check(!dialog.open && button('维护文字记录'), '关闭报告仍在记忆检修');
      press('维护文字记录');
      await waitFor(() => text('合成记忆000'), '返回原文字查询');
    """,
    )


def test_memory_workbench_preserves_query_page_and_drafts_on_narrow_screen(
    browser: Browser,
) -> None:
    browser.open("memory", 390, 740)
    _run(
        browser,
        r"""
      await waitFor(() => text('合成记忆000'), '记忆查询就绪');
      const names = label => [...find(`[aria-label="${label}"]`).querySelectorAll('button')]
        .map(node => node.textContent).join('|');
      check(names('记忆浏览') === '记忆查询|图片记忆|记忆流', '浏览导航顺序');
      check(names('记忆管理') === '导入导出|记忆检修|记忆移除', '管理导航顺序');
      check(find('[data-memory-query="graph"]').closest('[aria-label="查询内容"]'),
        '关系图只在查询中');
      check(!find('#memory-search select').querySelector('[value="messages"]'),
        '查询不提供原始消息类别');
      check(!button('只读检修'), '查询不混放检修');
      const search = find('#memory-search [name="q"]');
      const requests = [];
      const fetchOriginal = window.fetch;
      window.fetch = (url, options) => {
        const path = new URL(url, location.origin);
        if (path.pathname === '/api/resources/memory' && options.method === 'GET') {
          requests.push(path.searchParams);
        }
        return fetchOriginal(url, options);
      };
      press('下一页');
      await waitFor(() => page().startsWith('第 2 /') && text('合成记忆'), '查询第二页就绪');
      const offset = requests.at(-1).get('offset');
      search.value = '未提交的查询草稿';
      press('记忆移除');
      check(text('不永久删除原件') && text('操作记录') && !button('清空'), '明确可逆边界');
      press('选择文字记忆');
      check(document.activeElement === search && search.value === '未提交的查询草稿',
        '快捷入口恢复焦点与草稿');
      await waitFor(() => page().startsWith('第 2 /') && text('合成记忆'), '返回原来的列表页');
      check(requests.at(-1).get('offset') === offset && requests.at(-1).get('q') === '',
        '不应用搜索草稿');
      press('记忆检修');
      press('维护人物别名');
      await waitFor(() => text('合成人物000'), '别名入口通往真实人物记录');
      check(document.activeElement === find('#memory-query-profiles input[type="search"]'),
        '焦点进入人物查询');
      check(root().scrollWidth <= innerWidth, '窄屏不横向溢出');
      press('记忆检修');
      click('a[href="#inbox"]');
      await waitFor(() => root()?.id === 'page-inbox', '原始聊天入口交给平台');
    """,
    )


def test_memory_package_preview_requires_current_file_confirmation_at_390px(
    browser: Browser,
) -> None:
    browser.open("memory", 390, 740)
    _run(
        browser,
        """
      const transferPath = '/api/resources/memory/transfer';
      const requests = [];
      const imports = [];
      const upstreamFetch = window.fetch;
      window.fetch = async (path, options) => {
        if (path === transferPath + '/preview') {
          return new Promise(resolve => requests.push({file:options.body, resolve}));
        }
        if (path === transferPath) {
          imports.push(options.body);
          return Response.json({added:1, skipped:1});
        }
        return upstreamFetch(path, options);
      };
      const summary = (total, counts = {}) => Response.json({version:1, total, counts});
      await waitFor(() => find('[data-memory-tab="transfer"]'), '长期记忆页就绪');
      click('[data-memory-tab="transfer"]');
      const input = find('input[type="file"]');
      const confirm = () => button('确认追加导入');
      const select = (name, body) => {
        const selection = new DataTransfer();
        selection.items.add(new File([body], name));
        input.files = selection.files;
        input.dispatchEvent(new Event('change', {bubbles:true}));
      };
      select('原来的包.jsonl', '第一份文件');
      await waitFor(() => requests.length === 1 && text('正在检视'), '选文件后开始检视');
      check(confirm().disabled && !imports.length, '等待检视时不可导入');
      press('取消');
      requests[0].resolve(summary(99));
      await waitFor(() => text('已取消'), '取消可见');
      check(!input.files.length && confirm().disabled, '取消后清空选件');

      select('拖延的包.jsonl', '第二份文件');
      await waitFor(() => requests.length === 2, '第二份检视开始');
      select('有坏尾的包.jsonl', '第三份文件');
      await waitFor(() => requests.length === 3, '更换文件重新检视');
      requests[2].resolve(Response.json({detail:'记忆包记录数或结束行有误。'}, {status:422}));
      await waitFor(() => text('结束行有误'), '坏包提示');
      requests[1].resolve(summary(77));
      await new Promise(resolve => setTimeout(resolve, 30));
      check(!text('77 条') && !text('99 条') && confirm().disabled, '迟到结果不能覆盖取消或坏包');
      check(imports.length === 0, '没有写请求');

      select('完整的包.jsonl', '确认后必须导入这份文件');
      await waitFor(() => requests.length === 4, '完整包检视开始');
      requests[3].resolve(summary(2, {messages:1, paragraphs:1}));
      await waitFor(() => !confirm().disabled, '检视完成才可确认');
      check(text('版本 1') && text('包内共 2 条') && text('不是预计新增') &&
        text('消息 1 条') && text('段落 1 条'), '显示包内版本、分类与计数边界');
      check(document.documentElement.scrollWidth === 390 && !button('下一页'),
        '390 px 无横向溢出或长列表分页');
      check(imports.length === 0, '检视成功也不自动导入');

      select('实际要导入的包.jsonl', '确认使用这一份');
      await waitFor(() => requests.length === 5, '换文件清除已通过的报告');
      check(!text('包内共 2 条') && confirm().disabled, '旧报告不能确认新文件');
      requests[4].resolve(summary(2, {messages:2}));
      await waitFor(() => !confirm().disabled, '新报告可以取消');
      input.dispatchEvent(new Event('cancel'));
      check(confirm().disabled && !input.files.length && !imports.length, '取消文件选择清空确认');

      select('实际要导入的包.jsonl', '确认使用这一份');
      await waitFor(() => requests.length === 6, '再次选择需要重新检视');
      requests[5].resolve(summary(2, {messages:2}));
      await waitFor(() => !confirm().disabled, '确认当前包');
      press('确认追加导入');
      await waitFor(() => text('新增 1 条，跳过相同记录 1 条'), '实际结果与检视计数分开');
      check(imports.length === 1 && imports[0] === requests[5].file &&
        await imports[0].text() === '确认使用这一份', '只上传刚通过检视的文件');
      check(confirm().disabled && !input.files.length, '成功后不能再次确认旧包');
    """,
    )


def test_memory_package_browser_confirm_uses_real_transfer(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    source = MemoryIndex(tmp_path / "source.db", rig.timeline, rig.clock)
    target = MemoryIndex(tmp_path / "aureus.db", rig.timeline, rig.clock)
    original = HistoricalMemory(MemorySource("test", "memory", "episodes", (1,)), "合成原件")
    source.open()
    target.open()
    try:
        set_password(rig)
        source.publish([original])
        package = BytesIO()
        source.export_archive(package)
        payload = json.dumps(package.getvalue().decode("utf-8"))
        app = create_app(
            config=rig.config,
            auth=rig.auth,
            sessions=rig.sessions,
            channel=WebChannel(FakeHeartbeat()),
            timeline=rig.timeline,
            resource_memory=target,
        )
        with (
            TestClient(app) as client,
            TemporaryDirectory(prefix="aureus-package-chrome-") as directory,
            _open_browser(Path(directory), client) as browser_instance,
        ):
            client.cookies.set(SESSION_COOKIE, rig.sessions.create())
            browser_instance.open("memory", 390, 740)
            _run(
                browser_instance,
                f"""
              await waitFor(() => find('[data-memory-tab="transfer"]'), '长期记忆页');
              click('[data-memory-tab="transfer"]');
              const input = find('input[type="file"]');
              const selection = new DataTransfer();
              selection.items.add(new File([{payload}], '合成记忆包.jsonl'));
              input.files = selection.files;
              input.dispatchEvent(new Event('change', {{bubbles:true}}));
              await waitFor(() => text('情景记忆 1 条'), '真实检视接口回报');
              check(!text('合成原件'), '不回显正文');
            """,
            )
            assert target.get_history(original.source) is None
            _run(
                browser_instance,
                """
              press('确认追加导入');
              await waitFor(() => text('新增 1 条，跳过相同记录 0 条'), '真实导入结果');
            """,
            )
            assert target.get_history(original.source) == original
    finally:
        target.close()
        source.close()
        rig.close()


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
      press('记忆移除');
      await waitFor(() => text('没有可见的移除或恢复记录') && page() === '第 0 / 0 页', '空态');
      press('记忆查询');
      mode = 'disabled';
      press('记忆移除');
      await waitFor(() => text('此功能当前关闭'), '关闭状态');
      press('记忆查询');
      mode = 'error';
      press('记忆移除');
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
      const sameName = {...entity(2), name:entity(1).name};
      const link = (index, predicate, subject, object) => ({kind:'relation',
        source:`["合成","memory","relations",[${index}]]`, subject, object, predicate,
        confidence:0.7, evidence:[]});
      const relation = link(1, '观察', entity(0), entity(1));
      const parallel = link(2, '<b>帮助</b>', entity(0), entity(1));
      const loop = link(3, '复盘', entity(1), entity(1));
      const records = [entity(0), entity(1), sameName, relation, parallel, loop];
      let granted = true, size = 0, delay = '', resume;
      const realFetch = window.fetch;
      window.fetch = (resource, options) => {
        const url = new URL(resource, location.origin);
        if (![path, path + '/access', path + '/detail'].includes(url.pathname)) {
          return realFetch(resource, options);
        }
        const params = url.searchParams;
        let result, status = 200;
        if (url.pathname.endsWith('/access')) {
          result = {};
        } else if (url.pathname.endsWith('/detail')) {
          const item = granted && records.find(item => item.source === params.get('source'));
          result = item || {detail:'找不到这条图谱资料。'};
          status = item ? 200 : 404;
        } else {
          const offset = Number(params.get('offset'));
          size = Number(params.get('limit'));
          const neighbor = params.has('neighbor_of');
          const total = !granted || params.get('q') ? 0 : neighbor ? 1 : size * 2 + 1;
          result = {covered:granted, total,
            next_offset:offset + size < total ? offset + size : null,
            items:Array.from({length:Math.max(0, Math.min(size, total - offset))},
              (_, index) => neighbor ? relation : records[offset + index] ?? entity(offset + index)
            )};
        }
        const response = Response.json(result, {
          status, headers:{'X-Aureus-Graph-Revision':granted ? 'open' : 'closed'},
        });
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
      const canvas = panel.querySelector('.memory-graph-viewport svg');
      const graphNodes = [...canvas.querySelectorAll('.memory-graph-node')];
      const graphEdges = [...canvas.querySelectorAll('.memory-graph-edge')];
      check(graphNodes.length === size - 3 && graphEdges.length === 3, '保留孤点、自环和不同谓词');
      const identities = graphNodes.filter(node =>
        node.getAttribute('aria-label').includes('合成图谱实体1（'));
      check(identities.length === 2 && identities.every(node => node.tabIndex === 0),
        '同名实体分别可定位');
      check(new Set(graphEdges.map(edge =>
        edge.querySelector('path').getAttribute('d'))).size === 3,
        '平行边和自环路径不同');
      check(graphEdges.every(edge => edge.querySelector('path').hasAttribute('marker-end')),
        '每条边标出方向');
      identities[0].focus();
      check(graphEdges.some(edge => edge.classList.contains('is-neighbor'))
        && graphNodes.some(node => node.classList.contains('is-muted')), '键盘定位会突出邻接');
      const helping = graphEdges.find(edge =>
        edge.getAttribute('aria-label').includes('<b>帮助</b>'));
      helping.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter'}));
      await waitFor(() => document.querySelector('#editor-dialog').open, '键盘打开具体关系详情');
      check(document.querySelector('[data-graph-detail]').textContent.includes('<b>帮助</b>')
        && !document.querySelector('[data-graph-detail] b'), '关系谓词按文本显示');
      document.querySelector('#editor-dialog').close();
      const originalWidth = canvas.style.width;
      press('放大');
      check(canvas.style.width !== originalWidth, '可缩放');
      press('复位');
      check(canvas.style.width === originalWidth, '可复位');
      press('隐藏关系图');
      check(!canvas.getClientRects().length && panel.querySelector('.resource-record'),
        '不用图也能浏览');
      press('显示关系图');
      const height = panel.scrollHeight;
      delay = path;
      press('下一页');
      await waitFor(() => resume, '延迟第二页');
      const oldPage = resume;
      resume = null;
      const jump = panel.querySelector('.resource-page-jump');
      jump.value = '3';
      jump.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter'}));
      await waitFor(() => page() === '第 3 / 3 页', '末页');
      check(button('下一页').disabled && panel.scrollHeight <= height + 1, '末页不接成长列表');
      oldPage();
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      check(page() === '第 3 / 3 页' && !text(`合成图谱实体${size}`), '较早页不得覆盖跳页结果');
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
      press('文字记录');
      await waitFor(() => text('合成记忆000'), '离开图谱');
      press('关系图');
      await waitFor(() => text('当前没有获准的图谱资料'), '撤权后无覆盖');
      oldDetail();
      oldList();
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      check(!panel.textContent.includes('合成图谱实体'), '迟到列表不得重现旧资料');
      check(!document.querySelector('#editor-dialog').open, '迟到详情不得重开对话框');
      check(text('当前没有获准的图谱资料') && page() === '第 0 / 0 页', '撤权后保留正确空态');
      check(!canvas.childElementCount, '撤权清除 SVG 节点和边');
      granted = true;
      press('刷新');
      await waitFor(() => canvas.querySelector('.memory-graph-node'), '重新查询获准图谱');
      press('实体 · 合成图谱实体0');
      await waitFor(() => document.querySelector('#editor-dialog').open, '撤权前已有详情');
      granted = false;
      window.dispatchEvent(new Event('focus'));
      await waitFor(() => text('当前没有获准的图谱资料'), '活动图谱遇到授权变化');
      check(!canvas.childElementCount && !document.querySelector('#editor-dialog').open,
        '撤权清除当前画布和详情');
    """,
    )


def test_memory_graph_drag_selects_node_and_opens_its_detail_at_390px(browser: Browser) -> None:
    browser.open("memory", 390, 740)
    _run(
        browser,
        r"""
      await waitFor(() => text('合成记忆000'), '文字记忆初始查询');
      const path = '/api/resources/memory/graph';
      const entity = index => ({kind:'entity', source:`["合成","memory","entities",[${index}]]`,
        name:'同名拖动实体', evidence:[]});
      const link = (index, predicate) => ({kind:'relation',
        source:`["合成","memory","relations",[${index}]]`, subject:entity(0), object:entity(1),
        predicate, confidence:0.5, evidence:[]});
      const records = [entity(0), entity(1), link(1, '合成谓词甲'), link(2, '合成谓词乙')];
      window.graphDetails = [];
      const realFetch = window.fetch;
      window.fetch = (resource, options) => {
        const url = new URL(resource, location.origin);
        const headers = {'X-Aureus-Graph-Revision':'open'};
        if (url.pathname === path + '/access') return Promise.resolve(Response.json({}, {headers}));
        if (url.pathname === path + '/detail') {
          const source = url.searchParams.get('source');
          window.graphDetails.push(source);
          const item = records.find(item => item.source === source);
          return Promise.resolve(Response.json(item, {headers}));
        }
        if (url.pathname === path) {
          return Promise.resolve(Response.json({covered:true, items:records,
            total:records.length, next_offset:null}, {headers}));
        }
        return realFetch(resource, options);
      };
      press('关系图');
      await waitFor(() => find('.memory-graph-node'), '图谱绘出');
      const canvas = find('#memory-panel-graph .memory-graph-viewport svg');
      const edges = [...canvas.querySelectorAll('.memory-graph-edge')];
      for (const predicate of ['合成谓词甲', '合成谓词乙']) {
        edges.find(edge => edge.getAttribute('aria-label').includes(predicate))
          .dispatchEvent(new KeyboardEvent('keydown', {key:'Enter'}));
        await waitFor(() => document.querySelector('[data-graph-detail]')?.textContent
          .includes(predicate), '同一有向端点的每个谓词都能单独打开');
        document.querySelector('#editor-dialog').close();
      }
      const target = canvas.querySelectorAll('.memory-graph-node')[1];
      target.scrollIntoView({block:'center', inline:'center'});
      window.graphDrag = {before:target.getAttribute('transform')};
    """,
    )
    box = browser.evaluate(
        "(() => { const rect = document.querySelectorAll('.page.active .memory-graph-node')[1]"
        ".querySelector('circle').getBoundingClientRect();"
        " return [rect.x + rect.width / 2, rect.y + rect.height / 2]; })()"
    )
    assert isinstance(box, list)
    x, y = box
    for event_type, offset in (("mousePressed", 0), ("mouseMoved", 30), ("mouseReleased", 30)):
        browser.command(
            "Input.dispatchMouseEvent",
            type=event_type,
            x=x + offset,
            y=y + offset,
            button="left",
            buttons=0 if event_type == "mouseReleased" else 1,
            clickCount=1,
        )
    _run(
        browser,
        r"""
      const nodes = [...find('#memory-panel-graph svg').querySelectorAll('.memory-graph-node')];
      const target = nodes[1];
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      check(target.getAttribute('transform') !== window.graphDrag.before, '拖动改变节点位置');
      check(document.activeElement === target && target.classList.contains('is-selected'),
        '拖动结束选中并聚焦该节点');
      check(!document.querySelector('#editor-dialog').open && window.graphDetails.length === 2,
        '拖动本身不误开详情');
      target.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter'}));
      await waitFor(() => document.querySelector('#editor-dialog').open, '拖选后打开详情');
      check(window.graphDetails.at(-1) === '["合成","memory","entities",[1]]',
        '同名实体中打开的正是被拖选的那个');
      document.querySelector('#editor-dialog').close();
      press('复位');
      check(target.getAttribute('transform') === window.graphDrag.before
        && !nodes.some(node => node.classList.contains('is-selected')), '复位回到全图初始布局');
    """,
    )


def test_memory_graph_grant_editor_and_late_response_at_390px(browser: Browser) -> None:
    browser.open("memory", 390, 740)
    _run(
        browser,
        r"""
      await waitFor(() => text('合成记忆000'), '记忆初始查询');
      const path = '/api/resources/memory/graph';
      const endpoint = path + '/grants';
      const source = '["合成","memory","entities",[1]]';
      const item = {kind:'entity', source, name:'获准合成节点', evidence:[]};
      const realFetch = window.fetch;
      let categories = [], generation = 0, pauseDetail = false, releaseDetail, releaseRevoke;
      let pausePage = false, releasePage;
      window.fetch = (resource, options) => {
        const {pathname} = new URL(resource, location.origin);
        if (pathname === endpoint) {
          const batch = {batch_id:'batch', instance:'<b>合成批次</b>', categories};
          return Promise.resolve(Response.json({enabled:true, items:[batch]}));
        }
        if (pathname === endpoint + '/batch') {
          const body = JSON.parse(options.body);
          check(body.password === 'synthetic', '写操作提交复验密码');
          check(Object.keys(body).length === 2, '不提交节点或名称');
          const apply = () => {
            categories = body.categories;
            generation++;
            return Response.json({});
          };
          if (body.categories.length) return Promise.resolve(apply());
          return new Promise(resolve => { releaseRevoke = () => resolve(apply()); });
        }
        const headers = {'X-Aureus-Graph-Revision':String(generation)};
        const covered = categories.includes('entities');
        if (pathname === path) {
          const result = {
            covered, items:covered ? [item] : [], total:Number(covered), next_offset:null,
          };
          const response = Response.json(result, {headers});
          if (pausePage) return new Promise(resolve => { releasePage = () => resolve(response); });
          return Promise.resolve(response);
        }
        if (pathname === path + '/access') return Promise.resolve(Response.json({}, {headers}));
        if (pathname === path + '/detail') {
          const response = Response.json(item, {headers});
          if (pauseDetail) {
            return new Promise(resolve => { releaseDetail = () => resolve(response); });
          }
          return Promise.resolve(response);
        }
        return realFetch(resource, options);
      };
      press('关系图');
      await waitFor(() => text('当前没有获准的图谱资料'), '关闭态不显示图');
      const panel = find('#memory-panel-graph');
      check(!panel.querySelector('.memory-graph-node, .memory-graph-edge'), '空授权不绘制任何节点');
      panel.querySelector('summary').click();
      await waitFor(() => button('保存展示授权'), '真实管理表单');
      const legend = panel.querySelector('legend');
      check(legend.textContent === '<b>合成批次</b>' && !legend.querySelector('b'),
        '批次名是纯文本');
      check(!panel.querySelector('input[type=checkbox]:checked'), '各类授权默认不勾选');
      panel.querySelector('input[value=entities]').click();
      panel.querySelector('input[type=password]').value = 'synthetic';
      press('保存展示授权');
      const canRevoke = () => button('撤回本批授权') && !button('撤回本批授权').disabled;
      await waitFor(() => text('获准合成节点') && canRevoke(), '保存后查询新的范围');
      check(!panel.querySelector('input[type=password]').value, '成功后清除密码');
      check(root().scrollWidth <= innerWidth, '展开管理操作后窄屏不溢出');

      pauseDetail = true;
      press('实体 · 获准合成节点');
      await waitFor(() => releaseDetail, '旧详情尚在途中');
      panel.querySelector('input[type=password]').value = 'synthetic';
      press('撤回本批授权');
      await waitFor(() => releaseRevoke, '撤权请求在途');
      check(!text('获准合成节点') && !panel.querySelector('.memory-graph-node'),
        '撤回开始即移除旧图');
      releaseDetail();
      releaseRevoke();
      await waitFor(() => text('当前没有获准的图谱资料'), '撤回后不显示旧图');
      check(!document.querySelector('#editor-dialog').open && !text('获准合成节点'),
        '迟到详情不能重开');

      categories = ['entities'];
      generation++;
      pausePage = true;
      press('刷新');
      await waitFor(() => releasePage, '另一会话撤回前，旧列表已从服务端返回');
      categories = [];
      generation++;
      pausePage = false;
      releasePage();
      await waitFor(() => text('读取失败'), '活动页复核后丢弃旧列表');
      check(!text('获准合成节点') && !panel.querySelector('.memory-graph-node'),
        '不得渲染失效范围');
    """,
    )


def test_expression_clusters_paginate_and_discard_stale_results_at_390px(browser: Browser) -> None:
    browser.open("learning", 390, 740)
    _run(
        browser,
        r"""
      await waitFor(() => find('#learning-expression-list')?.querySelector('.resource-record'),
        '学习页就绪');
      const path = '/api/resources/learning/clusters';
      let granted = true, size = 0, delay = '', resume;
      const realFetch = window.fetch;
      window.fetch = (resource, options) => {
        const url = new URL(resource, location.origin);
        if (![path, path + '/detail'].includes(url.pathname)) return realFetch(resource, options);
        check(!options?.method || options.method === 'GET', '仅发出读请求');
        let result, status = 200;
        if (url.pathname.endsWith('/detail')) {
          status = granted ? 200 : 404;
          result = granted ? {total:1, offset:0, limit:3, next_offset:null,
            members:[{text:'合成成员' + '长'.repeat(80), detail:'合成成员情景'}]}
            : {detail:'找不到这个表达簇。'};
        } else {
          const offset = Number(url.searchParams.get('offset'));
          size = Number(url.searchParams.get('limit'));
          const total = granted ? size * 2 + 1 : 0;
          result = {total, next_offset:offset + size < total ? offset + size : null,
            items:Array.from({length:Math.max(0, Math.min(size, total - offset))}, (_, index) => ({
              location:String(offset + index), number:offset + index + 1, count:1,
              title:`合成簇${offset + index}`
            }))};
        }
        const response = Response.json(result, {status});
        if (delay === url.pathname) {
          delay = '';
          return new Promise(resolve => { resume = () => resolve(response); });
        }
        return Promise.resolve(response);
      };
      press('表达簇');
      await waitFor(() => page() === '第 1 / 3 页' && text('合成簇0'), '首屏表达簇');
      check(text(`共 ${size * 2 + 1} 个可见表达簇`), '正确可见总数');
      check(innerWidth === 390 && root().scrollWidth <= innerWidth, '窄屏列表不横向溢出');
      press('下一页');
      await waitFor(() => page() === '第 2 / 3 页', '第二页');
      press('下一页');
      await waitFor(() => page() === '第 3 / 3 页' && button('下一页').disabled, '末页');
      press('刷新');
      await waitFor(() => text('合成簇0'), '刷新回到首屏');
      const panel = find('#learning-panel-clusters');
      const memberButton = () => panel.querySelector('.resource-record button');
      const detail = () => document.querySelector('[data-cluster-detail]');
      memberButton().click();
      const dialog = document.querySelector('#editor-dialog');
      await waitFor(() => dialog?.open && detail()?.textContent.includes('合成成员情景'),
        '显示成员详情');
      check(detail().scrollWidth <= detail().clientWidth + 1, '长成员文本可换行');
      dialog.close();
      delay = path + '/detail';
      memberButton().click();
      await waitFor(() => resume, '旧详情已发起');
      const oldDetail = resume;
      dialog.close();
      resume = null;
      delay = path;
      press('刷新');
      await waitFor(() => resume, '旧列表已发起');
      const oldList = resume;
      granted = false;
      press('黑话');
      check(!button('表达簇'), '黑话页不显示表达子页');
      press('表达方式');
      await waitFor(() => text('当前没有可见的表达簇') && page() === '第 0 / 0 页', '撤权空态');
      oldDetail();
      oldList();
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      check(!dialog.open && !detail(), '旧详情未重现');
      check(!panel.textContent.includes('合成簇0') && page() === '第 0 / 0 页', '旧列表未重现');
    """,
    )


def test_expression_cluster_members_paginate_at_390px(browser: Browser) -> None:
    browser.open("learning", 390, 740)
    _run(
        browser,
        r"""
      await waitFor(() => find('#learning-expression-list')?.querySelector('.resource-record'),
        '学习页就绪');
      const path = '/api/resources/learning/clusters';
      const rows = Array.from({length:8}, (_, index) => ({
        text:`合成分页成员${index}`, detail:'合成情景'
      }));
      let release, current = 'first';
      const realFetch = window.fetch;
      window.fetch = (resource, options) => {
        const url = new URL(resource, location.origin);
        if (url.pathname === path) return Promise.resolve(Response.json({total:2, next_offset:null,
          items:[{location:current, number:1, count:8, title:'合成分页簇'},
            {location:'other', number:2, count:1, title:'另一簇'}]}));
        if (url.pathname !== path + '/detail') return realFetch(resource, options);
        const offset = Number(url.searchParams.get('offset') ?? 0);
        const limit = Number(url.searchParams.get('limit') ?? rows.length);
        const clusterKey = url.searchParams.get('location');
        if (clusterKey !== current && clusterKey !== 'other') {
          return Promise.resolve(Response.json({detail:'找不到这个表达簇。'}, {status:404}));
        }
        const visible = clusterKey === 'other' ? [{text:'另一簇成员', detail:'合成情景'}] : rows;
        const response = Response.json({members:visible.slice(offset, offset + limit),
          total:visible.length, offset, limit,
          next_offset:offset + limit < visible.length ? offset + limit : null});
        if (offset === 3) return new Promise(resolve => { release = () => resolve(response); });
        return Promise.resolve(response);
      };
      const dialog = () => document.querySelector('#editor-dialog');
      const details = () => dialog()?.querySelector('[data-cluster-detail]');
      const has = value => details()?.textContent.includes(value);
      const nav = label => [...dialog().querySelectorAll('button')]
        .find(node => node.textContent.trim() === label);
      const pageStatus = value => dialog()?.textContent.includes(value);
      const select = index => find('#learning-panel-clusters')
        .querySelectorAll('.resource-record button')[index].click();
      press('表达簇');
      await waitFor(() => text('合成分页簇'), '簇列表');
      select(0);
      await waitFor(() => dialog()?.open && has('合成分页成员0'), '簇成员首页');
      check(pageStatus('第 1 / 3 页'), '详情显示当前页和总页数');
      check(!has('合成分页成员3') && nav('上一页').disabled, '首页有界');
      const body = dialog().querySelector('.dialog-body');
      check(body.scrollHeight <= body.clientHeight + 1, '大簇的详情不形成长滚动');
      nav('下一页').click();
      await waitFor(() => release, '第二页请求已发出');
      check(!nav('上一页').disabled && !nav('下一页').disabled && !has('合成分页成员0'),
        '成员加载期间清除旧成员，但仍可翻页');
      const delayedSecond = release;
      const jump = dialog().querySelector('.resource-page-jump');
      jump.value = '3';
      jump.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', cancelable:true}));
      await waitFor(() => pageStatus('第 3 / 3 页') && has('合成分页成员7'), '详情末页');
      delayedSecond();
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      check(nav('下一页').disabled && !nav('上一页').disabled && !has('合成分页成员3'), '末页有界');
      release = null;
      nav('上一页').click();
      await waitFor(() => release, '关闭前仍有成员页在读取');
      const old = release;
      dialog().close();
      select(1);
      await waitFor(() => dialog().open && has('另一簇成员'), '切换簇');
      old();
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      check(pageStatus('第 1 / 1 页') && !has('合成分页成员3'), '旧成员页不覆盖新簇');
      dialog().close();
      select(0);
      await waitFor(() => has('合成分页成员0'), '再次进入旧快照');
      current = 'replacement';
      nav('下一页').click();
      await waitFor(() => !dialog().open && text('合成分页簇'), '快照替换关闭旧详情');
      check(!has('合成分页成员0'), '快照替换不保留旧页');
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
      await waitFor(() => find('[data-learning-tab="jargon"]'), '学习页就绪');
      click('[data-learning-tab="jargon"]');
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


@pytest.mark.parametrize("edited_field", ["start", "end"])
def test_memory_flow_keeps_date_draft_during_shortcut(browser: Browser, edited_field: str) -> None:
    browser.open("memory", 390, 740)
    _run(
        browser,
        f"const editedField = {json.dumps(edited_field)};\n"
        """
      await waitFor(() => button('记忆流'), '长期记忆页已打开');
      click('[data-memory-tab="flow"]');
      await waitFor(() => text('合成事件000') && !button('下一页').disabled, '记忆流首页');
      const panel = find('#memory-panel-flow');
      const [start, end] = panel.querySelectorAll('input[type="datetime-local"]');
      const localTime = value => {
        const date = new Date(value);
        return new Date(+date - date.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
      };
      const requests = [];
      const original = window.fetch;
      window.fetch = (url, options) => {
        const path = new URL(url, location.href);
        if (path.pathname !== '/api/resources/memory/flow') return original(url, options);
        return new Promise(resolve => requests.push({params:path.searchParams, resolve}));
      };
      const respond = (index, range, summary) => {
        requests[index].resolve({ok:true, status:200, headers:new Headers(), json:async () => ({
          enabled:true, total:99, next_offset:3, conversations:[], ...range,
          items:[{event_type:'profile_created', recorded_at:range.start,
            summary, conversations:[]}],
        })});
        return new Promise(resolve => setTimeout(resolve, 0));
      };
      const oldRange = {start:'2026-01-01T00:00:01.000Z', end:'2026-01-02T00:00:01.000Z'};
      const currentRange = {start:'2026-02-01T00:00:02.000Z', end:'2026-02-08T00:00:02.000Z'};
      const newestRange = {start:'2026-03-01T00:00:03.000Z', end:'2026-03-31T00:00:03.000Z'};
      try {
        press('最近24小时');
        press('最近7天');
        check(requests[0].params.get('hours') === '24' &&
          requests[1].params.get('hours') === '168', '两次快捷查询都已发出');
        check(button('下一页').disabled, '读取中不可翻页');
        const edited = editedField === 'start' ? start : end;
        check(!edited.matches(':disabled'), '读取中可编辑日期');
        edited.value = '2025-06-01T12:15:10';
        edited.dispatchEvent(new Event('input', {bubbles:true}));
        const draft = [start.value, end.value];
        respond(1, currentRange, '当前快捷结果');
        await waitFor(() => text('当前快捷结果'), '后发请求先返回');
        check(start.value === draft[0] && end.value === draft[1], '快捷响应不得覆盖任一日期草稿');
        const currentPage = page();
        await respond(0, oldRange, '先发迟到结果');
        check(text('当前快捷结果') && !text('先发迟到结果') && page() === currentPage,
          '迟到响应不得替换当前结果或分页');
        check(start.value === draft[0] && end.value === draft[1], '迟到响应不得改写日期草稿');
        press('下一页');
        const nextQuery = requests[2].params;
        check(nextQuery.get('start') === currentRange.start &&
          nextQuery.get('end') === currentRange.end && !nextQuery.has('hours'),
          '翻页沿用已应用范围');
        respond(2, currentRange, '已应用范围第二页');
        await waitFor(() => text('已应用范围第二页'), '已应用范围翻页成功');
        check(start.value === draft[0] && end.value === draft[1], '翻页不覆盖草稿');
        press('刷新');
        for (const [index, field] of ['start', 'end'].entries()) {
          const expected = draft[index] ? new Date(draft[index]).toISOString() : null;
          check(requests[3].params.get(field) === expected, '刷新使用手填范围');
        }
        respond(3, currentRange, '手填范围结果');
        await waitFor(() => text('手填范围结果'), '手填范围刷新成功');
        press('最近30天');
        respond(4, newestRange, '无编辑的快捷结果');
        await waitFor(() => text('无编辑的快捷结果'), '无编辑的快捷请求完成');
        check(start.value === localTime(newestRange.start) &&
          end.value === localTime(newestRange.end), '未编辑时仍回填两个日期');
      } finally {
        window.fetch = original;
      }
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
      await waitFor(() => button('确认追加导入'), '导入导出页可见');
      check(button('确认追加导入').disabled, '未选择文件不可导入');
      check(!button('下一页'), '导入导出不是分页列表');
    """,
    )
