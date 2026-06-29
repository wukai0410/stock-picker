# app.py - 尾盘智能选股工具（完整版：综合评分 + 详情页 + 主力分析 + 资金流向 + 企业微信推送）
# -*- coding: utf-8 -*-
import streamlit as st
import akshare as ak
import pandas as pd
import numpy as np
import requests
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
    now = beijing_now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tail_start = today.replace(hour=14, minute=30, second=0)
    tail_end = today.replace(hour=15, minute=0, second=0)
    return tail_start <= now <= tail_end

def is_trading_day() -> bool:
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
        "wecom": {
            "corpid": "wwab9a5075f240347d",
            "agentid": "1000002",
            "secret": "jnwWrisTzy-ni2iFUOpdciihlGfs4DHQyhOqpj1AM_o",
            "touser": "@all",
        }
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
# 数据获取（缓存 + 重试）
# ============================================================
@st.cache_data(ttl=600, show_spinner=False)
def fetch_realtime_quotes():
    import time
    last_err = None
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_spot_em()
            if df is None or df.empty:
                time.sleep(1)
                continue
            column_mapping = {
                "代码": ["代码", "code", "股票代码"],
                "名称": ["名称", "name", "股票名称"],
                "涨跌幅": ["涨跌幅", "涨跌幅%", "change_pct", "涨幅"],
                "量比": ["量比", "volume_ratio", "量比(当日)"],
                "成交额": ["成交额", "amount", "成交金额"],
                "换手率": ["换手率", "turn_over", "换手率%"],
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
                for col in df.columns:
                    for req in required_cols:
                        if req in col or col in req:
                            df = df.rename(columns={col: req})
                            break
                missing = [c for c in required_cols if c not in df.columns]
                if missing:
                    return None
            numeric_cols = ["涨跌幅", "量比", "成交额", "换手率", "最新价"]
            for col in numeric_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            return df
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    return None

@st.cache_data(ttl=60)
def fetch_intraday_minute(symbol: str):
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

@st.cache_data(ttl=600)
def fetch_ma20(symbol: str) -> float | None:
    try:
        df = ak.stock_zh_a_hist(symbol=symbol, period="daily", adjust="qfq", start_date="20240101")
        if df is None or df.empty:
            return None
        close_col = _get_column(df, ["收盘", "close"])
        if close_col is None:
            return None
        closes = pd.to_numeric(df[close_col], errors="coerce").dropna()
        if len(closes) < 20:
            return None
        return closes.tail(20).mean()
    except Exception:
        return None

# ============================================================
# 资金流向数据获取
# ============================================================
@st.cache_data(ttl=3600)
def fetch_fund_flow(symbol: str) -> dict:
    """
    获取近10日机构和大户资金流入流出情况
    返回：机构净流入、大户净流入、近10日每日数据
    """
    result = {
        "institution": {"流入": 0, "流出": 0, "净额": 0},
        "big_trader": {"流入": 0, "流出": 0, "净额": 0},
        "available": False,
        "error": None,
        "source": None
    }
    
    # 方法1：龙虎榜数据（机构席位）
    try:
        df = ak.stock_lhb_em(symbol=symbol)
        if df is not None and not df.empty:
            df = df.head(10)
            # 检查是否有机构席位
            has_inst = False
            if "买方席位名称" in df.columns:
                inst_mask = df["买方席位名称"].str.contains("机构", na=False) | df["卖方席位名称"].str.contains("机构", na=False)
                inst_data = df[inst_mask]
                if not inst_data.empty:
                    has_inst = True
                    inst_in = inst_data[inst_data["买卖方向"] == "买入"]["成交额"].sum() / 1e8
                    inst_out = inst_data[inst_data["买卖方向"] == "卖出"]["成交额"].sum() / 1e8
                    result["institution"]["流入"] = round(inst_in, 2)
                    result["institution"]["流出"] = round(inst_out, 2)
                    result["institution"]["净额"] = round(inst_in - inst_out, 2)
                    result["source"] = "龙虎榜-机构席位"
            
            # 大户统计（非机构的大额交易）
            if "买方席位名称" in df.columns:
                non_inst_mask = ~df["买方席位名称"].str.contains("机构", na=False) & ~df["卖方席位名称"].str.contains("机构", na=False)
                bt_data = df[non_inst_mask]
                if not bt_data.empty:
                    bt_in = bt_data[bt_data["买卖方向"] == "买入"]["成交额"].sum() / 1e8
                    bt_out = bt_data[bt_data["买卖方向"] == "卖出"]["成交额"].sum() / 1e8
                    result["big_trader"]["流入"] = round(bt_in, 2)
                    result["big_trader"]["流出"] = round(bt_out, 2)
                    result["big_trader"]["净额"] = round(bt_in - bt_out, 2)
                    if result["source"] is None:
                        result["source"] = "龙虎榜-大户席位"
            
            if has_inst:
                result["available"] = True
                return result
    except Exception as e:
        result["error"] = str(e)
    
    # 方法2：大单资金流向（逐笔数据）
    try:
        df = ak.stock_large_order_em(symbol=symbol)
        if df is not None and not df.empty:
            df = df.head(10)
            # 超大单（>100万）视为机构
            super_df = df[df["订单类型"].str.contains("超大单", na=False)]
            if not super_df.empty:
                super_net = super_df["净流入"].sum() / 1e8
                result["institution"]["净额"] = round(super_net, 2)
                result["institution"]["流入"] = round(super_df[super_df["净流入"] > 0]["净流入"].sum() / 1e8, 2) if not super_df[super_df["净流入"] > 0].empty else 0
                result["institution"]["流出"] = round(abs(super_df[super_df["净流入"] < 0]["净流入"].sum() / 1e8), 2) if not super_df[super_df["净流入"] < 0].empty else 0
            
            # 大单（50-100万）视为大户
            large_df = df[df["订单类型"].str.contains("大单", na=False)]
            if not large_df.empty:
                large_net = large_df["净流入"].sum() / 1e8
                result["big_trader"]["净额"] = round(large_net, 2)
                result["big_trader"]["流入"] = round(large_df[large_df["净流入"] > 0]["净流入"].sum() / 1e8, 2) if not large_df[large_df["净流入"] > 0].empty else 0
                result["big_trader"]["流出"] = round(abs(large_df[large_df["净流入"] < 0]["净流入"].sum() / 1e8), 2) if not large_df[large_df["净流入"] < 0].empty else 0
            
            if not super_df.empty or not large_df.empty:
                result["available"] = True
                result["source"] = "逐笔大单数据"
                return result
    except Exception as e:
        result["error"] = str(e)
    
    return result


def get_fund_flow_summary(symbol: str) -> dict:
    """获取资金流向摘要，用于详情页展示"""
    data = fetch_fund_flow(symbol)
    
    if not data["available"]:
        return {
            "has_data": False,
            "message": "暂无近10日资金流向数据（该股可能未上榜龙虎榜或数据源限制）"
        }
    
    inst = data["institution"]
    big = data["big_trader"]
    
    inst_status = "净流入" if inst["净额"] > 0 else ("净流出" if inst["净额"] < 0 else "持平")
    big_status = "净流入" if big["净额"] > 0 else ("净流出" if big["净额"] < 0 else "持平")
    inst_color = "🟢" if inst["净额"] > 0 else ("🔴" if inst["净额"] < 0 else "⚪")
    big_color = "🟢" if big["净额"] > 0 else ("🔴" if big["净额"] < 0 else "⚪")
    
    return {
        "has_data": True,
        "source": data.get("source", "龙虎榜"),
        "institution": {
            "流入": inst["流入"],
            "流出": inst["流出"],
            "净额": inst["净额"],
            "status": inst_status,
            "color": inst_color
        },
        "big_trader": {
            "流入": big["流入"],
            "流出": big["流出"],
            "净额": big["净额"],
            "status": big_status,
            "color": big_color
        },
        "summary": f"机构{inst_status} {inst_color}，大户{big_status} {big_color}"
    }

# ============================================================
# 综合评分系统（满分100分）
# ============================================================
def calc_composite_score(vol_ratio: float, turnover: float, pct: float, close: float, ma20: float | None) -> int:
    score = 0
    if vol_ratio >= 2.5:
        score += 30
    elif vol_ratio >= 2.0:
        score += 20
    else:
        score += 10
    if 5 <= turnover <= 8:
        score += 25
    elif 3 <= turnover <= 10:
        score += 15
    if pct >= 4:
        score += 25
    elif pct >= 3:
        score += 15
    if ma20 is not None and ma20 > 0:
        deviation = (close - ma20) / ma20 * 100
        if deviation > 5:
            score += 20
        elif deviation > 3:
            score += 10
    return min(score, 100)

# ============================================================
# 主力阶段分析
# ============================================================
def analyze_main_force_stage(vol_ratio: float, pct: float, turnover: float, close: float, ma20: float | None) -> dict:
    is_high_volume = vol_ratio >= 2.0
    is_high_turnover = turnover >= 5
    is_high_pct = pct >= 3
    is_above_ma20 = ma20 is not None and close > ma20
    deviation = ((close - ma20) / ma20 * 100) if ma20 and ma20 > 0 else 0

    if is_high_volume and is_high_pct and is_above_ma20 and deviation > 5:
        stage = "🚀 主升浪拉升"
        detail = "放量上涨，股价远高于均线，主力拉升阶段"
        confidence = "高"
    elif is_high_volume and not is_high_pct and is_above_ma20:
        stage = "📦 震荡洗盘"
        detail = "放量但涨幅不大，主力在清洗浮筹"
        confidence = "中"
    elif is_high_volume and pct < 0:
        stage = "📉 主力出货"
        detail = "放量下跌，主力可能在高位派发筹码"
        confidence = "高"
    elif not is_high_volume and is_high_pct and deviation < 3:
        stage = "📈 主力建仓"
        detail = "温和放量上涨，主力在悄悄收集筹码"
        confidence = "中"
    elif vol_ratio < 1.5 and turnover < 3:
        stage = "⏸️ 横盘整理"
        detail = "缩量横盘，方向不明，等待突破"
        confidence = "低"
    else:
        stage = "🔀 方向不明"
        detail = "量价关系不明确，建议观望"
        confidence = "低"

    return {
        "stage": stage,
        "detail": detail,
        "confidence": confidence,
        "deviation": round(deviation, 2)
    }

# ============================================================
# 走势预判
# ============================================================
def predict_trend(score: int, stage_info: dict, pct: float, fund_summary: dict = None) -> dict:
    confidence = stage_info.get("confidence", "低")
    
    # 如果有资金流向数据，结合判断
    fund_boost = 0
    if fund_summary and fund_summary.get("has_data"):
        inst_net = fund_summary["institution"]["净额"]
        big_net = fund_summary["big_trader"]["净额"]
        if inst_net > 0 and big_net > 0:
            fund_boost = 10
        elif inst_net > 0 or big_net > 0:
            fund_boost = 5
        elif inst_net < 0 and big_net < 0:
            fund_boost = -10

    adjusted_score = min(100, score + fund_boost)

    if adjusted_score >= 80 and "拉升" in stage_info["stage"]:
        trend = "📈 短期看涨"
        suggestion = "✅ 强烈推荐关注"
        reason = "综合评分高，主力处于拉升阶段"
    elif adjusted_score >= 70 and "建仓" in stage_info["stage"]:
        trend = "📈 中期看涨"
        suggestion = "✅ 建议关注"
        reason = "主力在建仓，评分良好"
    elif adjusted_score >= 60 and "震荡" in stage_info["stage"]:
        trend = "➡️ 震荡偏多"
        suggestion = "⏳ 等待突破"
        reason = "震荡洗盘阶段，等待放量突破"
    elif adjusted_score >= 70 and "出货" in stage_info["stage"]:
        trend = "📉 短期看跌"
        suggestion = "⚠️ 建议回避"
        reason = "主力出货迹象，风险较高"
    elif adjusted_score < 60:
        trend = "📉 短期看跌"
        suggestion = "⚠️ 建议观望"
        reason = "综合评分较低，暂不介入"
    else:
        trend = "➡️ 方向不明"
        suggestion = "⏳ 建议观望"
        reason = "技术指标不明确"

    if fund_boost > 0:
        reason += "；机构/大户资金净流入，加分"
    elif fund_boost < 0:
        reason += "；机构/大户资金净流出，需谨慎"

    return {
        "trend": trend,
        "suggestion": suggestion,
        "reason": reason,
        "confidence": confidence,
        "fund_boost": fund_boost
    }

# ============================================================
# 计算建议购入价和止损价
# ============================================================
def calc_price_levels(close: float, ma20: float | None, pct: float) -> dict:
    if ma20 is not None and ma20 > 0:
        support = round(ma20, 2)
        buy_price = round(ma20 * 1.01, 2)
        stop_loss = round(ma20 * 0.97, 2)
        target = round(close * 1.05, 2)
    else:
        support = round(close * 0.95, 2)
        buy_price = round(close * 0.98, 2)
        stop_loss = round(close * 0.92, 2)
        target = round(close * 1.05, 2)

    return {
        "current": round(close, 2),
        "buy_price": buy_price,
        "stop_loss": stop_loss,
        "target": target,
        "support": support
    }

# ============================================================
# 抢筹分析
# ============================================================
def calc_intraday_rush(df_1min: pd.DataFrame) -> dict:
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

# ============================================================
# 企业微信推送
# ============================================================
def get_wecom_access_token(corpid: str, secret: str) -> str | None:
    try:
        url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={corpid}&corpsecret={secret}"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get("errcode") == 0:
            return data.get("access_token")
        else:
            st.warning(f"⚠️ 获取 access_token 失败: {data.get('errmsg', '未知错误')}")
            return None
    except Exception as e:
        st.warning(f"⚠️ 获取 access_token 异常: {e}")
        return None

def send_wecom_message(df: pd.DataFrame, summary: dict) -> bool:
    if df is None or df.empty:
        return False
    config = get_config()
    wecom = config.get("wecom", {})
    corpid = wecom.get("corpid", "")
    agentid = wecom.get("agentid", "")
    secret = wecom.get("secret", "")
    touser = wecom.get("touser", "@all")

    if not all([corpid, agentid, secret]):
        st.warning("⚠️ 企业微信配置不完整")
        return False

    token = get_wecom_access_token(corpid, secret)
    if token is None:
        return False

    now = beijing_now()
    top_stocks = df.head(5)

    msg_lines = [
        f"📈 **尾盘智能选股结果**",
        f"🕐 时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"📊 共筛选出 **{len(df)}** 只候选股",
        f"📈 平均涨幅：**{summary.get('avg_pct', 0)}%**",
        f"⭐ 最高评分：**{summary.get('max_score', 0)}分**",
        "",
        "🏆 **TOP5 精选**",
    ]

    for i, (_, row) in enumerate(top_stocks.iterrows(), 1):
        code = row.get("代码", "-")
        name = row.get("名称", "-")
        chg = row.get("涨跌幅%", 0)
        vol = row.get("量比", 0)
        score = row.get("综合评分", 0)
        stage = row.get("主力阶段", "-")

        emoji = "🚀" if chg > 5 else ("🔥" if chg > 3 else "📌")

        msg_lines.append(f"{i}. {emoji} **{name}**（{code}）")
        msg_lines.append(f"   涨幅：{chg:+.2f}% ｜ 量比：{vol:.2f} ｜ 评分：{score}分")
        msg_lines.append(f"   {stage}")

    msg_lines.append("")
    msg_lines.append("⚠️ *以上内容仅供参考，不构成投资建议*")

    msg = "\n".join(msg_lines)

    try:
        send_url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
        payload = {
            "touser": touser,
            "msgtype": "markdown",
            "agentid": int(agentid),
            "markdown": {"content": msg}
        }
        resp = requests.post(send_url, json=payload, timeout=10)
        data = resp.json()
        return data.get("errcode") == 0
    except Exception:
        return False

# ============================================================
# 主选股逻辑
# ============================================================
def run_selection(enable_rush: bool = True, max_stocks: int = 30):
    now = beijing_now()

    if not is_trading_day():
        st.warning("⚠️ 今日非交易日，请于交易日运行时再试")
        return None

    tail_time = is_tail_time()
    if not tail_time:
        enable_rush = False
        st.info("ℹ️ 当前非尾盘时段，抢筹分析已自动跳过")

    st.session_state["rush_actual_enabled"] = enable_rush

    status_text = st.empty()
    progress = st.progress(0.0, text="正在初始化...")
    status_text.text("⏳ 准备获取实时行情...")

    df = fetch_realtime_quotes()
    if df is None:
        st.error("❌ 无法获取实时行情")
        return None

    config = get_config()
    total = len(df)
    results = []
    rush_cache = {}
    errors = 0

    for i, (idx, row) in enumerate(df.iterrows()):
        if i % 10 == 0:
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

            if not (config["pct_min"] < chg < config["pct_max"]):
                continue
            if vol_ratio < config["vol_ratio_min"]:
                continue
            if not (config["turnover_min"] < turnover < config["turnover_max"]):
                continue
            if amount < config["amount_min"]:
                continue

            ma20 = fetch_ma20(symbol)
            composite_score = calc_composite_score(vol_ratio, turnover, chg, close, ma20)
            stage_info = analyze_main_force_stage(vol_ratio, chg, turnover, close, ma20)
            price_levels = calc_price_levels(close, ma20, chg)
            
            # 获取资金流向（用于走势预判）
            fund_summary = get_fund_flow_summary(symbol)
            trend_info = predict_trend(composite_score, stage_info, chg, fund_summary)

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
                "MA20": round(ma20, 2) if ma20 else "-",
                "综合评分": composite_score,
                "主力阶段": stage_info["stage"],
                "阶段详情": stage_info["detail"],
                "信心度": stage_info["confidence"],
                "走势预判": trend_info["trend"],
                "操作建议": trend_info["suggestion"],
                "建议理由": trend_info["reason"],
                "建议购入价": price_levels["buy_price"],
                "止损价": price_levels["stop_loss"],
                "目标价": price_levels["target"],
                "支撑位": price_levels["support"],
                "抢筹": rush["label"],
                "抢筹评分": rush["score"],
                "_sort_key": composite_score,
            })
        except Exception:
            errors += 1
            continue

    progress.progress(1.0, text="✅ 选股完成！")
    status_text.text(f"✅ 选股完成！共找到 {len(results)} 只候选股")

    if not results:
        st.warning("⚠️ 未找到符合条件的股票")
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
        "max_score": int(df_result["综合评分"].max()),
        "rush_distribution": df_result["抢筹"].value_counts().to_dict(),
        "errors": errors,
    }
    st.session_state["last_summary"] = summary
    st.session_state["last_results"] = df_result
    st.session_state["last_results_ts"] = beijing_now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        if df_result is not None and not df_result.empty:
            success = send_wecom_message(df_result, summary)
            if success:
                st.toast("✅ 已推送到企业微信", icon="📱")
    except Exception:
        pass

    return df_result

# ============================================================
# 结果存储
# ============================================================
def save_daily_results(df: pd.DataFrame):
    if df is None or df.empty:
        return
    today = today_str()
    if "history_results" not in st.session_state:
        st.session_state["history_results"] = {}
    st.session_state["history_results"][today] = {
        "data": df.to_dict(orient="records"),
        "timestamp": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if len(st.session_state["history_results"]) > 30:
        sorted_dates = sorted(st.session_state["history_results"].keys())
        for old_date in sorted_dates[:-30]:
            del st.session_state["history_results"][old_date]
    st.session_state["last_results"] = df
    st.session_state["last_results_ts"] = beijing_now().strftime("%Y-%m-%d %H:%M:%S")

def load_daily_results(date_str: str) -> pd.DataFrame | None:
    history = st.session_state.get("history_results", {})
    record = history.get(date_str)
    if record is None:
        return None
    return pd.DataFrame(record["data"])

def load_last_results() -> tuple[pd.DataFrame | None, str | None]:
    df = st.session_state.get("last_results")
    ts = st.session_state.get("last_results_ts")
    return df, ts

# ============================================================
# 渲染函数
# ============================================================
def render_summary_panel():
    summary = st.session_state.get("last_summary")
    if summary is None:
        return
    st.subheader("📊 选股统计摘要")
    cols = st.columns(6)
    cols[0].metric("总股票数", summary["total_stocks"])
    cols[1].metric("通过筛选", f"{summary['passed']} 只")
    cols[2].metric("展示数量", f"{summary['displayed']} 只")
    cols[3].metric("平均涨幅", f"{summary['avg_pct']}%")
    cols[4].metric("最高评分", summary["max_score"])
    cols[5].metric("数据异常", summary["errors"])

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
    today = today_str()
    yesterday = (beijing_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    df_today = load_daily_results(today)
    df_yesterday = load_daily_results(yesterday)

    if df_today is None and df_yesterday is None:
        st.caption("📅 暂无历史数据")
        return

    st.subheader("📅 历史对比")
    col1, col2 = st.columns(2)

    with col1:
        st.caption(f"📌 今日 ({today})")
        if df_today is not None and not df_today.empty:
            st.dataframe(df_today[["代码", "名称", "涨跌幅%", "量比", "综合评分", "主力阶段"]],
                        use_container_width=True, hide_index=True)
        else:
            st.text("今日暂无数据")

    with col2:
        st.caption(f"📌 昨日 ({yesterday})")
        if df_yesterday is not None and not df_yesterday.empty:
            st.dataframe(df_yesterday[["代码", "名称", "涨跌幅%", "量比", "综合评分", "主力阶段"]],
                        use_container_width=True, hide_index=True)
        else:
            st.text("昨日暂无数据")

    if df_today is not None and df_yesterday is not None:
        today_codes = set(df_today["代码"].astype(str))
        yesterday_codes = set(df_yesterday["代码"].astype(str))
        overlap = today_codes & yesterday_codes
        if overlap:
            overlap_df = df_today[df_today["代码"].astype(str).isin(overlap)].copy()
            st.success(f"⭐ 连续上榜：{len(overlap)} 只股票")
            st.dataframe(overlap_df[["代码", "名称", "涨跌幅%", "综合评分", "主力阶段"]],
                        use_container_width=True, hide_index=True)
        else:
            st.info("📊 今日与昨日无重叠股票")

# ============================================================
# 股票详情页（含资金流向）
# ============================================================
def render_stock_detail(symbol: str):
    df_result, _ = load_last_results()
    if df_result is None or df_result.empty:
        st.warning("暂无数据，请先运行选股")
        return

    stock_row = df_result[df_result["代码"].astype(str) == str(symbol)]
    if stock_row.empty:
        st.warning(f"未找到股票 {symbol}")
        return

    row = stock_row.iloc[0]

    st.subheader(f"📊 {row['名称']}（{row['代码']}）详细分析")
    
    # 返回按钮
    if st.button("← 返回列表"):
        st.session_state["selected_stock"] = None
        st.rerun()
    
    st.divider()

    # 基本信息
    col1, col2, col3 = st.columns(3)
    col1.metric("最新价", f"{row['最新价']} 元", delta=f"{row['涨跌幅%']:+.2f}%")
    col2.metric("综合评分", f"{row['综合评分']} 分", delta="满分100分")
    col3.metric("主力阶段", row.get("主力阶段", "-"))

    st.divider()

    # 价格参考
    st.subheader("💰 价格参考")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("建议购入价", f"{row.get('建议购入价', '-')} 元")
    col2.metric("止损价", f"{row.get('止损价', '-')} 元", delta="风险控制")
    col3.metric("目标价", f"{row.get('目标价', '-')} 元")
    col4.metric("支撑位", f"{row.get('支撑位', '-')} 元")

    st.divider()

    # 走势预判
    st.subheader("🔮 走势预判")
    trend = row.get("走势预判", "-")
    suggestion = row.get("操作建议", "-")
    reason = row.get("建议理由", "-")

    if "看涨" in trend or "推荐" in suggestion:
        st.success(f"**{trend}** | **{suggestion}**")
    elif "看跌" in trend or "回避" in suggestion or "观望" in suggestion:
        st.warning(f"**{trend}** | **{suggestion}**")
    else:
        st.info(f"**{trend}** | **{suggestion}**")
    st.caption(f"📝 {reason}")

    st.divider()

    # 主力阶段分析
    st.subheader("📈 主力阶段分析")
    st.info(f"**{row.get('主力阶段', '-')}**")
    st.caption(f"📝 {row.get('阶段详情', '-')}")
    st.caption(f"信心度：{row.get('信心度', '-')}")

    st.divider()

    # 技术指标
    st.subheader("📊 技术指标")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("量比", row.get("量比", "-"))
    col2.metric("换手率", f"{row.get('换手率%', '-')}%")
    col3.metric("MA20", row.get("MA20", "-"))
    col4.metric("抢筹评分", row.get("抢筹评分", "-"))

    if "真抢筹" in str(row.get("抢筹", "")):
        st.success(f"⭐ 抢筹状态：{row.get('抢筹', '-')}")
    else:
        st.caption(f"抢筹状态：{row.get('抢筹', '-')}")

    st.divider()

    # ============================================================
    # 新增：近10日机构、大户资金流向
    # ============================================================
    st.subheader("💰 近10日资金流向")

    with st.spinner("正在获取资金流向数据..."):
        fund_data = get_fund_flow_summary(str(symbol))

    if fund_data.get("has_data", False):
        col1, col2 = st.columns(2)

        with col1:
            inst = fund_data["institution"]
            st.markdown(f"""
            <div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px;">
                <h4 style="margin:0 0 8px 0;">🏦 机构资金</h4>
                <table style="width:100%;border-collapse:collapse;">
                    <tr><td style="padding:4px 0;">流入</td><td style="padding:4px 0;text-align:right;font-weight:bold;color:#52c41a;">{inst['流入']:.2f} 亿</td></tr>
                    <tr><td style="padding:4px 0;">流出</td><td style="padding:4px 0;text-align:right;font-weight:bold;color:#ff4d4f;">{inst['流出']:.2f} 亿</td></tr>
                    <tr><td style="padding:4px 0;border-top:1px solid #e0e0e0;">净额</td>
                        <td style="padding:4px 0;text-align:right;font-weight:bold;border-top:1px solid #e0e0e0;color:{'#52c41a' if inst['净额'] >= 0 else '#ff4d4f'};">{inst['color']} {inst['净额']:+.2f} 亿</td></tr>
                    <tr><td style="padding:4px 0;">状态</td>
                        <td style="padding:4px 0;text-align:right;font-weight:bold;color:{'#52c41a' if inst['净额'] >= 0 else '#ff4d4f'};">{inst['status']}</td></tr>
                </table>
            </div>
            """, unsafe_allow_html=True)

        with col2:
            big = fund_data["big_trader"]
            st.markdown(f"""
            <div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px;">
                <h4 style="margin:0 0 8px 0;">👤 大户资金</h4>
                <table style="width:100%;border-collapse:collapse;">
                    <tr><td style="padding:4px 0;">流入</td><td style="padding:4px 0;text-align:right;font-weight:bold;color:#52c41a;">{big['流入']:.2f} 亿</td></tr>
                    <tr><td style="padding:4px 0;">流出</td><td style="padding:4px 0;text-align:right;font-weight:bold;color:#ff4d4f;">{big['流出']:.2f} 亿</td></tr>
                    <tr><td style="padding:4px 0;border-top:1px solid #e0e0e0;">净额</td>
                        <td style="padding:4px 0;text-align:right;font-weight:bold;border-top:1px solid #e0e0e0;color:{'#52c41a' if big['净额'] >= 0 else '#ff4d4f'};">{big['color']} {big['净额']:+.2f} 亿</td></tr>
                    <tr><td style="padding:4px 0;">状态</td>
                        <td style="padding:4px 0;text-align:right;font-weight:bold;color:{'#52c41a' if big['净额'] >= 0 else '#ff4d4f'};">{big['status']}</td></tr>
                </table>
            </div>
            """, unsafe_allow_html=True)

        st.caption(f"📌 数据来源：{fund_data.get('source', '龙虎榜')} | 近10日统计")
        
        # 综合资金判断
        inst_net = fund_data["institution"]["净额"]
        big_net = fund_data["big_trader"]["净额"]
        
        if inst_net > 0 and big_net > 0:
            st.success("✅ 机构和大户均呈净流入，主力看好该股")
        elif inst_net > 0 and big_net < 0:
            st.warning("⚠️ 机构净流入但大户净流出，存在分歧")
        elif inst_net < 0 and big_net > 0:
            st.warning("⚠️ 大户净流入但机构净流出，需谨慎")
        elif inst_net < 0 and big_net < 0:
            st.error("❌ 机构和大户均呈净流出，主力可能撤离")
        else:
            st.info("ℹ️ 机构和大户资金流向不明确，建议观望")

    else:
        st.info(f"ℹ️ {fund_data.get('message', '暂无数据')}")

    st.divider()
    
    # 返回按钮（底部）
    if st.button("← 返回列表", use_container_width=True):
        st.session_state["selected_stock"] = None
        st.rerun()

# ============================================================
# 主页面
# ============================================================
def main_page():
    st.set_page_config(
        page_title="尾盘智能选股",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.title("📈 尾盘智能选股工具")
    st.caption("基于 akshare 实时数据 + 综合评分系统 + 主力分析")

    with st.sidebar:
        st.header("⚙️ 参数设置")
        now = beijing_now()
        tail_time = is_tail_time()
        trading_day = is_trading_day()

        if tail_time and trading_day:
            st.success("✅ 已进入尾盘时段（14:30-15:00）")
        elif trading_day:
            st.info("ℹ️ 交易时段，尾盘分析将在14:30后可用")
        else:
            st.warning("⚠️ 今日非交易日")

        enable_rush = st.checkbox(
            "🔍 启用尾盘抢筹分析",
            value=tail_time and trading_day,
            disabled=not (tail_time and trading_day),
        )
        if not (tail_time and trading_day):
            st.caption("ℹ️ 抢筹分析已禁用（非尾盘时段）")

        max_stocks = st.number_input("📋 最多显示候选股数", 10, 100, 30, 5)

        st.divider()
        wecom = get_config().get("wecom", {})
        if wecom.get("corpid") and wecom.get("agentid") and wecom.get("secret"):
            st.caption("📱 企业微信推送：已启用 ✅")
        else:
            st.caption("📱 企业微信推送：未配置 ⚠️")

        st.divider()
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
            for key in ["history_results", "last_results", "last_results_ts", "last_summary", "selected_stock"]:
                if key in st.session_state:
                    del st.session_state[key]
            st.success("✅ 历史数据已清空")
            st.rerun()

        st.divider()
        st.caption(f"🕐 当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}")
        st.caption("数据来源：akshare")

    if st.session_state.get("selected_stock") is not None:
        render_stock_detail(st.session_state["selected_stock"])
        return

    render_summary_panel()

    df_result, cached_ts = load_last_results()

    if df_result is not None and not df_result.empty:
        st.subheader(f"📊 候选股票列表（共 {len(df_result)} 只）")
        if cached_ts:
            st.caption(f"⏱️ 缓存时间戳：{cached_ts}")

        display_cols = ["代码", "名称", "最新价", "涨跌幅%", "量比", "换手率%", "综合评分", "主力阶段", "操作建议"]

        st.dataframe(
            df_result[display_cols],
            column_config={
                "代码": st.column_config.TextColumn("代码", width="small"),
                "名称": st.column_config.TextColumn("名称", width="medium"),
                "最新价": st.column_config.NumberColumn("最新价", format="%.2f"),
                "涨跌幅%": st.column_config.NumberColumn("涨跌幅%", format="%.2f%%"),
                "量比": st.column_config.NumberColumn("量比", format="%.2f"),
                "换手率%": st.column_config.NumberColumn("换手率%", format="%.2f%%"),
                "综合评分": st.column_config.NumberColumn("综合评分", format="%d"),
                "主力阶段": st.column_config.TextColumn("主力阶段", width="medium"),
                "操作建议": st.column_config.TextColumn("操作建议", width="medium"),
            },
            use_container_width=True,
            hide_index=True,
        )

        st.subheader("⭐ 高分推荐（评分 ≥ 70）")
        high_score_df = df_result[df_result["综合评分"] >= 70]
        if not high_score_df.empty:
            st.dataframe(
                high_score_df[display_cols],
                column_config={
                    "代码": st.column_config.TextColumn("代码", width="small"),
                    "名称": st.column_config.TextColumn("名称", width="medium"),
                    "最新价": st.column_config.NumberColumn("最新价", format="%.2f"),
                    "涨跌幅%": st.column_config.NumberColumn("涨跌幅%", format="%.2f%%"),
                    "量比": st.column_config.NumberColumn("量比", format="%.2f"),
                    "换手率%": st.column_config.NumberColumn("换手率%", format="%.2f%%"),
                    "综合评分": st.column_config.NumberColumn("综合评分", format="%d"),
                    "主力阶段": st.column_config.TextColumn("主力阶段", width="medium"),
                    "操作建议": st.column_config.TextColumn("操作建议", width="medium"),
                },
                use_container_width=True,
                hide_index=True,
            )
            st.caption("💡 评分 ≥ 70 分的股票已用绿色背景高亮（在网页显示中）")
        else:
            st.info("ℹ️ 暂无评分 ≥ 70 的股票")

        st.subheader("🔍 点击查看详情")
        selected_code = st.selectbox(
            "选择股票查看详细分析",
            options=df_result["代码"].astype(str).tolist(),
            format_func=lambda x: f"{x} - {df_result[df_result['代码'].astype(str)==x]['名称'].iloc[0]}"
        )
        if st.button("📊 查看详情", use_container_width=True):
            st.session_state["selected_stock"] = selected_code
            st.rerun()

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

# ============================================================
# 程序入口
# ============================================================
if __name__ == "__main__":
    main_page()
