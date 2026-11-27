"""3.66 C4：复用 3.64 回环夹具，以独立 Chrome 对照固定的本地提交。"""

import argparse
import json
import socket
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from http.client import HTTPConnection
from itertools import cycle, islice, permutations
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
from typing import Any

import uvicorn
from starlette.responses import JSONResponse
from websockets.sync.client import connect

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
BASELINE = "e19c411"
HISTORY = 1000
EVENTS = 20
COLD_SAMPLES = 20
DEFER_COLD_SAMPLES = 24
START = datetime(2026, 11, 27, 12, tzinfo=UTC)
ACTION = "/__fixture/chat"
TIMELINE_SCRIPT = '<script src="/assets/timeline-live.js"></script>'


def _synthetic_message(identifier: int) -> dict[str, Any]:
    return {
        "id": identifier,
        "who": "user" if identifier <= HISTORY and identifier % 2 else "me",
        "content": f"离线消息 #{identifier:04d} · 窗边的薄荷长出了新叶。",
        "channel": "web",
        "time": (START + timedelta(seconds=identifier)).isoformat(),
        "delivery_key": f"{identifier}:1",
    }


def _make_fixture_types(fixture_path: Path) -> tuple[type, dict[str, str]]:
    sys.path.append(str(fixture_path))
    from audit_preview import Samples, timeline_page
    from ui364_asgi import ACTIONS, ROOTS, STATIC, AuditPreview, error

    ACTIONS[ACTION] = "append-synthetic-reply"
    STATIC["/assets/timeline-live.js"] = "timeline-live.js"

    class TrialSamples(Samples):
        def __init__(self, mode, *, incremental):
            super().__init__(mode)
            self.pressure = mode == "normal"
            self.incremental = incremental
            self.history = [_synthetic_message(index) for index in range(1, HISTORY + 1)]

        def read(self, path, query):
            if path == "/api/status":
                return {**super().read(path, query), "incremental_updates": self.incremental}
            if path == "/api/history":
                limit = int(query.get("limit", [str(HISTORY if self.pressure else 50)])[0])
                since = int(query.get("since_id", ["0"])[0])
                before = int(query.get("before_id", [str(len(self.history) + 1)])[0])
                rows = [
                    row
                    for row in self.history
                    if since < row["id"] < before
                    and (query.get("replies_only") != ["true"] or row["who"] == "me")
                ]
                return rows[:limit] if "since_id" in query else rows[-limit:]
            if path == "/api/timeline" and "since_id" in query:
                since = int(query["since_id"][0])
                limit = int(query.get("limit", ["100"])[0])
                tail = [row for row in self.records if row["id"] > since]
                page = timeline_page(tail[:limit], query)
                return {
                    **page,
                    "has_more": len(tail) > limit,
                    "since_id": tail[min(limit, len(tail)) - 1]["id"] if tail else since,
                }
            result = super().read(path, query)
            if path == "/api/timeline":
                return {**result, "since_id": self.records[-1]["id"], "has_more": False}
            return result

    class TrialPreview(AuditPreview):
        def __init__(self, port, root, incremental):
            ROOTS[port] = root
            super().__init__(port, root, partial(TrialSamples, incremental=incremental))
            self.incremental = incremental

        async def http(self, request):
            if request.scope["path"] != ACTION:
                return await super().http(request)
            if (
                request.method != "POST"
                or request.headers.getlist("x-ui364-action") != [ACTIONS[ACTION]]
                or request.url.query
                or await request.body()
            ):
                return error(403, "只接受空正文的本地合成操作")
            scene = self.scenes[(request.headers["host"], "normal")]
            if len(scene.samples.history) >= HISTORY + EVENTS or not scene.listeners:
                return error(409, "合成推送已结束，或没有监听器")
            row = _synthetic_message(len(scene.samples.history) + 1)
            scene.samples.history.append(row)
            for listener in scene.listeners:
                listener.put_nowait({**row, "type": "reply", "sender": "合成助手"})
                if self.incremental:
                    listener.put_nowait(
                        {
                            "type": "event",
                            "topic": "chat",
                            "data": {
                                "id": row["id"],
                                "previous_id": row["id"] - 1,
                                "delivery_key": row["delivery_key"],
                            },
                        }
                    )
            return JSONResponse({"id": row["id"]})

    return TrialPreview, STATIC


def _with_deferred_timeline(source: bytes) -> bytes:
    html = source.decode("utf-8")
    replacements = (
        (TIMELINE_SCRIPT + "\n", ""),
        (
            "<style>\n",
            TIMELINE_SCRIPT.replace("></script>", " defer></script>") + "\n<style>\n",
        ),
        (
            "\nwatchLauncherExit();\nenter();\n</script>",
            "\nwatchLauncherExit();\n"
            "document.addEventListener('DOMContentLoaded', enter, {once:true});\n</script>",
        ),
    )
    for old, new in replacements:
        if html.count(old) != 1:
            raise ValueError(f"找不到唯一的实验替换位置：{old!r}")
        html = html.replace(old, new)
    return html.encode("utf-8")


def _extract(revision: str, target: Path, files: set[str], *, defer_timeline: bool) -> None:
    for name in sorted(files):
        source = f"{revision}:aureus/web/static/{name}"
        if name == "timeline-live.js" and revision == BASELINE:
            continue
        result = subprocess.check_output(["git", "show", source], cwd=ROOT)
        if name == "index.html" and defer_timeline:
            result = _with_deferred_timeline(result)
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(result)


def _wait_until(predicate: Callable[[], object], description: str, deadline: float = 12) -> object:
    stop = monotonic() + deadline
    while monotonic() < stop:
        if value := predicate():
            return value
        sleep(0.025)
    raise TimeoutError(description)


@contextmanager
def _serve(trial_type: type, root: Path, incremental: bool) -> Iterator[str]:
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        trial = trial_type(port, root, incremental)
        server = uvicorn.Server(
            uvicorn.Config(
                trial.app,
                log_level="error",
                access_log=False,
                proxy_headers=False,
                ws="websockets-sansio",
            )
        )
        thread = Thread(target=server.run, kwargs={"sockets": [bound]}, daemon=True)
        thread.start()
        try:
            _wait_until(lambda: server.started, "回环夹具未启动")
            yield origin
        finally:
            server.should_exit = True
            thread.join(10)
            if thread.is_alive():
                raise RuntimeError("回环夹具未退出")


class _Browser:
    def __init__(self, connection):
        self.connection = connection
        self.number = 0
        self.errors = []
        self.version = self._command("Browser.getVersion")["product"]
        self._command("Page.enable")
        self._command("Runtime.enable")
        self._command("Network.enable")
        self._command("Network.setCacheDisabled", cacheDisabled=True)
        self._command(
            "Emulation.setDeviceMetricsOverride",
            width=1440,
            height=900,
            deviceScaleFactor=1,
            mobile=False,
        )
        self._command(
            "Emulation.setEmulatedMedia",
            features=[
                {
                    "name": "prefers-reduced-motion",
                    "value": "reduce",
                }
            ],
        )
        self._command(
            "Page.addScriptToEvaluateOnNewDocument",
            source="""
            if (location.hostname === '127.0.0.1') {
              localStorage.setItem('aureus-page-prefetch', 'off');
              let pendingHistory = 0;
              const nativeFetch = window.fetch;
              window.fixturePendingHistory = () => pendingHistory;
              window.fetch = async (input, options) => {
                if (new URL(input, location.href).pathname !== '/api/history') {
                  return nativeFetch(input, options);
                }
                pendingHistory++;
                try { return await nativeFetch(input, options); }
                finally { pendingHistory--; }
              };
            }
        """,
        )

    def _command(self, method: str, **params: object) -> dict[str, Any]:
        self.number += 1
        self.connection.send(json.dumps({"id": self.number, "method": method, "params": params}))
        while True:
            event = json.loads(self.connection.recv(timeout=60))
            if event.get("method") == "Runtime.exceptionThrown":
                self.errors.append(event["params"]["exceptionDetails"]["text"])
            if event.get("id") == self.number:
                if "error" in event:
                    raise RuntimeError(event["error"])
                return event["result"]

    def evaluate(self, expression: str) -> Any:
        """在当前合成页执行探针，脚本错误视为失败。"""
        result = self._command(
            "Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True
        )
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        return result["result"].get("value")

    def navigate(self, url: str) -> None:
        """打开新文档并等待完成，避免读取旧样本。"""
        self._command("Page.navigate", url=url)
        _wait_until(
            lambda: self.evaluate(
                f"location.href === {json.dumps(url)} && document.readyState === 'complete'"
            ),
            f"页面未完成：{url}",
        )


@contextmanager
def _chrome(executable: str, profile: Path) -> Iterator[_Browser]:
    process = subprocess.Popen(
        [
            executable,
            "--headless=new",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-domain-reliability",
            "--disable-sync",
            "--no-first-run",
            "--no-default-browser-check",
            "--proxy-server=http://127.0.0.1:1",
            "--proxy-bypass-list=127.0.0.1",
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        endpoint = profile / "DevToolsActivePort"
        _wait_until(
            lambda: endpoint.is_file() and endpoint.stat().st_size, "独立 Chrome 未开启调试端口"
        )
        port = int(endpoint.read_text(encoding="utf-8").splitlines()[0])
        http = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            http.request("GET", "/json/list")
            pages = json.load(http.getresponse())
            address = next(page["webSocketDebuggerUrl"] for page in pages if page["type"] == "page")
        finally:
            http.close()
        with connect(address, proxy=None) as connection:
            yield _Browser(connection)
    finally:
        process.terminate()
        process.wait(timeout=10)


def main() -> None:
    """从 Git 导出固定版本的静态文件，运行同环境对照并独占写入原始结果。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--chrome", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--defer-timeline", action="store_true", help="加入只改变实时脚本加载顺序的隔离对照"
    )
    args = parser.parse_args()
    fixture = args.fixture.resolve(strict=True)
    trial_type, assets = _make_fixture_types(fixture)
    files = {name for route, name in assets.items() if not route.startswith("/learning-workbench")}
    interaction = (HERE / "browser_ab.js").read_text(encoding="utf-8")
    revisions = {"baseline": BASELINE, "candidate": args.candidate}
    if args.defer_timeline:
        revisions["deferred"] = args.candidate
    result = {
        "revisions": {
            key: subprocess.check_output(
                ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=ROOT, text=True
            ).strip()
            for key, ref in revisions.items()
        },
        "cold": {key: [] for key in revisions},
    }
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    if build.resolve() != build:
        raise ValueError("隔离浏览器目录必须在当前工作树内")
    with TemporaryDirectory(prefix="c4-", dir=build) as temp:
        work = Path(temp).resolve()
        for arm, revision in revisions.items():
            _extract(
                revision,
                work / arm,
                files,
                defer_timeline=args.defer_timeline and arm == "deferred",
            )
        with ExitStack() as stack:
            origins = {
                arm: stack.enter_context(_serve(trial_type, work / arm, arm != "baseline"))
                for arm in revisions
            }
            browser = stack.enter_context(_chrome(args.chrome, work / "profile"))
            result["chrome"] = browser.version
            arms = tuple(revisions)
            if args.defer_timeline:
                orders = islice(cycle(permutations(arms)), DEFER_COLD_SAMPLES)
                result["variant"] = {
                    "deferred": "timeline-live.js: head defer; enter: DOMContentLoaded"
                }
            else:
                orders = islice(cycle((arms, arms[::-1])), COLD_SAMPLES)
            result["cold_orders"] = list(orders)
            for repeat, order in enumerate(result["cold_orders"]):
                for arm in order:
                    browser.navigate(f"{origins[arm]}/?fixture=normal&sample={repeat}#overview")
                    measurement = browser.evaluate(interaction + "\nmeasureCold()")
                    result["cold"][arm].append(measurement)
            if not args.defer_timeline:
                result["seed"] = HISTORY
                result["live_replies"] = EVENTS
                result["live"] = {}
                for arm in ("baseline", "candidate"):
                    browser.navigate(f"{origins[arm]}/?fixture=normal#chat")
                    measurement = browser.evaluate(interaction + f"\nmeasureLive({EVENTS})")
                    result["live"][arm] = measurement
                browser.navigate(f"{origins['candidate']}/?fixture=stress#chat")
                result["backfill"] = browser.evaluate(interaction + "\nmeasureBackfill()")
                browser.navigate(f"{origins['candidate']}/?fixture=late#timeline")
                result["timeline"] = browser.evaluate(interaction + "\nmeasureTimeline()")
            result["runtime_errors"] = browser.errors
    if result["runtime_errors"]:
        raise RuntimeError(result["runtime_errors"])
    with args.output.open("x", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
