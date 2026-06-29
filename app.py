# app.py - 尾盘智能选股工具（完整修复版）
# -*- coding: utf-8 -*-
import streamlit as st
import akshare as ak
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import warnings
warnings.filterwarnings("ignore")

# ============================================================
# 时区工具
# ============================================================
CST = ZoneInfo("Asia/Shanghai")

def beijing_now() -> datetime:
    return datetime.now(CST)

def today_str() -> str:
    return beijing_now().strftime("%Y-%m-%d")

def is_tail_time() -> bool:
    """判断当前是否处于尾盘时段（14:30:00 <= time <= 15:00:00）"""
    now = beijing_now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tail_start = today.replace(hour=14, minute=30, second=0)
    tail_end = today.replace(hour=15, minute=0, second=0)
    return tail_start <= now <= tail_end

def is_trading_day() -> bool:
    """判断今天是否为交易日（简单判断：周一至周五）"""
    now = beijing_now()
    return now.weekday() < 5

# ============================================================
# 全局配置
# ============================================================
@st.cache_resource
def get_config():
    return {
        "pct_min": 2.0,
        "pct_max": 7.0,
        "vol_ratio_min": 1.2,
        "turnover_min": 3.0,
        "turnover_max": 15.0,
        "amount_min": 1e8,
        "max_stocks": 30,
        "cache_ttl": 600,
    }

# ============================================================
# 列名兼容工具
# ============================================================
def _get_column(df: pd.DataFrame, candidates: list) -> str | None:
    for col in candidates:
        if col in df.columns:
            return col
    for col in df.columns:
        for c in candidates:
            if c in col or col in c:
                return col
    return None

# ============================================================
# 数据获取（缓存）
# ============================================================
@st.cache_data(ttl=600)
def fetch_realtime_quotes():
    """获取实时行情数据（带列名兼容性检查）"""
    try:
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return None
        column_mapping = {
            "代码": ["代码", "code", "股票代码"],
            "名称": ["名称", "name", "股票名称"],
            "涨跌幅": ["涨跌幅", "涨跌幅%", "change_pct", "涨幅"],
            "量比": ["量比", "volume_ratio", "量比(当日)"],
            "成交额": ["成交额", "amount", "成交金额"],
            "换手率": ["换手率", "turnover", "换手率%"],
            "最新价": ["最新价", "price", "收盘价", "现价"],
        }
        mapped_cols = {}
        for std_name, aliases in column_mapping.items():
            found = _get_column(df, aliases)
            if found:
                mapped_cols[found] = std_name
        if mapped_cols:
            df = df.rename(columns=mapped_cols)
        required_cols = ["代码", "名称", "涨跌幅", "量比", "成交额", "换手率", "最新价"]
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            st.warning(f"⚠️ 数据源列名不匹配，缺失: {missing}。尝试自动适配...")
            for col in df.columns:
                for req in required_cols:
                    if req in col or col in req:
                        df = df.rename(columns={col: req})
                        break
            missing = [c for c in required_cols if c not in df.columns]
            if missing:
                st.error(f"❌ 无法识别必要列: {missing}，请检查 akshare 版本")
                return None
        numeric_cols = ["涨跌幅", "量比", "成交额", "换手率", "最新价"]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except Exception as e:
        st.error(f"获取实时行情失败: {e}")
        return None

@st.cache_data(ttl=60)
def fetch_intraday_minute(symbol: str):
    """获取当日1分钟分时数据（仅保留已知可用接口）"""
    try:
        df = ak.stock_zh_a_hist_min_em(symbol=symbol, period="1", adjust="")
        if df is None or df.empty:
            return None
        vol_col = _get_column(df, ["成交量", "volume", "vol", "VOL"])
        close_col = _get_column(df, ["收盘", "close", "price", "最新价"])
        if vol_col is None or close_col is None:
            return None
        df = df.rename(columns={vol_col: "volume", close_col: "close"})
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        return df.dropna(subset=["volume", "close"])
    except Exception:
        return None

@st.cache_data(ttl=300)
def fetch_a50_change() -> float:
    """获取富时A50涨跌幅（仅保留期货接口）"""
    try:
        df = ak.futures_zh_minute_sina(symbol="A50")
        if df is None or df.empty or len(df) < 2:
            return 0.0
        price_col = _get_column(df, ["收盘价", "close", "price"])
        if price_col is None:
            return 0.0
        prices = pd.to_numeric(df[price_col], errors="coerce").dropna()
        if len(prices) < 2:
            return 0.0
        latest, prev = prices.iloc[-1], prices.iloc[-2]
        if prev == 0:
            return 0.0
        return round((latest - prev) / prev * 100, 2)
    except Exception:
        return 0.0

# ============================================================
# 选股逻辑
# ============================================================
def calc_intraday_rush(df_1min: pd.DataFrame) -> dict:
    """计算尾盘抢筹强度"""
    if df_1min is None or len(df_1min) < 10:
        return {"label": "数据不足", "score": 0, "detail": ""}
    try:
        last_30 = df_1min.tail(30)
        if len(last_30) < 5:
            return {"label": "数据不足", "score": 0, "detail": ""}
        vols = last_30["volume"].values
        closes = last_30["close"].values
        total_vol = vols.sum()
        last5_vol = vols[-5:].sum() if len(vols) >= 5 else total_vol
        last5_ratio = last5_vol / total_vol if total_vol > 0 else 0
        x = np.arange(len(closes))
        slope = np.polyfit(x, closes, 1)[0]
        slope_factor = max(slope / closes[0] * 100, 0.0) if closes[0] != 0 else 0.0
        score = last5_ratio * 50 + min(slope_factor * 10, 50)
        score = min(score, 100)
        if score > 70 and last5_ratio > 0.25 and slope_factor > 0.08:
            strength = "强" if last5_ratio > 0.35 else "中"
            label = f"真抢筹({strength})"
        elif score > 50:
            label = "真抢筹(弱)"
        elif score > 30:
            label = "偏弱"
        else:
            label = "无抢筹"
        return {
            "label": label,
            "score": round(score, 1),
            "detail": f"尾盘量比{last5_ratio:.1%}，斜率{slope_factor:.3f}",
        }
    except Exception:
        return {"label": "异常", "score": 0, "detail": ""}

def run_selection(enable_rush: bool = True, max_stocks: int = 30):
    """执行完整选股流程"""
    now = beijing_now()
    # 交易日判断：非交易日直接返回
    if not is_trading_day():
        st.warning("⚠️ 今日非交易日（周一至周五为交易日），请于交易日运行时再试")
        return None
    # 判断是否为尾盘时段
    tail_time = is_tail_time()
    # 非尾盘时段自动禁用抢筹分析
    if not tail_time:
        enable_rush = False
    # 将实际状态写入 session_state
    st.session_state["rush_actual_enabled"] = enable_rush
    st.session_state["rush_auto_disabled"] = not tail_time
    # 非尾盘提示（在进度条之前显示）
    if not tail_time:
        st.info("ℹ️ 当前非尾盘时段（14:30-15:00），抢筹分析已自动跳过。可查看历史数据或手动运行。")
    # 进度条
    status_text = st.empty()
    progress = st.progress(0.0, text="正在初始化...")
    status_text.text("⏳ 准备获取实时行情...")
    df = fetch_realtime_quotes()
    if df is None:
        st.error("❌ 无法获取实时行情，请检查网络或稍后重试")
        return None
    config = get_config()
    total = len(df)
    results = []
    rush_cache = {}
    errors = 0
    for i, (idx, row) in enumerate(df.iterrows()):
        if i % 10 == 0:
            progress.progress((i + 1) / total, text=f"⏳ 正在分析 {i+1}/{total} ...")
            status_text.text(f"📊 已筛选 {len(results)} 只候选股（已处理 {i+1}/{total}）")
        try:
            symbol = str(row["代码"]).zfill(6)
            name = str(row["名称"])
            chg = float(row["涨跌幅"])
            vol_ratio = float(row["量比"])
            amount = float(row["成交额"])
            turnover = float(row["换手率"])
            close = float(row.get("最新价", 0))
            # 筛选条件
            if not (config["pct_min"] < chg < config["pct_max"]):
                continue
            if vol_ratio < config["vol_ratio_min"]:
                continue
            if not (config["turnover_min"] < turnover < config["turnover_max"]):
                continue
            if amount < config["amount_min"]:
                continue
            # 抢筹分析（如果启用）
            if enable_rush:
                if symbol not in rush_cache:
                    rush_cache[symbol] = calc_intraday_rush(fetch_intraday_minute(symbol))
                rush = rush_cache[symbol]
            else:
                rush = {"label": "-", "score": 0, "detail": "-"}
            results.append({
                "代码": symbol,
                "名称": name,
                "涨跌幅%": round(chg, 2),
                "量比": round(vol_ratio, 2),
                "换手率%": round(turnover, 2),
                "成交额亿": round(amount / 1e8, 2),
                "最新价": round(close, 2),
                "抢筹": rush["label"],
                "抢筹评分": rush["score"],
                "_sort_key": vol_ratio,
            })
        except Exception:
            errors += 1
            continue
    progress.progress(1.0, text="✅ 选股完成！")
    status_text.text(f"✅ 选股完成！共找到 {len(results)} 只候选股")
    if not results:
        st.warning("⚠️ 未找到符合条件的股票，请调整筛选条件或稍后重试")
        return None
    # 按量比排序
    results.sort(key=lambda x: x["_sort_key"], reverse=True)
    df_result = pd.DataFrame(results[:max_stocks])
    df_result = df_result.drop(columns=["_sort_key"])
    # 统计摘要
    summary = {
        "total_stocks": total,
        "passed": len(results),
        "displayed": min(len(results), max_stocks),
        "avg_pct": round(df_result["涨跌幅%"].mean(), 2),
        "max_vol_ratio": round(df_result["量比"].max(), 2),
        "rush_distribution": df_result["抢筹"].value_counts().to_dict(),
        "errors": errors,
    }
    st.session_state["last_summary"] = summary
    return df_result

# ============================================================
# 结果存储
# ============================================================
def save_daily_results(df: pd.DataFrame):
    """保存当日选股结果（含空数据检查和30天清理）"""
    if df is None or df.empty:
        return
    today = today_str()
    if "history_results" not in st.session_state:
        st.session_state["history_results"] = {}
    st.session_state["history_results"][today] = {
        "data": df.to_dict(orient="records"),
        "timestamp": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 30天清理机制
    if len(st.session_state["history_results"]) > 30:
        sorted_dates = sorted(st.session_state["history_results"].keys())
        for old_date in sorted_dates[:-30]:
            del st.session_state["history_results"][old_date]
    st.session_state["last_results"] = df
    st.session_state["last_results_ts"] = beijing_now().strftime("%Y-%m-%d %H:%M:%S")

def load_daily_results(date_str: str) -> pd.DataFrame | None:
    """加载指定日期的选股结果"""
    history = st.session_state.get("history_results", {})
    record = history.get(date_str)
    if record is None:
        return None
    return pd.DataFrame(record["data"])

def load_last_results() -> tuple[pd.DataFrame | None, str | None]:
    """加载最近一次选股结果"""
    df = st.session_state.get("last_results")
    ts = st.session_state.get("last_results_ts")
    return df, ts

# ============================================================
# 页面渲染函数
# ============================================================
def render_summary_panel():
    """渲染统计摘要面板"""
    summary = st.session_state.get("last_summary")
    if summary is None:
        return
    st.subheader("📊 选股统计摘要")
    cols = st.columns(6)
    cols[0].metric("总股票数", summary["total_stocks"])
    cols[1].metric("通过筛选", f"{summary['passed']} 只")
    cols[2].metric("展示数量", f"{summary['displayed']} 只")
    cols[3].metric("平均涨幅", f"{summary['avg_pct']}%")
    cols[4].metric("最大量比", summary["max_vol_ratio"])
    cols[5].metric("数据异常", summary["errors"])
    # A50 整合到摘要面板
    a50_change = fetch_a50_change()
    rush_str = ""
    if summary.get("rush_distribution"):
        rush_str = " | ".join([f"{k}:{v}" for k, v in summary["rush_distribution"].items()])
    info_parts = []
    if a50_change is not None:
        sign = "+" if a50_change >= 0 else ""
        info_parts.append(f"富时A50：{sign}{a50_change:.2f}%")
    if rush_str:
        info_parts.append(f"抢筹分布：{rush_str}")
    if info_parts:
        st.caption("🏷️ " + "　|　".join(info_parts))
    st.divider()

def render_yesterday_review():
    """渲染昨日回顾面板"""
    today = today_str()
    yesterday = (beijing_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    df_today = load_daily_results(today)
    df_yesterday = load_daily_results(yesterday)
    if df_today is None and df_yesterday is None:
        st.caption("📅 暂无历史数据，运行选股后自动记录")
        return
    st.subheader("📅 历史对比")
    col1, col2 = st.columns(2)
    with col1:
        st.caption(f"📌 今日 ({today})")
        if df_today is not None and not df_today.empty:
            st.dataframe(df_today[["代码", "名称", "涨跌幅%", "量比", "抢筹"]],
                        use_container_width=True, hide_index=True)
        else:
            st.text("今日暂无数据")
    with col2:
        st.caption(f"📌 昨日 ({yesterday})")
        if df_yesterday is not None and not df_yesterday.empty:
            st.dataframe(df_yesterday[["代码", "名称", "涨跌幅%", "量比", "抢筹"]],
                        use_container_width=True, hide_index=True)
        else:
            st.text("昨日暂无数据")
    # 连续上榜分析
    if df_today is not None and df_yesterday is not None:
        today_codes = set(df_today["代码"].astype(str))
        yesterday_codes = set(df_yesterday["代码"].astype(str))
        overlap = today_codes & yesterday_codes
        if overlap:
            overlap_df = df_today[df_today["代码"].astype(str).isin(overlap)].copy()
            if "抢筹" in overlap_df.columns:
                overlap_df["抢筹"] = overlap_df["抢筹"].apply(
                    lambda x: f"⭐ {x}" if not str(x).startswith("⭐") else x
                )
            st.success(f"⭐ 连续上榜：{len(overlap)} 只股票")
            st.dataframe(overlap_df[["代码", "名称", "涨跌幅%", "量比", "抢筹"]],
                        use_container_width=True, hide_index=True)
        else:
            st.info("📊 今日与昨日无重叠股票，市场风格可能切换")


# ============================================================
# Streamlit 主页面
# ============================================================
st.set_page_config(
    page_title="尾盘智能选股",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)
st.title("📈 尾盘智能选股工具")
st.caption("基于 akshare 实时数据 + 尾盘抢筹分析（A股专用）")

# ---- 侧边栏 ----
with st.sidebar:
    st.header("⚙️ 参数设置")
    now = beijing_now()
    tail_time = is_tail_time()
    trading_day = is_trading_day()
    # 时段提示
    if tail_time and trading_day:
        st.success("✅ 已进入尾盘时段（14:30-15:00），可启用抢筹分析")
    elif trading_day:
        st.info("ℹ️ 当前为交易时段，但非尾盘时间（尾盘分析将在14:30后可用）")
    else:
        st.warning("⚠️ 今日非交易日，展示历史数据或手动运行")
    # 抢筹分析开关（非尾盘时段禁用）
    enable_rush = st.checkbox(
        "🔍 启用尾盘抢筹分析",
        value=tail_time and trading_day,
        disabled=not (tail_time and trading_day),
        help="仅在尾盘时段（14:30-15:00）可用" if not (tail_time and trading_day) else "分析尾盘抢筹强度"
    )
    if not (tail_time and trading_day):
        st.caption("ℹ️ 抢筹分析已禁用（非尾盘时段）")
    else:
        if enable_rush:
            st.caption("✅ 抢筹分析：已启用")
        else:
            st.caption("ℹ️ 抢筹分析：已禁用")
    max_stocks = st.number_input("📋 最多显示候选股数", 10, 100, 30, 5)
    st.divider()
    # 运行按钮
    if st.button("🔄 运行选股", use_container_width=True, type="primary"):
        with st.spinner("正在运行选股逻辑..."):
            df = run_selection(enable_rush, max_stocks)
            if df is not None:
                save_daily_results(df)
                st.success(f"✅ 选股完成，共 {len(df)} 只候选股")
                st.rerun()
    if st.button("🗑️ 清除缓存并刷新", use_container_width=True):
        st.cache_data.clear()
        st.session_state["last_summary"] = None
        st.rerun()
    if st.button("🗑️ 清空历史数据", use_container_width=True):
        for key in ["history_results", "last_results", "last_results_ts", "last_summary", "rush_auto_disabled"]:
            if key in st.session_state:
                del st.session_state[key]
        st.success("✅ 历史数据已清空")
        st.rerun()
    st.divider()
    st.caption(f"🕐 当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}")
    st.caption("数据来源：akshare")

# ---- 主页面 ----
# 渲染统计摘要（含A50）
render_summary_panel()
# 加载并显示最近结果
df_result, cached_ts = load_last_results()
if df_result is not None and not df_result.empty:
    st.subheader(f"📊 候选股票列表（共 {len(df_result)} 只）")
    if cached_ts:
        st.caption(f"⏱️ 缓存时间戳：{cached_ts}")
    st.dataframe(
        df_result,
        column_config={
            "代码": st.column_config.TextColumn("代码", width="small"),
            "名称": st.column_config.TextColumn("名称", width="medium"),
            "涨跌幅%": st.column_config.NumberColumn("涨跌幅%", format="%.2f%%"),
            "量比": st.column_config.NumberColumn("量比", format="%.2f"),
            "换手率%": st.column_config.NumberColumn("换手率%", format="%.2f%%"),
            "成交额亿": st.column_config.NumberColumn("成交额亿", format="%.2f"),
            "最新价": st.column_config.NumberColumn("最新价", format="%.2f"),
            "抢筹": st.column_config.TextColumn("抢筹", width="medium"),
            "抢筹评分": st.column_config.NumberColumn("抢筹评分", format="%.1f"),
        },
        use_container_width=True,
        hide_index=True,
    )
    # 导出CSV
    csv_data = df_result.to_csv(index=False, encoding="utf-8-sig")
    st.download_button(
        label="📥 导出 CSV",
        data=csv_data,
        file_name=f"尾盘选股_{now.strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
    )
else:
    st.info("💡 暂无选股结果，请点击侧边栏「运行选股」按钮")
st.divider()
render_yesterday_review()
st.divider()
st.caption(f"🔄 数据更新时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（北京时间）")
st.caption("⚠️ 以上内容仅供参考，不构成投资建议。股市有风险，投资需谨慎。")
