"""通过已认证网页接口正常重启，或打开网页由维护者确认。"""

import argparse
import getpass
import json
import re
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import httpx

from aureus.common.paths import HomePathError
from aureus.web.runner import running_web_instance, running_web_url

from .windows_launcher import _launcher_home


@dataclass(frozen=True)
class ListenerIdentity:
    """受验证的启动器与内核进程；创建时刻避免复用 PID 冒充。"""

    owner_pid: int
    owner_started: str
    listener_pid: int
    listener_started: str


def _base_pythonw_path(root: Path) -> Path | None:
    """仅使用当前项目虚拟环境的基础解释器定位对应的窗口程序。"""
    try:
        if Path(sys.prefix).resolve(strict=True) != (root / ".venv").resolve(strict=True):
            return None
        base_executable = Path(sys._base_executable).resolve(strict=True)
        if (
            Path(sys.base_prefix).resolve(strict=True) == Path(sys.prefix).resolve(strict=True)
            or base_executable.name.lower() != "python.exe"
        ):
            return None
        base_pythonw = base_executable.with_name("pythonw.exe")
        return base_pythonw.resolve(strict=True) if base_pythonw.is_file() else None
    except (AttributeError, OSError, ValueError):
        return None


def _listener_identity(port: int, owner: int) -> ListenerIdentity | None:
    """只认可项目启动器 → 项目中转 → 实际监听者的直属链。"""
    if sys.platform != "win32":
        return None
    command = (
        "@(Get-NetTCPConnection -LocalPort "
        + str(port)
        + " -State Listen -ErrorAction Stop | "
        + "Where-Object { $_.LocalAddress -eq '127.0.0.1' } | "
        + "Select-Object -ExpandProperty OwningProcess) | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        listener_pids = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if isinstance(listener_pids, int):
        listener_pids = [listener_pids]
    if not isinstance(listener_pids, list) or len(set(listener_pids)) != 1:
        return None
    listener_pid = listener_pids[0]
    if type(listener_pid) is not int:
        return None
    query = (
        "$p = Get-CimInstance Win32_Process -Filter 'ProcessId = "
        + str(listener_pid)
        + "' -ErrorAction Stop; "
        + "$m = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $p.ParentProcessId) "
        + "-ErrorAction Stop; "
        + "$o = Get-CimInstance Win32_Process -Filter 'ProcessId = "
        + str(owner)
        + "' -ErrorAction Stop; "
        + "@($p, $m, $o) | Select-Object ProcessId, ParentProcessId, "
        + "ExecutablePath, CommandLine, "
        + "@{Name='Started'; Expression={$_.CreationDate.ToUniversalTime().ToString('o')}} "
        + "| ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", query],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        processes = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(processes, list) or len(processes) != 3:
        return None
    listener, middle, launcher = processes
    if not all(isinstance(process, dict) for process in processes):
        return None
    root = Path(__file__).resolve().parents[2]
    launcher_path = root / "Aureus.exe"
    middle_path = root / ".venv" / "Scripts" / "pythonw.exe"
    base_pythonw = _base_pythonw_path(root)
    if base_pythonw is None:
        return None
    middle_args = middle.get("CommandLine")
    match = (
        re.search(r"(?<!\S)--owner-pid\s+(\d+)(?=\s|$)", middle_args)
        if isinstance(middle_args, str)
        else None
    )
    if (
        listener.get("ProcessId") != listener_pid
        or middle.get("ProcessId") != listener.get("ParentProcessId")
        or launcher.get("ProcessId") != middle.get("ParentProcessId")
        or launcher.get("ProcessId") != owner
        or launcher.get("ExecutablePath", "").lower() != str(launcher_path).lower()
        or middle.get("ExecutablePath", "").lower() != str(middle_path).lower()
        or listener.get("ExecutablePath", "").lower() != str(base_pythonw).lower()
        or not isinstance(middle_args, str)
        or not re.search(r"(?<!\S)-m\s+aureus\.cli\.windows_launcher(?=\s|$)", middle_args)
        or not re.search(r"(?<!\S)--tray(?=\s|$)", middle_args)
        or match is None
        or int(match.group(1)) != owner
        or not all(isinstance(process.get("Started"), str) for process in processes)
        or not (launcher["Started"] < middle["Started"] < listener["Started"])
    ):
        return None
    return ListenerIdentity(owner, launcher["Started"], listener_pid, listener["Started"])


def _verified_target(home: Path) -> tuple[str, ListenerIdentity] | None:
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
        or (listener := _listener_identity(parts.port, owner)) is None
    ):
        return None
    return url, listener


def _status(client: httpx.Client) -> str | None:
    response = client.get("/api/status")
    if response.status_code != 200:
        return None
    data = response.json()
    instance = data.get("restart_instance")
    return instance if isinstance(instance, str) and instance else None


def _interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _old_listener_exited(listener: ListenerIdentity) -> bool:
    """旧内核是否已退出；不能确认则不报告接管完成。"""
    if sys.platform != "win32":
        return False
    command = (
        "ConvertTo-Json -InputObject @(Get-CimInstance Win32_Process -Filter 'ProcessId = "
        + str(listener.listener_pid)
        + "' -ErrorAction Stop | Select-Object "
        + "@{Name='Started'; Expression={$_.CreationDate.ToUniversalTime().ToString('o')}}) "
        + "-Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        processes = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    if isinstance(processes, dict):
        processes = [processes]
    return isinstance(processes, list) and not any(
        isinstance(process, dict) and process.get("Started") == listener.listener_started
        for process in processes
    )


def _restart(home: Path) -> int:
    """只发一次重启请求；监听归属变化或协议异常均在提交前中止。"""
    if not _interactive_terminal():
        print("需要可交互的终端，已退出；未读取密码或发出重启请求。")
        return 1
    target = _verified_target(home)
    if target is None:
        print("无法确认当前网页地址与监听进程的归属；未提交密码或重启请求。")
        return 1
    try:
        confirmation = input("确认向该实例申请正常重启？输入 yes 继续：")
    except (EOFError, KeyboardInterrupt):
        print("已取消；未读取密码或发出重启请求。")
        return 1
    if confirmation.strip() != "yes":
        print("已取消；未提交密码或重启请求。")
        return 1
    try:
        password = getpass.getpass("网页登录密码：")
    except (EOFError, KeyboardInterrupt):
        print("已取消；未发出重启请求。")
        return 1
    if not password:
        print("未输入密码；未发出重启请求。")
        return 1
    if _verified_target(home) != target:
        print("目标实例发生变化；未提交密码或重启请求。")
        return 1
    url, original_listener = target
    try:
        with httpx.Client(
            base_url=url, trust_env=False, follow_redirects=False, timeout=5
        ) as client:
            login = client.post("/api/login", json={"password": password})
            if login.status_code != 200 or login.json().get("must_change_password") is not False:
                print("登录失败或需先在网页修改默认密码；未发出重启请求。")
                return 1
            previous = _status(client)
            if previous is None or _verified_target(home) != target:
                print("目标身份或监听归属无法确认；未发出重启请求。")
                return 1
            if _verified_target(home) != target:
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
                published = running_web_instance(home)
                if (
                    published is None
                    or published[0] != url
                    or published[1] != original_listener.owner_pid
                ):
                    continue
                try:
                    current_listener = _listener_identity(
                        httpx.URL(url).port or 0, original_listener.owner_pid
                    )
                    if (
                        current_listener is None
                        or current_listener.owner_started != original_listener.owner_started
                        or current_listener.listener_pid == original_listener.listener_pid
                        or current_listener.listener_started <= original_listener.listener_started
                        or not _old_listener_exited(original_listener)
                    ):
                        continue
                    with httpx.Client(
                        base_url=url, trust_env=False, follow_redirects=False, timeout=3
                    ) as probe:
                        landing = probe.get("/")
                        if landing.status_code != 200:
                            continue
                except (httpx.HTTPError, ValueError):
                    continue
                print("已确认同一启动器下的新监听进程接管且网页首页恢复；未核实新网页会话身份。")
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
