# app.py - 尾盘智能选股工具（合并最终版）
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

# ============================================================
# 全局配置（统一管理筛选阈值）
# ============================================================
@st.cache_resource
def get_config():
    return {
        "pct_min": 2.0,
        "pct_max": 7.0,
        "vol_ratio_min": 1.2,
        "turnover_min": 3.0,
        "turnover_max": 15.0,
        "amount_min": 1e8,          # 成交额 ≥ 1亿
        "max_stocks": 30,           # 最多显示候选股数
        "a50_weight": 0.3,          # A50夜盘权重（保留）
        "cache_ttl": 600,           # 缓存有效期（秒）
    }

# ============================================================
# 列名兼容工具
# ============================================================
def _get_column(df: pd.DataFrame, candidates: list) -> str | None:
    """在 DataFrame 中查找第一个匹配的列名"""
    for col in candidates:
        if col in df.columns:
            return col
    for col in df.columns:
        for c in candidates:
            if c in col or col in c:
                return col
    return None

# ============================================================
# 数据获取（缓存，带降级容错）
# ============================================================
@st.cache_data(ttl=600)
def fetch_realtime_quotes():
    """获取全市场实时行情（10分钟缓存）"""
    try:
        df = ak.stock_zh_a_spot_em()
        required_cols = ["代码", "名称", "涨跌幅", "量比", "成交额", "换手率", "最新价"]
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            st.warning(f"⚠️ 缺失列: {missing}，请检查数据源接口")
            return None
        return df
    except Exception as e:
        st.error(f"获取实时行情失败: {e}")
        return None

@st.cache_data(ttl=600)
def fetch_daily_kline(symbol: str, days: int = 30):
    """获取个股日线数据（用于MA20等）"""
    try:
        df = ak.stock_zh_a_hist(symbol=symbol, period="daily", adjust="qfq", start_date="20240101")
        if df.empty:
            return None
        close_col = _get_column(df, ["收盘", "close"])
        if close_col is None:
            return None
        df = df.rename(columns={close_col: "close"})
        return df.tail(days)
    except Exception:
        return None

@st.cache_data(ttl=60)
def fetch_intraday_minute(symbol: str):
    """获取当日1分钟分时数据（用于抢筹分析，带降级）"""
    # 尝试多种接口，第一个成功即用
    methods = [
        ("stock_zh_a_hist_min_em", lambda: ak.stock_zh_a_hist_min_em(symbol=symbol, period="1", adjust="")),
        ("stock_zh_a_spot_min", lambda: ak.stock_zh_a_spot_min(symbol=symbol, period="1")),
    ]
    for name, func in methods:
        try:
            df = func()
            if df is None or df.empty:
                continue
            # 统一列名
            vol_col = _get_column(df, ["成交量", "volume", "vol"])
            close_col = _get_column(df, ["收盘", "close", "price"])
            time_col = _get_column(df, ["时间", "datetime", "time"])
            if vol_col is None or close_col is None:
                continue
            df = df.rename(columns={vol_col: "volume", close_col: "close"})
            if time_col:
                df["time"] = df[time_col]
            return df
        except Exception:
            continue
    return None

@st.cache_data(ttl=300)
def fetch_a50_night_change() -> float:
    """获取富时A50夜盘涨跌幅（5分钟缓存，带降级）"""
    methods = [
        ("futures_zh_minute_sina", lambda: ak.futures_zh_minute_sina(symbol="A50")),
        ("stock_fta50_hist_sina", lambda: ak.stock_fta50_hist_sina(symbol="a50")),
    ]
    for name, func in methods:
        try:
            df = func()
            if df is None or df.empty or len(df) < 2:
                continue
            price_col = _get_column(df, ["收盘", "close", "最新价", "price", "最新"])
            if price_col is None:
                continue
            prices = pd.to_numeric(df[price_col], errors="coerce").dropna()
            if len(prices) < 2:
                continue
            latest, prev = prices.iloc[-1], prices.iloc[-2]
            if prev == 0:
                return 0.0
            return round((latest - prev) / prev * 100, 2)
        except Exception:
            continue
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
        # 价格斜率
        x = np.arange(len(closes))
        slope = np.polyfit(x, closes, 1)[0]
        slope_factor = max(slope / closes[0] * 100, 0.0) if closes[0] != 0 else 0.0
        # 综合评分
        score = last5_ratio * 50 + min(slope_factor * 10, 50)
        score = min(score, 100)
        # 强度分级
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
    progress = st.progress(0.0, text="⏰ 尾盘时段已到，正在运行选股逻辑，请稍候...")
    status_text = st.empty()
    df = fetch_realtime_quotes()
    if df is None:
        st.error("无法获取实时行情，请稍后重试")
        return None
    config = get_config()
    total = len(df)
    results = []
    rush_cache = {}
    errors = 0
    for i, row in df.iterrows():
        if i % 5 == 0:
            progress.progress((i + 1) / total, text=f"⏳ 正在分析 {i+1}/{total} ...")
            status_text.text(f"📊 已筛选 {len(results)} 只候选股")
        try:
            symbol = str(row["代码"]).zfill(6)
            name = str(row["名称"])
            chg = float(row["涨跌幅"])
            vol_ratio = float(row["量比"])
            amount = float(row["成交额"])
            turnover = float(row["换手率"])
            close = float(row.get("最新价", 0))
            # 核心筛选
            if not (config["pct_min"] < chg < config["pct_max"]):
                continue
            if vol_ratio < config["vol_ratio_min"]:
                continue
            if not (config["turnover_min"] < turnover < config["turnover_max"]):
                continue
            if amount < config["amount_min"]:
                continue
            # 抢筹分析
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
    if not results:
        st.warning("未找到符合条件的股票，请调整筛选条件")
        return None
    results.sort(key=lambda x: x["_sort_key"], reverse=True)
    df_result = pd.DataFrame(results[:max_stocks])
    df_result = df_result.drop(columns=["_sort_key"])
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
# 结果存储（按日期索引）
# ============================================================
def save_daily_results(df: pd.DataFrame):
    """保存当日选股结果"""
    today = today_str()
    if "history_results" not in st.session_state:
        st.session_state["history_results"] = {}
    st.session_state["history_results"][today] = {
        "data": df.to_dict(orient="records"),
        "timestamp": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
    }
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
    """渲染选股摘要面板"""
    summary = st.session_state.get("last_summary")
    if summary is None:
        return
    cols = st.columns(6)
    cols[0].metric("📊 总股票数", summary["total_stocks"])
    cols[1].metric("✅ 通过筛选", f"{summary['passed']} 只")
    cols[2].metric("📋 展示数量", f"{summary['displayed']} 只")
    cols[3].metric("📈 平均涨幅", f"{summary['avg_pct']}%")
    cols[4].metric("🔥 最大量比", summary["max_vol_ratio"])
    cols[5].metric("⚠️ 数据异常", summary["errors"])
    if summary.get("rush_distribution"):
        rush_str = ", ".join([f"{k}:{v}" for k, v in summary["rush_distribution"].items()])
        st.caption(f"🏷️ 抢筹分布：{rush_str}")

def render_yesterday_review():
    """渲染昨日回顾面板（修正：今日无数据时只展示昨日）"""
    today = today_str()
    yesterday = (beijing_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    df_today = load_daily_results(today)
    df_yesterday = load_daily_results(yesterday)
    if df_today is None and df_yesterday is None:
        st.caption("📅 暂无历史数据，运行选股后自动记录")
        return
    st.subheader("📅 昨日回顾")
    col1, col2 = st.columns(2)
    with col1:
        st.caption(f"📆 今日 ({today})")
        if df_today is not None and not df_today.empty:
            st.dataframe(df_today[["代码", "名称", "涨跌幅%", "量比", "抢筹"]], use_container_width=True)
        else:
            st.text("今日暂无数据")
    with col2:
        st.caption(f"📆 昨日 ({yesterday})")
        if df_yesterday is not None and not df_yesterday.empty:
            st.dataframe(df_yesterday[["代码", "名称", "涨跌幅%", "量比", "抢筹"]], use_container_width=True)
        else:
            st.text("昨日暂无数据")
    # 只有今日和昨日都有数据时才做对比
    if df_today is not None and df_yesterday is not None:
        today_codes = set(df_today["代码"].astype(str))
        yesterday_codes = set(df_yesterday["代码"].astype(str))
        overlap = today_codes & yesterday_codes
        if overlap:
            overlap_df = df_today[df_today["代码"].astype(str).isin(overlap)]
            st.success(f"⭐ 连续上榜：{len(overlap)} 只股票")
            st.dataframe(overlap_df[["代码", "名称", "涨跌幅%", "量比", "抢筹"]], use_container_width=True)
        else:
            st.info("今日与昨日无重叠股票，市场风格可能切换")

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
st.caption("基于 akshare 实时数据 + 尾盘抢筹分析")
# ---- 侧边栏 ----
with st.sidebar:
    st.header("⚙️ 参数设置")
    now = beijing_now()
    is_tail_session = now.hour >= 14 and now.hour < 16
    if is_tail_session:
        st.success("✅ 已进入尾盘时段（14:00-16:00）")
    else:
        st.warning("⚠️ 当前非尾盘时段，展示上次结果或手动运行")
    enable_rush = st.checkbox("🔍 启用尾盘抢筹分析", value=True)
    max_stocks = st.number_input("📋 最多显示候选股数", 10, 100, 30, 5)
    st.divider()
    if st.button("🔄 运行选股", use_container_width=True):
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
    st.divider()
    st.caption(f"🕐 当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}")
    st.caption("数据来源：akshare")
# ---- 主页面 ----
render_summary_panel()
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
