"""通过已认证网页接口正常重启，或打开网页由维护者确认。"""

import argparse
import getpass
import json
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import httpx

from aureus.common.paths import HomePathError
from aureus.web.runner import running_web_instance, running_web_url

from .windows_launcher import _launcher_home


def _listener_owner(port: int) -> tuple[int, int] | None:
    """由 Windows TCP 表核对监听端口所属进程；读不到时拒绝认证。"""
    if sys.platform != "win32":
        return None
    command = (
        "@(Get-NetTCPConnection -LocalPort "
        + str(port)
        + " -State Listen -ErrorAction Stop | "
        + "Where-Object { $_.LocalAddress -eq '127.0.0.1' } | "
        + "ForEach-Object { Get-CimInstance Win32_Process -Filter "
        + "('ProcessId = ' + $_.OwningProcess) | "
        + "Select-Object ProcessId, ParentProcessId }) | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        owners = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if isinstance(owners, dict):
        owners = [owners]
    if not isinstance(owners, list) or not owners:
        return None
    if not all(isinstance(process, dict) for process in owners):
        return None
    processes = {(process.get("ProcessId"), process.get("ParentProcessId")) for process in owners}
    if len(processes) != 1:
        return None
    pid, parent = processes.pop()
    if type(pid) is not int or type(parent) is not int:
        return None
    return pid, parent


def _verified_target(home: Path) -> tuple[str, int] | None:
    instance = running_web_instance(home)
    if instance is None:
        return None
    url, owner = instance
    parts = httpx.URL(url)
    if (
        parts.scheme != "http"
        or parts.host != "127.0.0.1"
        or not parts.port
        or parts.path not in ("", "/")
        or parts.query
        or (listener := _listener_owner(parts.port)) is None
        or owner not in listener
    ):
        return None
    return instance


def _status(client: httpx.Client) -> str | None:
    response = client.get("/api/status")
    if response.status_code != 200:
        return None
    data = response.json()
    instance = data.get("restart_instance")
    return instance if isinstance(instance, str) and instance else None


def _restart(home: Path) -> int:
    """只发一次重启请求；监听归属变化或协议异常均在提交前中止。"""
    target = _verified_target(home)
    if target is None:
        print("无法确认当前网页地址与监听进程的归属；未提交密码或重启请求。")
        return 1
    if input("确认向该实例申请正常重启？输入 yes 继续：").strip() != "yes":
        print("已取消；未提交密码或重启请求。")
        return 1
    password = getpass.getpass("网页登录密码：")
    if not password:
        print("未输入密码；未发出重启请求。")
        return 1
    if _verified_target(home) != target:
        print("目标实例发生变化；未提交密码或重启请求。")
        return 1
    url, _owner = target
    try:
        with httpx.Client(
            base_url=url, trust_env=False, follow_redirects=False, timeout=5
        ) as client:
            login = client.post("/api/login", json={"password": password})
            if login.status_code != 200 or login.json().get("must_change_password") is not False:
                print("登录失败或需先在网页修改默认密码；未发出重启请求。")
                return 1
            previous = _status(client)
            prior_listener = _listener_owner(httpx.URL(url).port or 0)
            if previous is None or _verified_target(home) != target or prior_listener is None:
                print("目标身份或监听归属无法确认；未发出重启请求。")
                return 1
            original_listener = _listener_owner(httpx.URL(url).port or 0)
            if (
                original_listener is None
                or _verified_target(home) != target
                or original_listener != prior_listener
            ):
                print("提交前目标监听归属发生变化；未发出重启请求。")
                return 1
            response = client.post("/api/restart", headers={"Origin": url})
            if response.status_code != 200 or response.json().get("instance") != previous:
                print("重启请求结果未确认；不会重试请求。")
                return 1
            print("已接受一次重启请求；正在等待新内核接管。")
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                time.sleep(1)
                updated = _verified_target(home)
                if updated is None:
                    continue
                try:
                    new_url, _ = updated
                    current_listener = _listener_owner(httpx.URL(new_url).port or 0)
                    if current_listener is None or current_listener[0] == original_listener[0]:
                        continue
                    with httpx.Client(
                        base_url=new_url, trust_env=False, follow_redirects=False, timeout=3
                    ) as probe:
                        landing = probe.get("/")
                        if landing.status_code != 200:
                            continue
                        new_login = probe.post("/api/login", json={"password": password})
                        if new_login.status_code != 200:
                            continue
                        new_instance = _status(probe)
                        if new_instance is None or new_instance == previous:
                            continue
                except (httpx.HTTPError, ValueError):
                    continue
                print("已确认新监听进程接管，实例身份已变化，网页恢复。")
                return 0
            print("等待新内核接管超时；不会重复重启请求，请在网页核实。")
            return 1
    except (httpx.HTTPError, ValueError):
        print("网络或响应异常，重启结果未知；不会重复重启请求。")
        return 1


def main(argv: list[str] | None = None) -> int:
    """默认打开网页登录页，--restart 经交互认证后申请一次正常重启。"""
    parser = argparse.ArgumentParser(description="安全打开网页，或交互式申请正常重启")
    parser.add_argument("--restart", action="store_true", help="交互输入网页密码并请求正常重启")
    args = parser.parse_args(argv)
    try:
        home = _launcher_home()
    except HomePathError as error:
        print(f"找不到本次检出的数据根目录：{error}")
        return 1
    if args.restart:
        return _restart(home)
    url = running_web_url(home)
    if url is None:
        print("未发现该数据根目录已发布的网页地址；未启动实例，也未重启。")
        return 1
    if not webbrowser.open(url):
        print("未能打开当前实例网页；未发出重启请求。")
        return 1
    print("已打开当前实例网页。请登录后点击侧栏「重启」并确认；尚未重启。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
