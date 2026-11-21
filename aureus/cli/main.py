"""命令行入口：解析参数、确定数据根目录、把命令交给对应的处理函数。"""

import argparse
import getpass
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from aureus.cli.agenda_command import DEFAULT_AGENDA_LIMIT, run_agenda_command
from aureus.cli.chat_command import run_chat_command
from aureus.cli.halt_command import run_halt_command, run_resume_command
from aureus.cli.init_command import run_init_command
from aureus.cli.inspect_commands import (
    DEFAULT_TIMELINE_LIMIT,
    positive_int,
    run_timeline_command,
    run_usage_command,
)
from aureus.cli.interests_command import run_interests_command
from aureus.cli.keys_command import EXIT_FAILURE, ReadSecret, key_name_argument, run_keys_command
from aureus.cli.lab_command import configure_lab_parser, list_experiments, run_lab_command
from aureus.cli.monitor_command import DEFAULT_MONITOR_PORT, run_monitor_command
from aureus.cli.persona_command import DEFAULT_PERSONA_LIMIT, run_persona_command
from aureus.cli.portability_commands import (
    archive_argument,
    run_export_command,
    run_import_command,
)
from aureus.common.paths import AureusPaths, HomePathError, home_is_default, resolve_home
from aureus.identity import IdentityLayer


def build_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器。

    Returns:
        解析器。命令有 init、chat、timeline、usage、persona、monitor、lab、halt、resume、export、
        import 和 keys，
        用法见各个 --help。
    """
    parser = argparse.ArgumentParser(prog="aureus", description="Aureus 命令行。")
    parser.add_argument(
        "--home",
        metavar="数据根目录",
        help="数据根目录的位置；不给就用环境变量 AUREUS_HOME；源码检出默认用项目下的 aureus_home/。"
        "可以写相对路径，按当前所在的目录算。",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="命令")
    init = commands.add_parser("init", help="建好数据根目录；没指定位置时会问你放哪里")
    init.add_argument("--yes", action="store_true", help="接受默认位置，不再询问")
    chat = commands.add_parser("chat", help="启动它，在终端里聊天")
    chat.add_argument(
        "--fake", action="store_true", help="用假器官：不调用模型、不花钱，用来试运行"
    )
    timeline = commands.add_parser("timeline", help="查看时间线上最新的几条记录（只读）")
    timeline.add_argument(
        "--limit",
        type=positive_int,
        default=DEFAULT_TIMELINE_LIMIT,
        metavar="条数",
        help=f"最多显示几条，默认 {DEFAULT_TIMELINE_LIMIT}",
    )
    timeline.add_argument(
        "--event-type", metavar="事件类型", help="只看这种事件，比如 message、woke_up"
    )
    commands.add_parser("usage", help="查看本月的模型用量和费用（只读）")
    persona = commands.add_parser("persona", help="查看人设版本；--revert 编号恢复三层之一")
    persona.add_argument(
        "--layer",
        choices=[layer.value for layer in IdentityLayer],
        help="只看这一层："
        + "、".join(f"{layer.value}（{layer.describe()}）" for layer in IdentityLayer),
    )
    persona.add_argument(
        "--limit",
        type=positive_int,
        default=None,
        metavar="个数",
        help=f"最多显示几个版本，默认 {DEFAULT_PERSONA_LIMIT}",
    )
    persona.add_argument("--full", action="store_true", help="显示完整内容，不截断")
    persona.add_argument(
        "--revert", type=positive_int, metavar="版本号", help="恢复到旧版本，运行中请用网页"
    )
    agenda = commands.add_parser("agenda", help="查看它答应过的事；--cancel 编号 取消一条")
    agenda.add_argument("--all", action="store_true", help="也列出已经结束的")
    agenda.add_argument(
        "--limit",
        type=positive_int,
        default=DEFAULT_AGENDA_LIMIT,
        metavar="条数",
        help=f"和 --all 一起用，最多显示几条，默认 {DEFAULT_AGENDA_LIMIT}",
    )
    agenda.add_argument("--cancel", type=positive_int, metavar="编号", help="取消这一条约定")
    interests = commands.add_parser("interests", help="查看兴趣和最近收获")
    interests.add_argument("--remove", type=positive_int, metavar="编号", help="结束这一条兴趣")
    monitor = commands.add_parser(
        "monitor", help="启动观察台：在浏览器里只读地看它的内部状态（只绑本机，要装 Streamlit）"
    )
    monitor.add_argument(
        "--port",
        type=positive_int,
        default=DEFAULT_MONITOR_PORT,
        metavar="端口",
        help=f"监听端口，1024 到 65535，默认 {DEFAULT_MONITOR_PORT}",
    )
    configure_lab_parser(commands.add_parser("lab", help="运行可复现的离线实验"))
    halt = commands.add_parser(
        "halt", help="急停：它不再调用模型、不再回复，消息照常记录（运行中的实例下一拍生效）"
    )
    halt.add_argument(
        "reason", nargs="?", default="", metavar="原因", help="记进时间线的原因，可以不写"
    )
    commands.add_parser("resume", help="解除急停")
    export = commands.add_parser("export", help="把数据导出成一个 zip（不含密钥）")
    export.add_argument("archive", type=archive_argument, metavar="导出文件")
    export.add_argument("--include-logs", action="store_true", help="同时导出日志（默认不含）")
    import_ = commands.add_parser("import", help="从导出的 zip 导入数据（要先停掉运行中的实例）")
    import_.add_argument("archive", type=archive_argument, metavar="导出文件")
    import_.add_argument(
        "--force",
        action="store_true",
        help="目标已有数据时覆盖它；覆盖前会先备份到 data/snapshots/",
    )
    import_.add_argument(
        "--no-backup",
        action="store_true",
        help="和 --force 一起用：覆盖前不备份旧数据（被覆盖的数据找不回来）",
    )
    keys = commands.add_parser("keys", help="管理加密的密钥库（API 密钥）")
    actions = keys.add_subparsers(dest="action", required=True, metavar="操作")
    set_parser = actions.add_parser("set", help="录入或更新一条密钥（提示输入，不回显）")
    set_parser.add_argument("name", type=key_name_argument, metavar="密钥名")
    actions.add_parser("list", help="列出密钥名（不显示密钥本身）")
    remove_parser = actions.add_parser("remove", help="删除一条密钥")
    remove_parser.add_argument("name", type=key_name_argument, metavar="密钥名")
    actions.add_parser("migrate", help="把 .env 里的明文密钥搬进密钥库（不删除 .env）")
    return parser


def _resolve_paths(home_argument: str | None, environ: Mapping[str, str] | None) -> AureusPaths:
    explicit: Path | None = None
    if home_argument is not None and home_argument.strip():
        # 命令行上敲的相对路径按“此刻所在的目录”算：这是用户当面敲的，
        # 不像环境变量或配置文件，含义不会随运行位置悄悄变化。
        explicit = Path(home_argument.strip()).expanduser().resolve()
    return AureusPaths(resolve_home(explicit, environ))


def main(
    argv: Sequence[str] | None = None,
    *,
    read_secret: ReadSecret = getpass.getpass,
    environ: Mapping[str, str] | None = None,
    ask: Callable[[str], str] = input,
) -> int:
    """命令行主函数。

    Args:
        argv: 命令行参数（不含程序名）；不给就读真实的命令行。
        read_secret: 读一条密钥的函数，默认是不回显的 getpass；测试时传假的。
        environ: 环境变量表，默认读真实的环境变量；测试时可以传入假的。
        ask: 提问并读一行回答的函数，init 用；测试时传假的。

    Returns:
        退出码：0 成功，1 失败。参数写错时由 argparse 直接以 2 退出。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "lab" and args.action == "list":
        return list_experiments()
    if (
        args.command == "persona"
        and args.revert is not None
        and (args.layer is not None or args.limit is not None or args.full)
    ):
        parser.error("--revert 不能和 --layer、--limit、--full 一起使用")
    try:
        paths = _resolve_paths(args.home, environ)
    except HomePathError as error:
        print(f"错误：{error}", file=sys.stderr)
        return EXIT_FAILURE
    # 每次都先说清楚用的是哪个数据根目录：位置不对时（比如忘了 --home），
    # 用户一眼就能看出来，而不是悄悄在别处新建一份空的密钥库。
    if args.command == "init":
        # init 自己会说明数据根目录，而且没指定位置时要先问用户，所以不先打印。
        return run_init_command(
            args, paths, home_is_default=home_is_default(args.home, environ), ask=ask
        )
    print(f"数据根目录：{paths.home}")
    if args.command == "chat":
        return run_chat_command(args, paths)
    if args.command == "timeline":
        return run_timeline_command(args, paths)
    if args.command == "usage":
        return run_usage_command(paths)
    if args.command == "persona":
        return run_persona_command(args, paths)
    if args.command == "agenda":
        return run_agenda_command(args, paths)
    if args.command == "interests":
        return run_interests_command(args, paths)
    if args.command == "monitor":
        return run_monitor_command(args, paths)
    if args.command == "lab":
        return run_lab_command(args, paths)
    if args.command == "halt":
        return run_halt_command(args, paths)
    if args.command == "resume":
        return run_resume_command(paths)
    if args.command == "export":
        return run_export_command(args, paths)
    if args.command == "import":
        return run_import_command(args, paths)
    try:
        return run_keys_command(args, paths, read_secret)
    except (EOFError, KeyboardInterrupt):
        print("已取消，什么都没有改。", file=sys.stderr)
        return EXIT_FAILURE
