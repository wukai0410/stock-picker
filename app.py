# app.py - 尾盘智能选股工具 v1.0
# -*- coding: utf-8 -*-
"""
尾盘智能选股工具
数据源：东方财富 API 主力 + akshare 兜底
选股逻辑：基础排雷 → 核心筛选 → 综合评分 → 分时抢筹 → 量化测压
"""

import streamlit as st
import pandas as pd
import numpy as np
import requests
import json
import time
import warnings
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")

# ════════════════════════════════════════════════════════════
# 时区 & 时间工具
# ════════════════════════════════════════════════════════════
CST = ZoneInfo("Asia/Shanghai")

def beijing_now() -> datetime:
    return datetime.now(CST)

def today_str() -> str:
    return beijing_now().strftime("%Y-%m-%d")

def is_tail_window() -> bool:
    """是否处于尾盘选股窗口 14:50-15:00"""
    now = beijing_now()
    return now.hour == 14 and now.minute >= 50

def is_rush_window() -> bool:
    """是否处于抢筹分析窗口 14:30-15:00"""
    now = beijing_now()
    if now.hour == 14 and now.minute >= 30:
        return True
    if now.hour == 15 and now.minute == 0:
        return True
    return False

def is_trading_day() -> bool:
    return beijing_now().weekday() < 5


# ════════════════════════════════════════════════════════════
# 页面配置
# ════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="尾盘智能选股",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 红涨绿跌配色
STYLE_CSS = """
<style>
    .stApp { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    .up { color: #e74c3c; font-weight: 600; }
    .down { color: #2ecc71; font-weight: 600; }
    .score-high { background-color: #d4edda !important; }
    div[data-testid="stDataFrame"] td { font-size: 0.9rem; }
    @media (max-width: 768px) {
        div[data-testid="stDataFrame"] td { font-size: 0.75rem; }
    }
</style>
"""
st.markdown(STYLE_CSS, unsafe_allow_html=True)


# ════════════════════════════════════════════════════════════
# 缓存的数据获取函数
# ════════════════════════════════════════════════════════════

@st.cache_data(ttl=300, show_spinner=False)
def fetch_realtime_quotes():
    """
    从东方财富 API 获取全 A 股实时行情。
    返回 DataFrame，含：代码、名称、最新价、涨跌幅、量比、换手率、成交额、成交量、流通市值
    """
    all_stocks = []
    page_size = 100
    max_pages = 60  # 最多拉 6000 只

    for page in range(1, max_pages + 1):
        try:
            url = (
                "https://push2.eastmoney.com/api/qt/clist/get?"
                "pn={page}&pz={size}&po=1&np=1&fltt=2&invt=2&"
                "fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&"
                "fields=f2,f3,f4,f5,f6,f7,f8,f10,f12,f14,f15,f16,f17,f18,f20,f21"
            ).format(page=page, size=page_size)
            resp = requests.get(url, timeout=15)
            data = resp.json()
            items = data.get("data", {}).get("diff", [])
            if not items:
                break
            for item in items:
                all_stocks.append({
                    "代码": str(item.get("f12", "")),
                    "名称": str(item.get("f14", "")),
                    "最新价": _safe_float(item.get("f2")),
                    "涨跌幅": _safe_float(item.get("f3")),
                    "涨跌额": _safe_float(item.get("f4")),
                    "成交量": _safe_float(item.get("f5")),
                    "成交额": _safe_float(item.get("f6")),
                    "换手率": _safe_float(item.get("f8")),
                    "量比": _safe_float(item.get("f10")),
                    "最高": _safe_float(item.get("f15")),
                    "最低": _safe_float(item.get("f16")),
                    "开盘价": _safe_float(item.get("f17")),
                    "昨收": _safe_float(item.get("f18")),
                    "流通市值": _safe_float(item.get("f20")),
                    "总市值": _safe_float(item.get("f21")),
                })
            if len(items) < page_size:
                break
        except Exception:
            continue

    if not all_stocks:
        return None

    df = pd.DataFrame(all_stocks)
    # 数值类型转换
    for col in ["最新价", "涨跌幅", "涨跌额", "成交量", "成交额", "换手率", "量比",
                "最高", "最低", "开盘价", "昨收", "流通市值", "总市值"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


@st.cache_data(ttl=300, show_spinner=False)
def fetch_kline(symbol: str, days: int = 30):
    """
    获取个股近期日 K 线，用于计算 MA20。
    优先东方财富，失败则回退 akshare。
    """
    # --- 东方财富日K ---
    try:
        secid = f"1.{symbol}" if symbol.startswith("6") else f"0.{symbol}"
        url = (
            f"https://push2his.eastmoney.com/api/qt/stock/kline/get?"
            f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6&"
            f"fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&"
            f"klt=101&fqt=1&end=20500101&lmt={days + 5}"
        )
        resp = requests.get(url, timeout=10)
        data = resp.json()
        klines = data.get("data", {}).get("klines", [])
        if klines:
            records = []
            for line in klines:
                parts = line.split(",")
                records.append({"close": float(parts[2])})
            return pd.DataFrame(records)
    except Exception:
        pass

    # --- akshare 兜底 ---
    try:
        import akshare as ak
        df = ak.stock_zh_a_hist(symbol=symbol, period="daily", adjust="qfq",
                                start_date=(beijing_now() - timedelta(days=60)).strftime("%Y%m%d"))
        if df is not None and not df.empty:
            close_col = None
            for c in ["收盘", "close"]:
                if c in df.columns:
                    close_col = c
                    break
            if close_col:
                df["close"] = pd.to_numeric(df[close_col], errors="coerce")
                return df[["close"]].tail(days + 5)
    except Exception:
        pass

    return None


@st.cache_data(ttl=120, show_spinner=False)
def fetch_intraday(symbol: str):
    """
    获取当日 1 分钟分时数据，用于抢筹分析和 VWAP 计算。
    """
    try:
        import akshare as ak
        df = ak.stock_zh_a_hist_min_em(symbol=symbol, period="1", adjust="")
        if df is None or df.empty:
            return None
        # 列名兼容
        rename_map = {}
        for col in df.columns:
            if "时间" in col or "time" in col.lower():
                rename_map[col] = "time"
            elif "开盘" in col or "open" in col.lower():
                rename_map[col] = "open"
            elif "收盘" in col or "close" in col.lower():
                rename_map[col] = "close"
            elif "最高" in col or "high" in col.lower():
                rename_map[col] = "high"
            elif "最低" in col or "low" in col.lower():
                rename_map[col] = "low"
            elif "成交" in col or "volume" in col.lower() or "vol" in col.lower():
                rename_map[col] = "volume"
        df = df.rename(columns=rename_map)
        for c in ["close", "volume"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        if "close" in df.columns and "volume" in df.columns:
            return df.dropna(subset=["close", "volume"])
    except Exception:
        pass
    return None


@st.cache_data(ttl=300, show_spinner=False)
def fetch_a50_change() -> float:
    """获取富时 A50 期指近月涨跌幅"""
    try:
        # 东方财富全球期指接口
        url = "https://push2.eastmoney.com/api/qt/stock/get?secid=100.FS_A50&fields=f43,f44,f45,f46,f47,f48,f50,f51,f52,f57,f58,f60,f169,f170,f171"
        resp = requests.get(url, timeout=10)
        data = resp.json().get("data", {})
        if data:
            return _safe_float(data.get("f170", 0))
    except Exception:
        pass
    return 0.0


def _safe_float(val):
    """安全转换为 float，失败返回 0.0"""
    if val is None or val == "-" or val == "":
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


# ════════════════════════════════════════════════════════════
# 选股逻辑
# ════════════════════════════════════════════════════════════

def basic_filter(df: pd.DataFrame) -> pd.DataFrame:
    """基础排雷"""
    if df is None or df.empty:
        return df
    mask = pd.Series(True, index=df.index)
    # 剔除 ST
    mask &= ~df["名称"].str.contains(r"[*]?ST", na=False, regex=True)
    # 剔除停牌（成交量为 0）
    mask &= df["成交量"] > 0
    # 剔除涨幅 ≥ 9.5% 或 ≤ 1%
    mask &= (df["涨跌幅"] < 9.5) & (df["涨跌幅"] > 1.0)
    # 剔除流通市值 < 20亿 或 > 200亿
    mask &= (df["流通市值"] >= 20e8) & (df["流通市值"] <= 200e8)
    return df[mask].copy()


def calc_ma20(symbol: str) -> float | None:
    """计算个股 20 日均线"""
    df_k = fetch_kline(symbol, days=30)
    if df_k is None or len(df_k) < 20:
        return None
    return df_k["close"].tail(20).mean()


def calc_composite_score(vol_ratio: float, turnover: float, pct: float,
                         close: float, ma20: float | None) -> int:
    """综合评分（满分 100）"""
    score = 0
    # 量比
    if vol_ratio >= 2.5:
        score += 30
    elif vol_ratio >= 2.0:
        score += 20
    else:
        score += 10
    # 换手率
    if 5 <= turnover <= 8:
        score += 25
    elif 3 <= turnover <= 10:
        score += 15
    # 涨幅
    if pct >= 4:
        score += 25
    elif pct >= 3:
        score += 15
    # MA20 偏离
    if ma20 is not None and ma20 > 0:
        deviation = (close - ma20) / ma20 * 100
        if deviation > 5:
            score += 20
        elif deviation > 3:
            score += 10
    return min(score, 100)


def analyze_rush(symbol: str, close: float) -> dict:
    """
    分时抢筹识别
    返回：{ "label": str, "score_adjust": int, "momentum": float, "vwap": float }
    """
    default = {"label": "-", "score_adjust": 0, "momentum": 0.0, "vwap": close}
    if not is_rush_window():
        return default

    df_min = fetch_intraday(symbol)
    if df_min is None or len(df_min) < 30:
        return {**default, "label": "数据不足"}

    # 取尾盘 30 分钟（最后 30 根 1 分钟 K 线）
    tail = df_min.tail(30)
    if len(tail) < 10:
        return {**default, "label": "数据不足"}

    closes = tail["close"].values
    volumes = tail["volume"].values

    # 价格斜率（线性回归）
    x = np.arange(len(closes))
    if closes[0] == 0:
        return default
    slope = np.polyfit(x, closes, 1)[0]
    slope_pct = slope / closes[0] * 100  # 每根 K 线百分比变化

    # 最后 5 分钟涨幅占尾盘总涨幅的比例
    total_chg = closes[-1] - closes[0]
    last5_chg = closes[-1] - closes[-6] if len(closes) >= 6 else total_chg
    if total_chg <= 0:
        last5_ratio = 0
    else:
        last5_ratio = last5_chg / total_chg

    # 尾盘量能集中度（最后 5 分钟量 / 尾盘 30 分钟总成交）
    total_vol = volumes.sum()
    last5_vol = volumes[-5:].sum() if len(volumes) >= 5 else total_vol
    vol_concentration = last5_vol / total_vol if total_vol > 0 else 0

    # 判定
    # "真抢筹"：斜率平缓（<0.003 每根即约 45°推升）且最后 5 分钟涨幅 < 60%
    if slope_pct < 0.003 and last5_ratio < 0.6 and total_chg > 0:
        label = "真抢筹"
        score_adjust = 10
    # "诱多嫌疑"：斜率陡峭（≥0.008 即约 80°拉升）且最后 5 分钟涨幅 ≥ 60%
    elif slope_pct >= 0.008 and last5_ratio >= 0.6:
        label = "诱多嫌疑"
        score_adjust = -10
    elif total_chg > 0 and vol_concentration > 0.3:
        label = "尾盘放量"
        score_adjust = 5
    else:
        label = "无信号"
        score_adjust = 0

    # VWAP = 成交均价（当日全部分时数据）
    if "volume" in df_min.columns and "close" in df_min.columns:
        all_v = df_min["volume"].values
        all_c = df_min["close"].values
        total_money = np.sum(all_v * all_c)
        total_vol_all = np.sum(all_v)
        vwap = total_money / total_vol_all if total_vol_all > 0 else close
    else:
        vwap = close

    return {
        "label": label,
        "score_adjust": score_adjust,
        "momentum": round(slope_pct, 6),  # 尾盘动能因子
        "vwap": round(vwap, 2),
    }


def calc_pressure_metrics(close: float, vwap: float, momentum: float,
                          a50_change: float) -> dict:
    """
    量化测压模块
    返回：{ "premium": float, "profit_loss_ratio": float }
    """
    # 预期开盘溢价
    premium = round(momentum * 100 * 0.6 + a50_change * 0.4, 2)

    # 盈亏比
    resistance = close * 1.025  # 压力位
    if vwap > 0 and (close - vwap) > 0.01:
        profit_loss = round((resistance - close) / (close - vwap), 2)
    else:
        profit_loss = round((resistance - close) / (close * 0.005), 2) if close > 0 else 0

    return {"premium": premium, "profit_loss_ratio": profit_loss}


def run_selection(pct_min: float, pct_max: float, turnover_min: float,
                  turnover_max: float) -> pd.DataFrame | None:
    """
    主选股流程
    """
    # 1. 获取行情
    df = fetch_realtime_quotes()
    if df is None or df.empty:
        st.error("❌ 无法获取实时行情数据")
        return None

    # 2. 基础排雷
    df = basic_filter(df)
    if df.empty:
        st.warning("⚠️ 基础排雷后无剩余股票")
        return None

    # 3. 核心筛选
    df = df[
        (df["涨跌幅"] >= pct_min) & (df["涨跌幅"] <= pct_max) &
        (df["换手率"] >= turnover_min) & (df["换手率"] <= turnover_max) &
        (df["量比"] > 1.5)
    ].copy()
    if df.empty:
        st.warning("⚠️ 核心筛选后无剩余股票，请放宽条件")
        return None

    # 4. 逐只分析
    a50 = fetch_a50_change()
    results = []
    total = len(df)
    progress = st.progress(0, text="正在逐只分析...")
    status = st.empty()

    for i, (_, row) in enumerate(df.iterrows()):
        symbol = str(row["代码"]).zfill(6)
        name = row["名称"]
        close = row["最新价"]
        pct = row["涨跌幅"]
        vol_ratio = row["量比"]
        turnover = row["换手率"]

        progress.progress((i + 1) / total, text=f"⏳ {i+1}/{total} {name}")
        status.text(f"📊 已通过 {len(results)} 只")

        try:
            # MA20
            ma20 = calc_ma20(symbol)
            # MA20 过滤
            if ma20 is not None and close <= ma20:
                continue

            # 综合评分
            score = calc_composite_score(vol_ratio, turnover, pct, close, ma20)

            # 分时抢筹
            rush = analyze_rush(symbol, close)
            final_score = score + rush["score_adjust"]
            final_score = max(0, min(100, final_score))

            # 量化测压
            pressure = calc_pressure_metrics(close, rush["vwap"], rush["momentum"], a50)

            # 建议价格
            if ma20 and ma20 > 0:
                buy_price = round(ma20 * 1.01, 2)
                stop_loss = round(ma20 * 0.97, 2)
            else:
                buy_price = round(close * 0.98, 2)
                stop_loss = round(close * 0.92, 2)

            results.append({
                "代码": symbol,
                "名称": name,
                "最新价": round(close, 2),
                "开盘价": row.get("开盘价", "-"),
                "昨收": row.get("昨收", "-"),
                "涨跌幅": round(pct, 2),
                "涨跌额": row.get("涨跌额", "-"),
                "最高": row.get("最高", "-"),
                "最低": row.get("最低", "-"),
                "换手率": round(turnover, 2),
                "量比": round(vol_ratio, 2),
                "成交额亿": round(row["成交额"] / 1e8, 2) if row["成交额"] else 0,
                "流通市值亿": round(row["流通市值"] / 1e8, 2) if row["流通市值"] else 0,
                "MA20": round(ma20, 2) if ma20 else "-",
                "综合评分": final_score,
                "抢筹信号": rush["label"],
                "预期开盘溢价(%)": pressure["premium"],
                "盈亏比": pressure["profit_loss_ratio"],
                "建议购入价": buy_price,
                "止损价": stop_loss,
                "_sort": final_score,
            })
        except Exception:
            continue

    progress.progress(1.0, text="✅ 选股完成")
    status.empty()

    if not results:
        st.warning("⚠️ 未找到符合条件的股票")
        return None

    results.sort(key=lambda x: x["_sort"], reverse=True)
    df_result = pd.DataFrame(results)
    df_result = df_result.drop(columns=["_sort"])

    # 保存到 session_state
    st.session_state["last_results"] = df_result
    st.session_state["last_results_ts"] = beijing_now().strftime("%Y-%m-%d %H:%M:%S")

    return df_result


# ════════════════════════════════════════════════════════════
# 样式工具
# ════════════════════════════════════════════════════════════

def color_pct(val):
    """涨跌幅着色：红涨绿跌"""
    if isinstance(val, (int, float)):
        if val > 0:
            return f"color: #e74c3c; font-weight: 600"
        elif val < 0:
            return f"color: #2ecc71; font-weight: 600"
    return ""

def highlight_high_score(row):
    """评分 ≥ 70 绿色背景"""
    if row.get("综合评分", 0) >= 70:
        return ["background-color: #d4edda"] * len(row)
    return [""] * len(row)


# ════════════════════════════════════════════════════════════
# 主界面
# ════════════════════════════════════════════════════════════

def main():
    now = beijing_now()
    tail_window = is_tail_window()

    st.title("📈 尾盘智能选股")
    st.caption("数据源：东方财富 + akshare ｜ 选股窗口：14:50-15:00")

    # ── 侧边栏 ──
    with st.sidebar:
        st.header("⚙️ 筛选条件")

        pct_min, pct_max = st.slider(
            "📊 涨幅范围 (%)", 0.0, 10.0, (2.0, 5.0), 0.1
        )
        turnover_min, turnover_max = st.slider(
            "🔄 换手率范围 (%)", 0.0, 30.0, (3.0, 10.0), 0.5
        )

        st.divider()
        st.caption(f"🕐 北京时间：{now.strftime('%Y-%m-%d %H:%M:%S')}")
        if tail_window:
            st.success("✅ 尾盘选股窗口已开启")
        else:
            st.info("ℹ️ 当前非尾盘时段（14:50-15:00）")

        st.divider()

        if st.button("🔄 运行选股", use_container_width=True, type="primary"):
            with st.spinner("正在获取数据并分析..."):
                df = run_selection(pct_min, pct_max, turnover_min, turnover_max)
                if df is not None and not df.empty:
                    st.success(f"✅ 共筛选出 {len(df)} 只候选股")
                    st.rerun()

        if st.button("🗑️ 清除缓存", use_container_width=True):
            st.cache_data.clear()
            st.session_state.pop("last_results", None)
            st.session_state.pop("last_results_ts", None)
            st.rerun()

    # ── 主区域 ──
    df_result = st.session_state.get("last_results")
    cached_ts = st.session_state.get("last_results_ts")

    # 尾盘窗口自动刷新
    if tail_window and is_trading_day():
        if "auto_ran" not in st.session_state or st.session_state.get("last_auto_ts") != now.strftime("%Y%m%d_%H%M"):
            st.session_state["auto_ran"] = True
            st.session_state["last_auto_ts"] = now.strftime("%Y%m%d_%H%M")
            with st.spinner("🔄 尾盘窗口自动刷新选股..."):
                df_result = run_selection(pct_min, pct_max, turnover_min, turnover_max)
    elif not tail_window and df_result is not None:
        st.info("💡 当前非尾盘时段，以下为最近一次选股结果（仅供参考）")

    if df_result is None or df_result.empty:
        st.info("👆 请点击侧边栏「运行选股」按钮开始分析")
        st.divider()
        st.caption("⚠️ 以上内容仅供参考，不构成投资建议。股市有风险，投资需谨慎。")
        return

    # ── 统计摘要 ──
    st.subheader("📊 选股统计")
    cols = st.columns(5)
    cols[0].metric("候选股数", f"{len(df_result)} 只")
    cols[1].metric("平均涨幅", f"{df_result['涨跌幅'].mean():.2f}%")
    cols[2].metric("最高评分", int(df_result["综合评分"].max()))
    cols[3].metric("平均量比", f"{df_result['量比'].mean():.2f}")
    cols[4].metric("真抢筹数", len(df_result[df_result["抢筹信号"] == "真抢筹"]))

    a50 = fetch_a50_change()
    rush_dist = df_result["抢筹信号"].value_counts().to_dict()
    rush_str = " | ".join([f"{k}: {v}" for k, v in rush_dist.items()])
    sign = "+" if a50 >= 0 else ""
    st.caption(f"🏷️ 富时A50：{sign}{a50:.2f}% ｜ 抢筹分布：{rush_str}")
    st.divider()

    # ── 结果表格 ──
    st.subheader(f"📋 候选股列表（{len(df_result)} 只）")
    if cached_ts:
        st.caption(f"⏱️ 数据时间：{cached_ts}")

    display_cols = [
        "代码", "名称", "最新价", "涨跌幅", "换手率", "量比",
        "成交额亿", "流通市值亿", "综合评分", "抢筹信号",
        "预期开盘溢价(%)", "盈亏比"
    ]

    df_display = df_result[display_cols].copy()

    # 应用样式
    styled = df_display.style \
        .apply(highlight_high_score, axis=1) \
        .map(color_pct, subset=["涨跌幅", "预期开盘溢价(%)"]) \
        .format({
            "最新价": "{:.2f}",
            "涨跌幅": "{:+.2f}%",
            "换手率": "{:.2f}%",
            "量比": "{:.2f}",
            "成交额亿": "{:.2f}",
            "流通市值亿": "{:.2f}",
            "预期开盘溢价(%)": "{:+.2f}%",
            "盈亏比": "{:.2f}",
        })

    st.dataframe(styled, use_container_width=True, hide_index=True,
                 column_config={
                     "代码": st.column_config.TextColumn("代码", width="small"),
                     "名称": st.column_config.TextColumn("名称", width="medium"),
                     "综合评分": st.column_config.NumberColumn("综合评分", format="%d"),
                     "抢筹信号": st.column_config.TextColumn("抢筹信号", width="small"),
                 })

    # ── 高分推荐 ──
    st.subheader("⭐ 高分推荐（评分 ≥ 70）")
    high = df_result[df_result["综合评分"] >= 70]
    if not high.empty:
        high_display = high[display_cols].style \
            .map(color_pct, subset=["涨跌幅", "预期开盘溢价(%)"]) \
            .format({
                "最新价": "{:.2f}",
                "涨跌幅": "{:+.2f}%",
                "换手率": "{:.2f}%",
                "量比": "{:.2f}",
                "成交额亿": "{:.2f}",
                "流通市值亿": "{:.2f}",
                "预期开盘溢价(%)": "{:+.2f}%",
                "盈亏比": "{:.2f}",
            })
        st.dataframe(high_display, use_container_width=True, hide_index=True)
    else:
        st.info("暂无评分 ≥ 70 的股票")

    # ── 详情查看 ──
    st.subheader("🔍 查看个股详情")
    col1, col2 = st.columns([3, 1])
    with col1:
        selected = st.selectbox(
            "选择股票",
            options=df_result["代码"].tolist(),
            format_func=lambda x: f"{x} - {df_result[df_result['代码']==x]['名称'].iloc[0]}",
            label_visibility="collapsed",
        )
    with col2:
        eastmoney_url = f"https://quote.eastmoney.com/concept/{selected}.html"
        st.link_button("🔗 打开东方财富详情", eastmoney_url, use_container_width=True)

    if selected:
        row = df_result[df_result["代码"] == selected].iloc[0]
        st.divider()
        st.subheader(f"📊 {row['名称']}（{row['代码']}）详细数据")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("最新价", f"{row['最新价']} 元", delta=f"{row['涨跌幅']:+.2f}%")
        c2.metric("综合评分", f"{int(row['综合评分'])} 分")
        c3.metric("抢筹信号", row["抢筹信号"])
        c4.metric("量比", f"{row['量比']:.2f}")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("开盘价", f"{row['开盘价']} 元")
        c2.metric("昨收", f"{row['昨收']} 元")
        c3.metric("最高", f"{row['最高']} 元")
        c4.metric("最低", f"{row['最低']} 元")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("换手率", f"{row['换手率']:.2f}%")
        c2.metric("成交额", f"{row['成交额亿']:.2f} 亿")
        c3.metric("流通市值", f"{row['流通市值亿']:.2f} 亿")
        c4.metric("MA20", f"{row['MA20']}" if row["MA20"] != "-" else "-")

        st.divider()
        c1, c2, c3 = st.columns(3)
        c1.metric("💰 建议购入价", f"{row['建议购入价']} 元")
        c2.metric("🛑 止损价", f"{row['止损价']} 元")
        c3.metric("📈 预期开盘溢价", f"{row['预期开盘溢价(%)']:+.2f}%")

        st.caption(f"📐 盈亏比：{row['盈亏比']:.2f}（压力位={row['最新价']*1.025:.2f}）")

    # ── CSV 导出 ──
    csv = df_result.to_csv(index=False, encoding="utf-8-sig")
    st.download_button(
        "📥 导出 CSV",
        data=csv,
        file_name=f"尾盘选股_{now.strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
    )

    # ── 风险提示 ──
    st.divider()
    st.caption(f"🔄 数据更新时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（北京时间）")
    st.caption("⚠️ 以上内容仅供参考，不构成投资建议。股市有风险，投资需谨慎。")


if __name__ == "__main__":
    main()
