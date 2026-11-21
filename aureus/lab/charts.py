"""实验台画图：按需用 matplotlib 将折线图保存成 PNG（D82）。"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

from aureus.lab.experiment import Chart

FONT_FAMILIES = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"]


def matplotlib_installed() -> bool:
    """检查可选依赖是否可用，不提前导入它。"""
    return importlib.util.find_spec("matplotlib") is not None


def draw_charts(run_dir: Path, charts: Sequence[Chart]) -> list[Path]:
    """用 Agg 绘制结果，返回已写出的 PNG 路径。"""
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.figure import Figure

    paths: list[Path] = []
    with matplotlib.rc_context({"font.sans-serif": FONT_FAMILIES}):
        for chart in charts:
            figure = Figure()
            try:
                axes = figure.subplots()
                for series in chart.series:
                    axes.plot(series.x, series.y, label=series.label)
                axes.set(title=chart.title, xlabel=chart.x_label, ylabel=chart.y_label)
                axes.legend()
                path = run_dir / f"{chart.name}.png"
                figure.savefig(path)
                paths.append(path)
            finally:
                figure.clear()
    return paths
