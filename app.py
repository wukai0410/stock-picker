"""
尾盘智能选股工具
基于 Streamlit + akshare 的 A 股尾盘选股应用
"""

import streamlit as st
import pandas as pd
import numpy as np
import akshare as ak
from datetime import datetime, time, timezone, timedelta
import warnings

warnings.filterwarnings("ignore")

# ============================================================
# 页面配置
# ============================================================
st.set_page_config(
    page_title="尾盘智能选股",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ============================================================
# 时区定义 — 东八区（北京时间）
# ============================================================
CST = timezone(timedelta(hours=8))

def beijing_now() -> datetime:
    """返回北京时间 now"""
    return datetime.now(CST)

# ============================================================
# 全局常量
# ============================================================
TAIL_SESSION_START = time(14, 30)  # 尾盘开始时间
DISPLAY_START = time(14, 50)       # 刷新展示时间


def get_cache_ttl() -> int:
    """尾盘时段 TTL=600s，非尾盘 TTL=3600s"""
    now = beijing_now().time()
    if TAIL_SESSION_START <= now <= time(15, 0):
        return 600
    return 3600

# ============================================================
# 样式注入
# ============================================================
def inject_css():
    st.markdown(
        """
        <style>
        /* 全局字体 */
        html, body, [class*="css"] {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
        }
        /* 表格高亮行 */
        .highlight-row td {
            background-color: #d4edda !important;
            font-weight: 600;
        }
        /* 风险提示 */
        .risk-warning {
            text-align: center;
            color: #999;
            font-size: 0.85rem;
            margin-top: 2rem;
            padding: 1rem 0;
            border-top: 1px solid #e0e0e0;
        }
        /* 手机适配 */
        @media (max-width: 768px) {
            .stTable { font-size: 0.8rem; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ============================================================
# 缓存数据获取
# ============================================================
@st.cache_data(ttl=get_cache_ttl())
def fetch_realtime_quotes():
    """获取全 A 股实时行情"""
    try:
        df = ak.stock_zh_a_spot_em()
        return df
    except Exception as e:
        st.error(f"获取实时行情失败: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=get_cache_ttl())
def fetch_historical_kline(symbol: str, period: int = 30):
    """获取单只股票近 N 日历史收盘价，返回 MA20"""
    try:
        # symbol 格式: "000001" → 需要补全为 "sz000001" 或 "sh000001"
        code = _format_code(symbol)
        df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=(beijing_now() - pd.Timedelta(days=60)).strftime("%Y%m%d"), end_date=beijing_now().strftime("%Y%m%d"), adjust="qfq")
        if df.empty or "收盘" not in df.columns:
            return None
        closes = df["收盘"].tail(period)
        if len(closes) < 20:
            return None
        ma20 = closes.tail(20).mean()
        latest_close = closes.iloc[-1]
        return {"ma20": ma20, "close": latest_close, "closes": closes.tolist()}
    except Exception:
        return None


@st.cache_data(ttl=60)
def fetch_intraday_minute(symbol: str):
    """获取当日分时数据（1分钟线），兼容「收盘」和「close」列名"""
    try:
        df = ak.stock_zh_a_hist_min_em(symbol=symbol, period="1", adjust="")
        if df.empty:
            return None
        # 统一列名：如果有 "close" 列，重命名为 "收盘"
        rename_map = {}
        for c in df.columns:
            if c.lower() == "close":
                rename_map[c] = "收盘"
            elif "时间" in c and c != "时间":
                rename_map[c] = "时间"
        if rename_map:
            df = df.rename(columns=rename_map)
        return df
    except Exception:
        return None


def _format_code(symbol: str) -> str:
    """将 6 位代码转为带交易所前缀的代码"""
    if symbol.startswith("6"):
        return f"sh{symbol}"
    else:
        return f"sz{symbol}"


# ============================================================
# 选股逻辑
# ============================================================
def basic_filter(df: pd.DataFrame) -> pd.DataFrame:
    """基础排雷"""
    if df.empty:
        return df

    # 列名映射（兼容 akshare 不同版本）
    col_map = _detect_columns(df)

    # 1. 名称含 ST / *ST
    mask_st = ~df[col_map["name"]].str.contains(r"\*?ST", na=False, regex=False)

    # 2. 成交量为 0（停牌）
    mask_vol = df[col_map["volume"]] > 0

    # 3. 涨幅 ≥ 9.5% 或 ≤ 1%
    mask_pct = (df[col_map["pct_chg"]] < 9.5) & (df[col_map["pct_chg"]] > 1.0)

    # 4. 流通市值 20亿 ~ 200亿
    mask_mcap = (df[col_map["mcap"]] >= 20_0000_0000) & (df[col_map["mcap"]] <= 200_0000_0000)

    result = df[mask_st & mask_vol & mask_pct & mask_mcap].copy()
    return result


def _detect_columns(df: pd.DataFrame) -> dict:
    """自动检测列名"""
    mapping = {
        "name": "名称",
        "code": "代码",
        "volume": "成交量",
        "pct_chg": "涨跌幅",
        "mcap": "流通市值",
        "turnover": "换手率",
        "amount": "成交额",
        "close": "最新价",
    }
    # 回退检测
    for key, default in mapping.items():
        if default not in df.columns:
            # 尝试模糊匹配
            for col in df.columns:
                if key == "name" and "名称" in col:
                    mapping[key] = col
                    break
                elif key == "code" and "代码" in col:
                    mapping[key] = col
                    break
                elif key == "volume" and "成交量" in col:
                    mapping[key] = col
                    break
                elif key == "pct_chg" and ("涨跌幅" in col or "涨幅" in col):
                    mapping[key] = col
                    break
                elif key == "mcap" and "流通市值" in col:
                    mapping[key] = col
                    break
                elif key == "turnover" and "换手率" in col:
                    mapping[key] = col
                    break
                elif key == "amount" and "成交额" in col:
                    mapping[key] = col
                    break
                elif key == "close" and "最新价" in col:
                    mapping[key] = col
                    break
    return mapping


def core_filter(
    df: pd.DataFrame,
    pct_min: float,
    pct_max: float,
    turnover_min: float,
    turnover_max: float,
) -> pd.DataFrame:
    """核心筛选"""
    if df.empty:
        return df

    col_map = _detect_columns(df)

    conditions = []
    if col_map["pct_chg"] in df.columns:
        conditions.append((df[col_map["pct_chg"]] >= pct_min) & (df[col_map["pct_chg"]] <= pct_max))
    if col_map["turnover"] in df.columns:
        conditions.append((df[col_map["turnover"]] >= turnover_min) & (df[col_map["turnover"]] <= turnover_max))

    # 量比（如果存在该列）
    vol_ratio_col = None
    for c in df.columns:
        if "量比" in c:
            vol_ratio_col = c
            break
    if vol_ratio_col:
        conditions.append(df[vol_ratio_col] > 1.5)

    # MA20 过滤在后续逐股计算

    if not conditions:
        return df

    mask = conditions[0]
    for cond in conditions[1:]:
        mask = mask & cond

    return df[mask].copy()


def calc_volume_ratio_score(vr: float) -> int:
    """量比评分"""
    if vr >= 2.5:
        return 30
    elif vr >= 2.0:
        return 20
    else:
        return 10


def calc_turnover_score(to: float) -> int:
    """换手率评分"""
    if 5 <= to <= 8:
        return 25
    elif 3 <= to <= 10:
        return 15
    else:
        return 0


def calc_pct_score(pct: float) -> int:
    """涨幅评分"""
    if pct >= 4:
        return 25
    elif pct >= 3:
        return 15
    else:
        return 0


def calc_ma20_score(close: float, ma20: float) -> int:
    """MA20 偏离评分"""
    if ma20 is None or ma20 == 0:
        return 0
    deviation = (close - ma20) / ma20 * 100
    if deviation > 5:
        return 20
    elif deviation > 3:
        return 10
    else:
        return 0


def calc_intraday_rush(symbol: str, close: float) -> dict:
    """
    分时抢筹识别
    返回: {"is_rush": bool, "score_delta": int, "slope_factor": float, "label": str}
    """
    now = beijing_now().time()
    if not (TAIL_SESSION_START <= now <= time(15, 0)):
        return {"is_rush": False, "score_delta": 0, "slope_factor": 0.0, "label": "非尾盘"}

    df_min = fetch_intraday_minute(symbol)
    if df_min is None or len(df_min) < 10:
        return {"is_rush": False, "score_delta": 0, "slope_factor": 0.0, "label": "数据不足"}

    # 取尾盘 30 分钟数据（14:30 之后）
    time_col = None
    price_col = None
    for c in df_min.columns:
        if "时间" in c:
            time_col = c
        if "收盘" in c or "close" in c.lower():
            price_col = c

    if time_col is None or price_col is None:
        return {"is_rush": False, "score_delta": 0, "slope_factor": 0.0, "label": "列缺失"}

    df_min[time_col] = pd.to_datetime(df_min[time_col])
    tail_mask = df_min[time_col].dt.time >= TAIL_SESSION_START
    tail_data = df_min[tail_mask].copy()

    if len(tail_data) < 5:
        return {"is_rush": False, "score_delta": 0, "slope_factor": 0.0, "label": "数据不足"}

    prices = tail_data[price_col].values.astype(float)

    # 计算尾盘整体斜率
    n = len(prices)
    x = np.arange(n)
    slope = np.polyfit(x, prices, 1)[0]  # 每分钟价格变化
    slope_normalized = slope / prices[0] * 100  # 归一化为百分比

    # 最后 5 分钟涨幅占尾盘总涨幅比例
    total_change = prices[-1] - prices[0]
    if len(prices) >= 5:
        last5_change = prices[-1] - prices[-5]
    else:
        last5_change = total_change

    if total_change <= 0:
        return {"is_rush": False, "score_delta": 0, "slope_factor": 0.0, "label": "尾盘无涨幅"}

    last5_ratio = last5_change / total_change if total_change != 0 else 0

    # 斜率因子（用于量化测压）
    slope_factor = slope_normalized

    # 判断抢筹 vs 诱多
    # 斜率平缓 (slope_normalized < 0.05 可视为 45 度推升) 且 最后5分钟占比 < 60%
    if slope_normalized < 0.08 and last5_ratio < 0.6:
        return {"is_rush": True, "score_delta": 10, "slope_factor": slope_factor, "label": "真抢筹"}
    # 斜率陡峭 (slope_normalized >= 0.12 可视为 80 度拉升) 且 最后5分钟占比 >= 60%
    elif slope_normalized >= 0.12 and last5_ratio >= 0.6:
        return {"is_rush": False, "score_delta": -10, "slope_factor": slope_factor, "label": "诱多嫌疑"}
    else:
        return {"is_rush": False, "score_delta": 0, "slope_factor": slope_factor, "label": "正常"}


def calc_pressure_test(close: float, slope_factor: float) -> dict:
    """
    量化测压模块
    返回: {"premium": float, "pl_ratio": float, "a50_pct": float}
    """
    # 获取 A50 夜盘涨跌幅
    a50_night = _fetch_a50_night_change()

    # 预期开盘溢价 = 尾盘动能因子 × 0.6 + A50 夜盘 × 0.4
    premium = (slope_factor * 0.6) + (a50_night * 0.4)

    # 盈亏比 = (压力位 - 收盘价) / (收盘价 - 支撑位)
    resistance = close * 1.025
    support = close * 0.99  # VWAP 近似用 0.99 × 收盘价
    if close - support == 0:
        pl_ratio = 0.0
    else:
        pl_ratio = (resistance - close) / (close - support)

    return {"premium": round(premium, 2), "pl_ratio": round(pl_ratio, 2), "a50_pct": round(a50_night, 2)}


@st.cache_data(ttl=300)
def _fetch_a50_night_change() -> float:
    """获取富时 A50 指数期货当日涨跌幅"""
    try:
        df = ak.futures_zh_minute_sina(symbol="A50")
        if df.empty or len(df) < 2:
            return 0.0
        # 取最新价和昨收（或前一日收盘）计算涨跌幅
        col_close = None
        for c in df.columns:
            if "收盘" in c or "close" in c.lower() or "price" in c.lower():
                col_close = c
                break
        if col_close is None and len(df.columns) > 0:
            col_close = df.columns[-1]  # 兜底用最后一列
        if col_close:
            prices = pd.to_numeric(df[col_close], errors="coerce").dropna()
            if len(prices) >= 2:
                latest = prices.iloc[-1]
                prev = prices.iloc[-2]
                if prev != 0:
                    return (latest - prev) / prev * 100
        return 0.0
    except Exception:
        return 0.0


def run_selection(pct_min: float, pct_max: float, turnover_min: float, turnover_max: float, enable_rush: bool = False) -> pd.DataFrame:
    """执行完整选股流程"""
    # Step 1: 获取实时行情
    with st.spinner("正在获取全 A 股实时行情..."):
        df_raw = fetch_realtime_quotes()

    if df_raw.empty:
        st.error("无法获取行情数据，请检查网络或稍后重试。")
        return pd.DataFrame()

    # Step 2: 基础排雷
    with st.spinner("基础排雷中..."):
        df_filtered = basic_filter(df_raw)

    if df_filtered.empty:
        st.warning("基础排雷后无符合条件股票。")
        return pd.DataFrame()

    # Step 3: 核心筛选
    with st.spinner("核心筛选 + 计算 MA20..."):
        df_core = core_filter(df_filtered, pct_min, pct_max, turnover_min, turnover_max)

    if df_core.empty:
        st.warning("核心筛选后无符合条件股票。")
        return pd.DataFrame()

    col_map = _detect_columns(df_core)

    # Step 4: 逐股评分
    results = []
    total = len(df_core)
    progress_bar = st.progress(0)
    status_text = st.empty()

    vol_ratio_col = None
    for c in df_core.columns:
        if "量比" in c:
            vol_ratio_col = c
            break

    for idx, (_, row) in enumerate(df_core.iterrows()):
        code = str(row[col_map["code"]])
        name = str(row[col_map["name"]])
        close = float(row.get(col_map["close"], 0))
        pct = float(row.get(col_map["pct_chg"], 0))
        turnover = float(row.get(col_map["turnover"], 0))
        vol_ratio = float(row.get(vol_ratio_col, 1.0)) if vol_ratio_col else 1.0

        # 获取 MA20
        hist = fetch_historical_kline(code)
        ma20 = hist["ma20"] if hist else None

        # 评分
        score_vr = calc_volume_ratio_score(vol_ratio)
        score_to = calc_turnover_score(turnover)
        score_pct = calc_pct_score(pct)
        score_ma = calc_ma20_score(close, ma20)
        total_score = score_vr + score_to + score_pct + score_ma

        # 分时抢筹（仅在开关开启时执行）
        if enable_rush:
            rush = calc_intraday_rush(code, close)
            total_score += rush["score_delta"]
        else:
            rush = {"label": "未开启", "score_delta": 0, "slope_factor": 0.0}

        # 量化测压
        pressure = calc_pressure_test(close, rush["slope_factor"])

        # 东财链接
        link = f"https://quote.eastmoney.com/concept/{code}.html"

        results.append(
            {
                "股票名称": name,
                "代码": code,
                "收盘价": close,
                "涨幅(%)": round(pct, 2),
                "换手率(%)": round(turnover, 2),
                "量比": round(vol_ratio, 2),
                "综合评分": max(0, min(100, total_score)),  # clamp 0-100
                "抢筹标记": rush["label"],
                "预期开盘溢价(%)": pressure["premium"],
                "盈亏比": pressure["pl_ratio"],
                "链接": link,
            }
        )

        # 更新进度
        progress = (idx + 1) / total
        progress_bar.progress(progress)
        status_text.text(f"分析进度: {idx + 1}/{total}")

    progress_bar.empty()
    status_text.empty()

    if not results:
        return pd.DataFrame()

    df_result = pd.DataFrame(results)
    df_result = df_result.sort_values("综合评分", ascending=False).reset_index(drop=True)
    return df_result


# ============================================================
# 结果缓存（st.session_state）
# ============================================================
def save_results(df: pd.DataFrame):
    """保存选股结果到 session_state"""
    st.session_state["cached_picks"] = {
        "timestamp": beijing_now().isoformat(),
        "data": df.to_dict(orient="records"),
    }


def load_results() -> pd.DataFrame | None:
    """从 session_state 加载上次保存的结果"""
    cached = st.session_state.get("cached_picks")
    if cached is None:
        return None
    try:
        return pd.DataFrame(cached["data"])
    except Exception:
        return None


# ============================================================
# 主页面
# ============================================================
def main():
    inject_css()

    st.title("📈 尾盘智能选股")
    st.caption("基于实时行情的尾盘量化选股工具")

    # ---- 侧边栏 ----
    with st.sidebar:
        st.header("⚙️ 筛选条件")
        pct_min, pct_max = st.slider(
            "涨幅范围 (%)",
            min_value=0.0,
            max_value=20.0,
            value=(2.0, 5.0),
            step=0.5,
        )
        turnover_min, turnover_max = st.slider(
            "换手率范围 (%)",
            min_value=0.0,
            max_value=30.0,
            value=(3.0, 10.0),
            step=0.5,
        )

        st.divider()
        enable_rush_detection = st.checkbox("开启抢筹识别（耗时较长）", value=False)
        st.caption(f"数据缓存: 尾盘10分钟 / 非尾盘1小时")
        st.caption(f"尾盘时段: {TAIL_SESSION_START.strftime('%H:%M')} - 15:00")

        if st.button("🔄 手动刷新数据", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    # ---- 时间判断 ----
    now = beijing_now()
    current_time = now.time()
    is_tail_session = current_time >= DISPLAY_START and current_time <= time(15, 0)

    # ---- 非尾盘提示 ----
    if not is_tail_session:
        st.info(
            f"⏰ 当前时间 {current_time.strftime('%H:%M')}，非尾盘时段（尾盘展示时间 ≥ {DISPLAY_START.strftime('%H:%M')}），"
            "以下为最近一次保存的选股结果，仅供参考。"
        )
        cached = load_results()
        if cached is not None and not cached.empty:
            df_result = cached
        else:
            st.warning("暂无历史选股结果。请等待尾盘时段自动生成。")
            _render_footer()
            return
    else:
        # 尾盘时段：自动运行选股
        df_result = run_selection(pct_min, pct_max, turnover_min, turnover_max, enable_rush_detection)
        if not df_result.empty:
            save_results(df_result)

    # ---- 展示结果 ----
    if df_result.empty:
        st.warning("当前没有符合条件的股票。")
        _render_footer()
        return

    st.subheader(f"📋 选股结果（共 {len(df_result)} 只）")

    # 构建可点击表格
    _render_clickable_table(df_result)

    # ---- 风险提示 ----
    _render_footer()


def _render_clickable_table(df: pd.DataFrame):
    """渲染可点击的表格，评分 ≥ 70 高亮"""
    display_cols = ["股票名称", "代码", "收盘价", "涨幅(%)", "换手率(%)", "量比", "综合评分", "抢筹标记", "预期开盘溢价(%)", "盈亏比"]

    # 构建 HTML 表格
    html = '<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:0.9rem;">'
    html += "<thead><tr style='background:#f5f5f5;'>"
    for col in display_cols:
        html += f"<th style='padding:10px;border-bottom:2px solid #ddd;text-align:center;'>{col}</th>"
    html += "</tr></thead><tbody>"

    for _, row in df.iterrows():
        score = row.get("综合评分", 0)
        highlight = "highlight-row" if score >= 70 else ""
        html += f"<tr class='{highlight}'>"
        for col in display_cols:
            val = row.get(col, "-")
            if col == "代码":
                code = str(row.get("代码", ""))
                link = row.get("链接", f"https://quote.eastmoney.com/concept/{code}.html")
                html += f"<td style='padding:8px;border-bottom:1px solid #eee;text-align:center;'><a href='{link}' target='_blank' style='color:#1890ff;text-decoration:none;'>{code}</a></td>"
            elif col == "综合评分":
                color = "#52c41a" if score >= 70 else ("#faad14" if score >= 50 else "#999")
                html += f"<td style='padding:8px;border-bottom:1px solid #eee;text-align:center;font-weight:bold;color:{color};'>{score}</td>"
            elif col == "抢筹标记":
                label = str(val)
                label_color = "#52c41a" if label == "真抢筹" else ("#ff4d4f" if label == "诱多嫌疑" else "#999")
                html += f"<td style='padding:8px;border-bottom:1px solid #eee;text-align:center;color:{label_color};font-weight:bold;'>{label}</td>"
            elif col in ("涨幅(%)",):
                val_num = float(val) if val != "-" else 0
                color = "#cf1322" if val_num > 0 else ("#3f8600" if val_num < 0 else "#999")
                html += f"<td style='padding:8px;border-bottom:1px solid #eee;text-align:center;color:{color};'>{val}</td>"
            else:
                html += f"<td style='padding:8px;border-bottom:1px solid #eee;text-align:center;'>{val}</td>"
        html += "</tr>"

    html += "</tbody></table></div>"
    st.markdown(html, unsafe_allow_html=True)


def _render_footer():
    st.markdown(
        '<div class="risk-warning">⚠️ 以上内容仅供参考，不构成投资建议。股市有风险，投资需谨慎。</div>',
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
