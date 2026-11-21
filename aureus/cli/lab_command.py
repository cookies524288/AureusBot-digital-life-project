"""lab 命令：列出并运行只用假器官的离线实验（D82）。"""

import argparse
import asyncio
import sys

from aureus.cli.inspect_commands import EXIT_FAILURE, EXIT_OK
from aureus.common import AureusPaths, SystemClock
from aureus.lab import EXPERIMENTS, run_experiment

NOT_INSTALLED_MESSAGE = "没装 matplotlib，这次只输出了指标和表格，没画图。装法：uv sync --extra lab"


def non_negative_int(text: str) -> int:
    """把命令行参数解析为 0 或正整数。"""
    try:
        value = int(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是 0 或正整数") from error
    if value < 0:
        raise argparse.ArgumentTypeError("必须是 0 或正整数")
    return value


def configure_lab_parser(parser: argparse.ArgumentParser) -> None:
    """注册实验台的 list 和 run 参数。"""
    actions = parser.add_subparsers(dest="action", required=True, metavar="操作")
    actions.add_parser("list", help="列出可用的实验")
    run = actions.add_parser("run", help="跑一个实验并保存结果")
    run.add_argument("experiment", metavar="实验名")
    run.add_argument(
        "--seed", type=non_negative_int, default=0, metavar="整数", help="随机种子，默认 0"
    )
    # 支持把 --home 写在实验名后；不给时沿用主命令解析的值。
    run.add_argument(
        "--home", default=argparse.SUPPRESS, metavar="数据根目录", help="数据根目录的位置"
    )


def list_experiments() -> int:
    """按登记顺序打印每个实验的名字、编号和标题。"""
    for experiment in EXPERIMENTS.values():
        print(f"{experiment.name}  {experiment.code}  {experiment.title}")
    return EXIT_OK


def run_lab_command(args: argparse.Namespace, paths: AureusPaths) -> int:
    """执行一个实验，报告结果目录、指标和图表状态。"""
    experiment = EXPERIMENTS.get(args.experiment)
    if experiment is None:
        available = "、".join(EXPERIMENTS)
        print(f"没有叫 {args.experiment} 的实验，可用的有：{available}", file=sys.stderr)
        return EXIT_FAILURE
    try:
        report = asyncio.run(
            run_experiment(experiment, args.seed, paths.experiments_dir, SystemClock())
        )
    except Exception as error:
        detail = "，".join((str(error), *getattr(error, "__notes__", ())))
        print(f"实验失败：{detail}", file=sys.stderr)
        return EXIT_FAILURE

    print(f"运行目录：{report.run_dir}")
    for name, value in report.metrics.items():
        display = f"{value:.4f}" if isinstance(value, float) else str(value)
        print(f"{name}：{display}")
    if report.charts_drawn:
        print(f"已画图 {len(list(report.run_dir.glob('*.png')))} 张")
    else:
        print(NOT_INSTALLED_MESSAGE)
    if report.pruned:
        print(f"删掉了 {report.pruned} 个更早的运行结果（每个实验只留最近 20 次）")
    return EXIT_OK
