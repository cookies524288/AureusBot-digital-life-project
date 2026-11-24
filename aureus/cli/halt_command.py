"""halt 和 resume 命令：设置和解除急停（06 号文档第 7 节，D54）。

只读写数据根目录里的急停标记文件，不打开数据库、不要锁：正在运行的实例下一拍就会看到。
所以另开一个终端也能在它运行时叫停它。
"""

import argparse
import sys

from aureus.cli.keys_command import EXIT_FAILURE
from aureus.common import AureusPaths, SystemClock
from aureus.safety import EmergencyStop

EXIT_OK = 0


def run_halt_command(args: argparse.Namespace, paths: AureusPaths) -> int:
    """执行 halt 命令。

    Args:
        args: 解析好的命令行参数，要有 reason（可以为空）。
        paths: 数据根目录的路径集合。

    Returns:
        退出码：0 急停已生效（包括本来就在急停中），1 标记写不进去。
    """
    stop = EmergencyStop(paths.halt_file, SystemClock())
    try:
        engaged = stop.engage(args.reason)
    except OSError as error:
        # 没写成功就要说清楚：用户会以为它已经停了。
        print(f"错误：急停没有生效，写不进 {paths.halt_file}（{error}）。", file=sys.stderr)
        return EXIT_FAILURE
    if engaged:
        print("已急停。助手不会再调用模型、不会再回复，收到的消息照常记录。")
        print("解除用：aureus resume")
    else:
        print(f"本来就在急停中：{stop.status().reason}")
    return EXIT_OK


def run_resume_command(paths: AureusPaths) -> int:
    """执行 resume 命令。

    Returns:
        退出码：0 已解除（包括本来就没有急停），1 标记删不掉，急停还在。
    """
    stop = EmergencyStop(paths.halt_file, SystemClock())
    try:
        released = stop.release()
    except OSError as error:
        print(f"错误：急停还在，删不掉 {paths.halt_file}（{error}）。", file=sys.stderr)
        return EXIT_FAILURE
    print("已解除急停。" if released else "本来就没有急停。")
    return EXIT_OK
