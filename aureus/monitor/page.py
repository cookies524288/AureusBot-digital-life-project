"""观察台页面（Streamlit，D64）：只读，没有任何写入按钮。

由 aureus monitor 用 streamlit run 启动，数据根目录从命令行的 --home 取。
读数据都走 MonitorReader（只读连接）；这里只管显示。
"""

import argparse
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from aureus.body import BodySample
from aureus.common import AureusPaths, ConfigError, RuntimeConfig, SystemClock, load_runtime_config
from aureus.common.time_format import format_local_time
from aureus.hormones import HormoneKind
from aureus.monitor import MonitorError, MonitorReader
from aureus.monitor.hormone_curves import hormone_curves, lookback

CURVE_HOURS = 24
REFRESH_SECONDS = 10
TIMELINE_ROWS = 30
PERSONA_ROWS = 20
OFF = "已关闭"


def _paths() -> AureusPaths:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    args, _ = parser.parse_known_args(sys.argv[1:])
    return AureusPaths(Path(args.home))


def _percent(value: float | None) -> str:
    # None 是信号关着或不限，不是 0：不能显示成「正常」。
    return OFF if value is None else f"{value:.0%}"


def _show_latest(sample: BodySample | None) -> None:
    if sample is None:
        st.info("还没有身体信号样本：它还没运行，或 [body] sample_enabled 关着。")
        return
    st.caption(f"最新样本：{format_local_time(sample.timestamp)}")
    cells = st.columns(6)
    cells[0].metric("精力", sample.energy_tier, _percent(sample.energy_fraction))
    cells[1].metric("疲劳", _percent(sample.fatigue_level))
    cells[2].metric("压力", _percent(sample.stress_level))
    night = OFF if sample.night is None else ("夜间" if sample.night else "白天")
    cells[3].metric("睡眠", night)
    cells[4].metric("焦点", sample.focus or "无")
    cells[5].metric("收件箱", f"{sample.inbox_count} 条")


def _show_curves(samples: list[BodySample]) -> None:
    if not samples:
        return
    frame = pd.DataFrame(
        {
            "精力": pd.Series([s.energy_fraction for s in samples], dtype="float64"),
            "疲劳": pd.Series([s.fatigue_level for s in samples], dtype="float64"),
            "压力": pd.Series([s.stress_level for s in samples], dtype="float64"),
        }
    )
    frame.index = pd.Index([s.timestamp for s in samples], name="时间")
    st.line_chart(frame)


HORMONE_NAMES = {
    HormoneKind.ANGER: "怒气",
    HormoneKind.JOY: "愉悦",
    HormoneKind.HURT: "低落",
    HormoneKind.SURPRISE: "惊讶",
}


def _show_hormones(reader: MonitorReader, runtime: RuntimeConfig) -> None:
    if not runtime.hormones.enabled:
        st.info("激素已关闭（[hormones] enabled = false）。")
        return
    end = SystemClock().now()
    records = reader.hormone_trigger_records(end - lookback(CURVE_HOURS, runtime.hormones))
    if not records:
        st.info("还没有激素触发记录。")
        return
    moments, curves = hormone_curves(
        records, end - timedelta(hours=CURVE_HOURS), end, runtime.hormones
    )
    frame = pd.DataFrame(
        {HORMONE_NAMES[kind]: pd.Series(levels, dtype="float64") for kind, levels in curves.items()}
    )
    frame.index = pd.Index(moments, name="时间")
    st.line_chart(frame)


def _show_spending(reader: MonitorReader, runtime: RuntimeConfig) -> None:
    now = SystemClock().now()
    calendar = runtime.time.build_calendar()
    today = reader.tokens_used(*calendar.day_bounds(now))
    month = reader.tokens_used(*calendar.month_bounds(now))
    limit = runtime.safety.monthly_token_limit
    cells = st.columns(3)
    cells[0].metric("今日已用 token", today)
    cells[1].metric("本月已用 token", month)
    cells[2].metric("月度上限", "没有设置" if limit is None else limit)


def _show(reader: MonitorReader, runtime: RuntimeConfig) -> None:
    st.subheader("身体信号")
    _show_latest(reader.latest_sample())
    since = SystemClock().now() - timedelta(hours=CURVE_HOURS)
    _show_curves(reader.samples_since(since))
    st.subheader("激素")
    _show_hormones(reader, runtime)
    st.subheader("花费")
    _show_spending(reader, runtime)
    st.subheader("最近的时间线")
    entries = reader.recent_timeline(TIMELINE_ROWS)[::-1]
    st.dataframe(
        [
            {
                "序号": e.id,
                "时间": format_local_time(e.timestamp),
                "谁": e.actor_type,
                "类型": e.event_type,
                "可见范围": e.visibility,
                "内容": e.content,
            }
            for e in entries
        ],
        hide_index=True,
    )
    st.subheader("人设的历史版本")
    versions = reader.identity_versions(PERSONA_ROWS)[::-1]
    st.dataframe(
        [
            {
                "序号": v.id,
                "时间": format_local_time(v.created_at),
                "层": v.layer.describe(),
                "来源": v.source.describe(),
                "内容": v.content,
            }
            for v in versions
        ],
        hide_index=True,
    )


@st.fragment(run_every=REFRESH_SECONDS)
def dashboard(paths: AureusPaths) -> None:
    """定时刷新的主体：每次都重新只读地打开数据库，用完就关。"""
    try:
        runtime = load_runtime_config(paths)
    except ConfigError as error:
        st.error(str(error))
        return
    reader = MonitorReader(paths.database_file)
    try:
        reader.open()
    except MonitorError as error:
        st.warning(str(error))
        return
    try:
        _show(reader, runtime)
    finally:
        reader.close()


st.set_page_config(page_title="Aureus 观察台", layout="wide")
st.title("Aureus 观察台")
st.caption(f"只读，只有本机能打开；每 {REFRESH_SECONDS} 秒自动刷新。")
dashboard(_paths())
