"""init 命令：建好数据根目录，没指定位置时提示用户选一个（D46，路线图 0.10）。

只建文件夹，不写任何个人设定，也不往数据根目录之外写东西：选好的位置要用户自己设成
环境变量 AUREUS_HOME（或每次加 --home），这里只告诉他怎么设。
"""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from aureus.common import AureusPaths, HomePathError, resolve_home

EXIT_OK = 0
EXIT_FAILURE = 1

WriteLine = Callable[[str], None]
Ask = Callable[[str], str]


def _choose_home(paths: AureusPaths, ask: Ask, write_line: WriteLine) -> AureusPaths | None:
    """问用户数据根目录放哪里；回车表示用默认位置。输入被关闭或位置不合法时返回 None。"""
    write_line(f"还没有指定数据根目录。默认位置是 {paths.home}")
    write_line("源码检出时默认放在项目根目录的 aureus_home/；也可以输入其他绝对路径。")
    try:
        answer = ask("想放在别处的话，输入一个绝对路径；直接回车用默认位置：").strip()
    except EOFError:
        print(
            "错误：现在没法提问。请加 --home <路径> 指定位置，或加 --yes 用默认位置。",
            file=sys.stderr,
        )
        return None
    if not answer:
        return paths
    try:
        return AureusPaths(resolve_home(Path(answer).expanduser()))
    except HomePathError as error:
        print(f"错误：{error}", file=sys.stderr)
        return None


def _explain_how_to_keep(paths: AureusPaths, write_line: WriteLine) -> None:
    write_line("以后每次运行，要让软件找到这个位置，二选一：")
    write_line(f'  - 每次加 --home "{paths.home}"')
    write_line("  - 设成环境变量 AUREUS_HOME，只需设一次：")
    write_line(f'      Windows：setx AUREUS_HOME "{paths.home}"（新开的终端才生效）')
    write_line(f'      Linux/macOS：export AUREUS_HOME="{paths.home}"（写进 shell 的配置文件）')


def run_init_command(
    args: argparse.Namespace,
    paths: AureusPaths,
    *,
    home_is_default: bool,
    ask: Ask = input,
    write_line: WriteLine = print,
) -> int:
    """执行 init 命令。可以重复运行，已存在的东西不动。

    Args:
        args: 解析好的命令行参数，要有 yes。
        paths: 按 --home、环境变量或默认位置得到的数据根目录。
        home_is_default: 位置是不是落在默认位置（没指定）；是就要先问用户。
        ask: 提问并读一行回答的函数；测试时传假的。
        write_line: 打印一行的函数；测试时传假的。

    Returns:
        退出码：0 成功，1 失败（什么都没有建）。
    """
    chosen = paths
    if home_is_default and not args.yes:
        picked = _choose_home(paths, ask, write_line)
        if picked is None:
            return EXIT_FAILURE
        chosen = picked
    existed = chosen.home.is_dir()
    try:
        chosen.ensure_directories()
    except (HomePathError, OSError) as error:
        print(f"错误：建不了数据根目录：{error}", file=sys.stderr)
        return EXIT_FAILURE
    write_line(f"数据根目录{'已存在，补齐了缺的文件夹' if existed else '已建好'}：{chosen.home}")
    if chosen.home != paths.home:
        _explain_how_to_keep(chosen, write_line)
    write_line("接下来：")
    write_line(
        f"  1. 把项目里 config/identity.example.toml 复制到 {chosen.identity_file}，改成你自己的"
    )
    write_line("  2. aureus keys set 密钥名（录入模型的密钥）")
    write_line("  3. aureus chat --fake 试运行；换成 aureus chat 才会真实调用模型")
    return EXIT_OK
