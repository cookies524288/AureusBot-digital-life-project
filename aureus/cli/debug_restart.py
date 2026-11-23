"""只打开已运行实例的网页重启入口；由维护者在网页登录后确认。"""

import webbrowser

from aureus.common.paths import HomePathError
from aureus.web.runner import running_web_url

from .windows_launcher import _launcher_home


def main() -> int:
    """复用启动器的数据目录规则和已发布地址，不启动新的内核。"""
    try:
        home = _launcher_home()
    except HomePathError as error:
        print(f"找不到本次检出的数据根目录：{error}")
        return 1
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
