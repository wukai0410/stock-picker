# app.py - 尾盘智能选股工具（AlphaFeed Pro 版 - 完整修复版）
# -*- coding: utf-8 -*-
import streamlit as st
import pandas as pd
import numpy as np
import requests
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
import warnings
warnings.filterwarnings("ignore")

# ============================================================
# AlphaFeed 导入和初始化（Pro 版）
# ============================================================
from alphafeed import AlphaFeed
import os

# 优先环境变量（Streamlit Cloud Secrets），本机兜底硬编码
_af_key = os.environ.get("ALPHAFEED_API_KEY", "sk_8d52237598264e3d9f5d95913fc9a404")
af = AlphaFeed(api_key=_af_key)

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
# 代码格式转换工具
# ============================================================
def to_alphafeed_symbol(symbol: str) -> str:
    """6位数字代码 -> AlphaFeed 格式 (600000 -> 600000.SH)"""
    symbol = str(symbol).zfill(6)
    if symbol.startswith('6'):
        return f"{symbol}.SH"
    elif symbol.startswith('8') or symbol.startswith('4'):
        return f"{symbol}.BJ"
    else:
        return f"{symbol}.SZ"

def from_alphafeed_symbol(af_symbol: str) -> str:
    return af_symbol.split('.')[0]

# ============================================================
# 全局配置
# ============================================================
@st.cache_resource
def get_config():
    return {
        "pct_min": st.session_state.get("cfg_pct_min", 2.0),
        "pct_max": st.session_state.get("cfg_pct_max", 7.0),
        "vol_ratio_min": st.session_state.get("cfg_vol_ratio_min", 1.2),
        "turnover_min": st.session_state.get("cfg_turnover_min", 3.0),
        "turnover_max": st.session_state.get("cfg_turnover_max", 15.0),
        "amount_min": st.session_state.get("cfg_amount_min", 1e8),
        "max_stocks": st.session_state.get("cfg_max_stocks", 30),
        "cache_ttl": 600,
        "wecom": {
            "corpid": os.environ.get("WECOM_CORPID", "wwab9a5075f240347d"),
            "agentid": os.environ.get("WECOM_AGENTID", "1000002"),
            "secret": os.environ.get("WECOM_SECRET", "jnwWrisTzy-ni2iFUOpdciihlGfs4DHQyhOqpj1AM_o"),
            "touser": "@all",
        }
    }

# ============================================================
# 东方财富全市场行情（主力数据源）
# ============================================================
@st.cache_data(ttl=600, show_spinner=False)
def _fetch_eastmoney_market():
    """
    从东方财富 clist 接口获取全市场实时行情。
    返回 DataFrame：代码、名称、涨跌幅、量比、成交额、换手率、最新价
    """
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "*/*",
    })

    all_rows = []
    for page in range(1, 60):
        params = {
            "pn": str(page),
            "pz": "100",
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
            "fields": "f2,f3,f5,f6,f8,f9,f10,f12,f14,f20,f21",
        }
        try:
            resp = session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            diff_list = (data.get("data") or {}).get("diff") or []
            if not diff_list:
                break
            all_rows.extend(diff_list)
            if len(diff_list) < 100:
                break
            time.sleep(0.3)
        except Exception:
            break

    if not all_rows:
        return None

    df = pd.DataFrame(all_rows)

    result = pd.DataFrame()
    result["代码"] = df["f12"].astype(str).str.zfill(6)
    result["名称"] = df["f14"].fillna("")
    result["最新价"] = pd.to_numeric(df["f2"], errors="coerce")
    result["涨跌幅"] = pd.to_numeric(df["f3"], errors="coerce")
    result["量比"] = pd.to_numeric(df["f10"], errors="coerce")
    result["换手率"] = pd.to_numeric(df["f8"], errors="coerce")
    result["成交额"] = pd.to_numeric(df["f20"], errors="coerce")

    result = result.dropna(subset=["代码", "最新价"])
    result = result[result["代码"].str.match(r"^\d{6}$")]
    result.reset_index(drop=True, inplace=True)
    return result


# ============================================================
# AlphaFeed 全市场行情（备用数据源）
# ============================================================
@st.cache_data(ttl=600, show_spinner=False)
def _fetch_alphafeed_market():
    """AlphaFeed 备用：提供股票列表+涨跌幅+价格（无成交额/换手率）"""
    try:
        data = af.quotes.get(universes="CN_Stock")
        if not data:
            return None

        rows = []
        for item in data:
            ext = item.get("ext", {}) or {}
            rows.append({
                "symbol": item.get("symbol", ""),
                "name": ext.get("name", ""),
                "last_price": item.get("last_price"),
                "change_pct": ext.get("change_pct"),
            })
        df = pd.DataFrame(rows)
        if df.empty:
            return None

        result = pd.DataFrame()
        result["代码"] = df["symbol"].astype(str).str.replace(r"\.(SH|SZ|BJ)$", "", regex=True).str.zfill(6)
        result["名称"] = df["name"].fillna("").astype(str)
        result["最新价"] = pd.to_numeric(df["last_price"], errors="coerce")
        result["涨跌幅"] = pd.to_numeric(df["change_pct"], errors="coerce") * 100
        result["量比"] = np.nan
        result["换手率"] = np.nan
        result["成交额"] = np.nan

        result = result.dropna(subset=["代码", "最新价"])
        result = result[result["代码"].str.match(r"^\d{6}$")]
        return result
    except Exception:
        return None


# ============================================================
# 统一行情入口（东方财富优先 → AlphaFeed备用）
# ============================================================
@st.cache_data(ttl=600, show_spinner=False)
def fetch_realtime_quotes():
    """
    全市场实时行情。
    策略：东方财富clist为主（有完整成交额/换手率/量比），
         AlphaFeed为备用（仅价格+涨跌幅）。
    返回统一列名：代码、名称、涨跌幅、量比、成交额、换手率、最新价
    """
    # 策略1：东方财富（完整数据）
    try:
        em_df = _fetch_eastmoney_market()
        if em_df is not None and len(em_df) > 500:
            return em_df
    except Exception:
        pass

    # 策略2：AlphaFeed备用（可能缺少成交额/换手率，初筛时放宽条件）
    try:
        af_df = _fetch_alphafeed_market()
        if af_df is not None and len(af_df) > 100:
            st.warning("⚠️ 东方财富不可用，使用AlphaFeed备用源（部分字段缺失）")
            return af_df
    except Exception:
        pass

    st.error("❌ 所有行情数据源均不可用")
    return None


@st.cache_data(ttl=600)
def fetch_ma20(symbol: str) -> float | None:
    """获取个股20日均线（兼容 list[dict] 和 dict of lists 两种返回格式）"""
    try:
        af_symbol = to_alphafeed_symbol(symbol)
        result = af.klines.get(af_symbol, period="1d", count=300, adjust="forward")
        if not result:
            return None

        closes = None

        if isinstance(result, list):
            close_list = []
            for item in result:
                if isinstance(item, dict) and "close" in item:
                    close_list.append(item["close"])
            if close_list:
                closes = pd.Series(close_list)
        elif isinstance(result, dict) and "close" in result:
            closes = pd.Series(result["close"]).dropna()

        if closes is None or len(closes) < 20:
            return None
        return closes.tail(20).mean()
    except Exception:
        return None


@st.cache_data(ttl=60)
def fetch_intraday_minute(symbol: str):
    """获取当日1分钟分时数据（自动排序）"""
    try:
        af_symbol = to_alphafeed_symbol(symbol)
        result = af.klines.intraday(af_symbol, period="1m")
        if not result or not isinstance(result, dict):
            return None
        if "close" in result and "volume" in result:
            df = pd.DataFrame({
                "close": result["close"],
                "volume": result["volume"],
            })
            if len(df) > 1 and df["close"].iloc[0] > df["close"].iloc[-1]:
                df = df.iloc[::-1].reset_index(drop=True)
            return df
        return None
    except Exception:
        return None


@st.cache_data(ttl=300)
def fetch_a50_change() -> float:
    """获取富时A50涨跌幅（AlphaFeed 暂不支持，用 akshare 兜底）"""
    try:
        import akshare as ak
        df = ak.futures_zh_minute_sina(symbol="A50")
        if df is None or df.empty or len(df) < 2:
            return 0.0
        price_col = None
        for c in df.columns:
            if "收盘价" in c or "close" in c.lower():
                price_col = c
                break
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
# 量化测压：计算预期开盘溢价
# ============================================================
def calc_pressure_test(close: float, slope_factor: float, a50_change: float) -> dict:
    """
    量化测压
    - 预期开盘溢价 = 尾盘动能因子 × 0.6 + A50夜盘 × 0.4
    - 盈亏比 = (压力位 - 当前价) / (当前价 - 支撑位)
    """
    premium = (slope_factor * 0.6) + (a50_change * 0.4)
    resistance = close * 1.025
    support = close * 0.99
    if close - support == 0:
        pl_ratio = 0.0
    else:
        pl_ratio = round((resistance - close) / (close - support), 2)
    return {
        "premium": round(premium, 2),
        "pl_ratio": pl_ratio,
    }


# ============================================================
# 资金流向（东方财富逐日资金流向，近10日汇总）
# ============================================================
@st.cache_data(ttl=600)
def fetch_fund_flow(symbol: str) -> dict:
    """
    获取个股近10日资金流向（机构/大户/散户），使用东方财富逐日数据
    返回: {institution, big_trader, retail, available, source}
    """
    result = {
        "institution": {"流入": 0, "流出": 0, "净额": 0},
        "big_trader": {"流入": 0, "流出": 0, "净额": 0},
        "retail": {"流入": 0, "流出": 0, "净额": 0},
        "available": False,
        "source": None
    }
    try:
        import akshare as ak
        df = ak.stock_individual_fund_flow(stock=symbol)
        if df is None or df.empty:
            return result

        recent = df.tail(10).copy()

        inst_col = "超大单净流入-净额"
        big_col = "大单净流入-净额"
        retail_col = "小单净流入-净额"

        if inst_col not in recent.columns:
            return result

        inst_net = recent[inst_col].sum()
        big_net = recent[big_col].sum() if big_col in recent.columns else 0
        retail_net = recent[retail_col].sum() if retail_col in recent.columns else 0

        result["institution"]["净额"] = round(inst_net / 1e8, 2)
        result["big_trader"]["净额"] = round(big_net / 1e8, 2)
        result["retail"]["净额"] = round(retail_net / 1e8, 2)

        result["institution"]["流入"] = round(max(inst_net, 0) / 1e8, 2)
        result["institution"]["流出"] = round(abs(min(inst_net, 0)) / 1e8, 2)
        result["big_trader"]["流入"] = round(max(big_net, 0) / 1e8, 2)
        result["big_trader"]["流出"] = round(abs(min(big_net, 0)) / 1e8, 2)
        result["retail"]["流入"] = round(max(retail_net, 0) / 1e8, 2)
        result["retail"]["流出"] = round(abs(min(retail_net, 0)) / 1e8, 2)

        result["available"] = True
        result["source"] = "东方财富逐日资金流向"
    except Exception:
        pass
    return result

def get_fund_flow_summary(symbol: str) -> dict:
    data = fetch_fund_flow(symbol)
    if not data["available"]:
        return {"has_data": False, "message": "暂无近10日资金流向数据"}
    inst = data["institution"]
    big = data["big_trader"]
    retail = data["retail"]
    inst_status = "净流入" if inst["净额"] > 0 else ("净流出" if inst["净额"] < 0 else "持平")
    big_status = "净流入" if big["净额"] > 0 else ("净流出" if big["净额"] < 0 else "持平")
    retail_status = "净流入" if retail["净额"] > 0 else ("净流出" if retail["净额"] < 0 else "持平")
    return {
        "has_data": True,
        "source": data.get("source", "东方财富"),
        "institution": {"流入": inst["流入"], "流出": inst["流出"], "净额": inst["净额"], "status": inst_status},
        "big_trader": {"流入": big["流入"], "流出": big["流出"], "净额": big["净额"], "status": big_status},
        "retail": {"流入": retail["流入"], "流出": retail["流出"], "净额": retail["净额"], "status": retail_status},
    }


# ============================================================
# 量比获取（AlphaFeed 计算 + 东方财富备用）
# ============================================================
def _calc_vol_ratio_from_intraday(symbol: str) -> float | None:
    """
    通过 AlphaFeed 分时数据计算量比：
    量比 = 今日累计成交量 / 过去5日同期平均累计成交量
    """
    try:
        af_symbol = to_alphafeed_symbol(symbol)
        today_data = af.klines.get(af_symbol, period="1m", count=300, adjust="none")
        if not today_data or not isinstance(today_data, dict):
            return None
        today_volumes = today_data.get("volume", [])
        if not today_volumes or len(today_volumes) < 30:
            return None
        today_total = sum(today_volumes)

        hist = af.klines.get(af_symbol, period="1d", count=10, adjust="forward")
        if not hist:
            return None
        if isinstance(hist, dict) and "volume" in hist:
            vols = list(hist["volume"])
            if len(vols) < 5:
                return None
            hist_vols = vols[-6:-1] if len(vols) >= 6 else vols[:-1]
            if len(hist_vols) < 3:
                return None
            avg_daily_vol = sum(hist_vols) / len(hist_vols)
        else:
            return None

        minutes_today = len(today_volumes)
        expected_vol = avg_daily_vol * (minutes_today / 240.0)

        if expected_vol <= 0:
            return None
        vol_ratio = today_total / expected_vol
        return round(vol_ratio, 2)
    except Exception:
        return None


def _batch_fetch_vol_ratio_eastmoney(candidates: list[dict]) -> dict[str, float]:
    """
    尝试从东方财富 clist 接口批量获取量比（f10）
    """
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    result = {}
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    for page in range(1, 13):
        params = {
            "pn": str(page),
            "pz": "500",
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f10",
            "_": str(int(datetime.now().timestamp() * 1000)),
        }
        try:
            resp = session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            diff_list = (data.get("data") or {}).get("diff") or []
            if not diff_list:
                break
            for item in diff_list:
                code = str(item.get("f12", ""))
                vr = item.get("f10")
                if code and vr is not None:
                    try:
                        result[code] = float(vr)
                    except (ValueError, TypeError):
                        continue
            if len(diff_list) < 500:
                break
            time.sleep(0.3)
        except Exception:
            break
    return result


def _batch_calc_vol_ratios(candidates: list[dict]) -> dict[str, float]:
    """为候选股批量计算量比"""
    if not candidates:
        return {}

    em_map = _batch_fetch_vol_ratio_eastmoney(candidates)
    em_codes = set(em_map.keys())
    needed_codes = [c for c in candidates if c["symbol"] not in em_codes]
    success_count = len(em_codes & set(c["symbol"] for c in candidates))

    if success_count >= len(candidates) * 0.8:
        return em_map

    if len(needed_codes) > 50:
        return em_map

    result = dict(em_map)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_calc_vol_ratio_from_intraday, c["symbol"]): c["symbol"]
            for c in needed_codes
        }
        for f in as_completed(futures):
            sym = futures[f]
            try:
                vr = f.result()
                if vr is not None and vr > 0:
                    result[sym] = vr
            except Exception:
                pass
    return result


# ============================================================
# 综合评分系统（满分100分）
# ============================================================
def calc_composite_score(vol_ratio: float | None, turnover: float | None, pct: float, close: float, ma20: float | None) -> int:
    score = 0

    if vol_ratio is None:
        score += 10
    elif vol_ratio >= 2.5:
        score += 30
    elif vol_ratio >= 2.0:
        score += 20
    elif vol_ratio >= 1.5:
        score += 10
    else:
        score += 5

    if turnover is not None:
        if 5 <= turnover <= 8:
            score += 25
        elif 3 <= turnover <= 10:
            score += 15
    else:
        score += 10

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


def analyze_main_force_stage(vol_ratio: float | None, pct: float, turnover: float | None, close: float, ma20: float | None) -> dict:
    is_high_volume = (vol_ratio or 0) >= 2.0
    is_active_volume = (vol_ratio or 0) >= 1.5

    is_high_pct = pct >= 3
    is_above_ma20 = True if ma20 is None else (close > ma20)
    deviation = ((close - ma20) / ma20 * 100) if ma20 and ma20 > 0 else 0

    if is_high_volume and is_high_pct and is_above_ma20 and deviation > 5:
        stage = "🚀 主升浪拉升"
        detail = "放量上涨，主力拉升阶段"
        confidence = "高"
    elif is_high_volume and not is_high_pct and is_above_ma20:
        stage = "📦 震荡洗盘"
        detail = "放量但涨幅不大，主力在清洗浮筹"
        confidence = "中"
    elif is_high_volume and pct < 0:
        stage = "📉 主力出货"
        detail = "放量下跌，主力可能在高位派发"
        confidence = "高"
    elif is_active_volume and is_high_pct and is_above_ma20:
        stage = "📈 主力建仓"
        detail = "温和放量上涨，主力在悄悄收集筹码"
        confidence = "中"
    elif not is_active_volume and (turnover or 0) < 3:
        stage = "⏸️ 横盘整理"
        detail = "缩量横盘，等待方向选择"
        confidence = "低"
    else:
        stage = "🔀 方向不明"
        detail = "量价关系不明确，建议观望"
        confidence = "低"

    return {"stage": stage, "detail": detail, "confidence": confidence, "deviation": round(deviation, 2)}


def predict_trend(score: int, stage_info: dict, pct: float, fund_summary: dict = None) -> dict:
    confidence = stage_info.get("confidence", "低")
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

    return {"trend": trend, "suggestion": suggestion, "reason": reason, "confidence": confidence, "fund_boost": fund_boost}


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
    return {"current": round(close, 2), "buy_price": buy_price, "stop_loss": stop_loss, "target": target, "support": support}


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
        return {"label": label, "score": round(score, 1), "detail": f"尾盘量比{last5_ratio:.1%}，斜率{slope_factor:.3f}"}
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
# 并行批量数据获取
# ============================================================
def _batch_fetch_ma20(candidates: list[dict]) -> dict[str, float | None]:
    """并行获取所有候选股的MA20，返回 {symbol: ma20}"""
    result = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        def _fetch_one(sym):
            return sym, fetch_ma20(sym)
        futures = {pool.submit(_fetch_one, c["symbol"]): c["symbol"] for c in candidates}
        for f in as_completed(futures):
            try:
                sym, ma20 = f.result()
                result[sym] = ma20
            except Exception:
                pass
    return result

def _batch_fetch_fund_flow(candidates: list[dict]) -> dict[str, dict]:
    """并行获取所有候选股的资金流向"""
    result = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        def _fetch_one(sym):
            return sym, get_fund_flow_summary(sym)
        futures = {pool.submit(_fetch_one, c["symbol"]): c["symbol"] for c in candidates}
        for f in as_completed(futures):
            try:
                sym, fund = f.result()
                result[sym] = fund
            except Exception:
                pass
    return result

def _batch_fetch_intraday(candidates: list[dict]) -> dict[str, dict]:
    """并行获取所有候选股的分时抢筹分析，同时返回斜率因子用于量化测压"""
    result = {}
    a50_change = fetch_a50_change()
    with ThreadPoolExecutor(max_workers=10) as pool:
        def _fetch_one(sym):
            minute = fetch_intraday_minute(sym)
            rush = calc_intraday_rush(minute)
            # 提取斜率因子（从rush中获取）
            slope_factor = 0.0
            if rush.get("label") != "数据不足" and rush.get("detail"):
                # 从detail中提取斜率值，或使用默认值
                try:
                    detail = rush.get("detail", "")
                    if "斜率" in detail:
                        slope_factor = float(detail.split("斜率")[-1].strip())
                except:
                    pass
            # 计算量化测压
            pressure = calc_pressure_test(
                close=candidates[[c["symbol"] for c in candidates].index(sym)]["close"] if candidates else 0,
                slope_factor=slope_factor,
                a50_change=a50_change
            )
            return sym, {**rush, "pressure": pressure}
        futures = {pool.submit(_fetch_one, c["symbol"]): c["symbol"] for c in candidates}
        for f in as_completed(futures):
            try:
                sym, data = f.result()
                result[sym] = data
            except Exception:
                pass
    return result


# ============================================================
# 主选股逻辑（并行加速版）
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

    # ---- 阶段1：获取全市场行情 ----
    df = fetch_realtime_quotes()
    if df is None:
        st.error("❌ 无法获取实时行情")
        return None

    config = get_config()
    total = len(df)

    # ---- 阶段2：粗筛（涨跌幅+换手率+成交额，暂不卡量比） ----
    status_text.text("🔍 正在进行初筛...")
    progress.progress(0.15, text="正在进行初筛...")

    em_count = df["成交额"].notna().sum()
    use_em_source = em_count > 500

    candidates = []
    for _, row in df.iterrows():
        try:
            symbol = str(row["代码"]).zfill(6)
            name = str(row["名称"])
            chg = float(row["涨跌幅"]) if pd.notna(row["涨跌幅"]) else None
            amount = float(row["成交额"]) if pd.notna(row["成交额"]) else None
            turnover = float(row["换手率"]) if pd.notna(row["换手率"]) else None
            close = float(row["最新价"]) if pd.notna(row.get("最新价")) else 0

            if chg is None or not (config["pct_min"] < chg < config["pct_max"]):
                continue
            if amount is not None and amount < config["amount_min"]:
                continue
            if turnover is not None and not (config["turnover_min"] < turnover < config["turnover_max"]):
                continue

            candidates.append({
                "symbol": symbol, "name": name, "chg": chg,
                "amount": amount, "turnover": turnover, "close": close,
            })
        except Exception:
            continue

    status_text.text(f"📊 初筛通过 {len(candidates)} 只，正在获取量比...")

    if not candidates:
        progress.progress(1.0, text="✅ 选股完成！")
        st.warning("⚠️ 未找到符合条件的股票")
        return None

    # ---- 阶段2.5：批量获取/计算量比 + 二次筛选 ----
    progress.progress(0.25, text="正在获取量比数据...")

    if not use_em_source:
        vol_map = {}
        st.info("ℹ️ 当前使用AlphaFeed备用源，跳过量比获取以节省时间")
    elif not tail_time:
        vol_map = {}
        st.info("ℹ️ 非尾盘时段，跳过量比获取")
    else:
        vol_map = _batch_calc_vol_ratios(candidates)

    vol_success_count = sum(1 for c in candidates if c["symbol"] in vol_map)
    vol_data_valid = vol_success_count >= len(candidates) * 0.2

    if not vol_data_valid:
        for c in candidates:
            c["vol_ratio"] = None
            c["_vol_missing"] = True
        status_text.text(
            f"📊 量比数据不可用（{vol_success_count}/{len(candidates)}只），"
            f"跳过量比筛选，{len(candidates)} 只全部放行"
        )
    else:
        filtered = []
        skipped_low_vol = 0
        auto_passed = 0
        for c in candidates:
            sym = c["symbol"]
            vol_ratio = vol_map.get(sym)
            if vol_ratio is None:
                c["vol_ratio"] = None
                c["_vol_missing"] = True
                auto_passed += 1
                filtered.append(c)
                continue
            if vol_ratio < config["vol_ratio_min"]:
                skipped_low_vol += 1
                continue
            c["vol_ratio"] = vol_ratio
            c["_vol_missing"] = False
            filtered.append(c)

        status_text.text(
            f"📊 量比筛选：{len(filtered)} 只通过"
            f"（无数据{auto_passed}只放行，量比不足{skipped_low_vol}只剔除）"
        )
        candidates = filtered

    if not candidates:
        progress.progress(1.0, text="✅ 选股完成！")
        st.warning("⚠️ 筛选后无候选股")
        return None

    # ---- 阶段3：并行批量获取深度数据 ----
    progress.progress(0.35, text="正在获取MA20...")
    ma20_map = _batch_fetch_ma20(candidates)

    progress.progress(0.55, text="正在获取资金流向...")
    fund_map = _batch_fetch_fund_flow(candidates)

    rush_map = {}
    if enable_rush:
        progress.progress(0.75, text="正在分析分时抢筹...")
        rush_map = _batch_fetch_intraday(candidates)
    else:
        rush_map = {c["symbol"]: {"label": "-", "score": 0, "detail": "-", "pressure": {"premium": 0, "pl_ratio": 0}} for c in candidates}

    # ---- 阶段4：评分排序 ----
    progress.progress(0.9, text="正在评分排序...")
    results = []
    for c in candidates:
        sym = c["symbol"]
        ma20 = ma20_map.get(sym)
        fund_summary = fund_map.get(sym, {"has_data": False})
        rush_data = rush_map.get(sym, {"label": "-", "score": 0, "detail": "-", "pressure": {"premium": 0, "pl_ratio": 0}})
        pressure = rush_data.get("pressure", {"premium": 0, "pl_ratio": 0})

        composite_score = calc_composite_score(c["vol_ratio"], c["turnover"], c["chg"], c["close"], ma20)
        stage_info = analyze_main_force_stage(c["vol_ratio"], c["chg"], c["turnover"], c["close"], ma20)
        price_levels = calc_price_levels(c["close"], ma20, c["chg"])
        trend_info = predict_trend(composite_score, stage_info, c["chg"], fund_summary)

        results.append({
            "代码": sym,
            "名称": c["name"],
            "涨跌幅%": round(c["chg"], 2),
            "量比": round(c["vol_ratio"], 2) if c["vol_ratio"] is not None else "-",
            "换手率%": round(c["turnover"], 2) if c["turnover"] is not None else "-",
            "成交额亿": round(c["amount"] / 1e8, 2) if c["amount"] is not None else "-",
            "最新价": round(c["close"], 2),
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
            "抢筹": rush_data["label"],
            "抢筹评分": rush_data["score"],
            "预期开盘溢价%": pressure["premium"],
            "盈亏比": pressure["pl_ratio"],
            "链接": f"https://quote.eastmoney.com/s/{sym}.html",
            "_sort_key": composite_score,
        })

    progress.progress(1.0, text="✅ 选股完成！")
    status_text.text(f"✅ 选股完成！共找到 {len(results)} 只候选股")

    results.sort(key=lambda x: x["_sort_key"], reverse=True)
    df_result = pd.DataFrame(results[:max_stocks])
    df_result = df_result.drop(columns=["_sort_key"])

    # 安全计算统计值
    _safe_pct = pd.to_numeric(df_result["涨跌幅%"], errors="coerce").dropna()
    _safe_vol = pd.to_numeric(df_result["量比"], errors="coerce").dropna()
    summary = {
        "total_stocks": total,
        "passed": len(results),
        "displayed": min(len(results), max_stocks),
        "avg_pct": round(_safe_pct.mean(), 2) if len(_safe_pct) > 0 else 0,
        "max_vol_ratio": round(_safe_vol.max(), 2) if len(_safe_vol) > 0 else 0,
        "max_score": int(df_result["综合评分"].max()) if len(df_result) > 0 else 0,
        "rush_distribution": df_result["抢筹"].value_counts().to_dict(),
        "errors": 0,
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

    st.subplot("📅 历史对比")
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
# 股票详情页
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

    if st.button("← 返回列表"):
        st.session_state["selected_stock"] = None
        st.rerun()

    st.divider()

    col1, col2, col3 = st.columns(3)
    col1.metric("最新价", f"{row['最新价']} 元", delta=f"{row['涨跌幅%']:+.2f}%")
    col2.metric("综合评分", f"{row['综合评分']} 分", delta="满分100分")
    col3.metric("主力阶段", row.get("主力阶段", "-"))

    st.divider()

    st.subheader("💰 价格参考")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("建议购入价", f"{row.get('建议购入价', '-')} 元")
    col2.metric("止损价", f"{row.get('止损价', '-')} 元", delta="风险控制")
    col3.metric("目标价", f"{row.get('目标价', '-')} 元")
    col4.metric("支撑位", f"{row.get('支撑位', '-')} 元")

    st.divider()

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

    st.subheader("📈 主力阶段分析")
    st.info(f"**{row.get('主力阶段', '-')}**")
    st.caption(f"📝 {row.get('阶段详情', '-')}")
    st.caption(f"信心度：{row.get('信心度', '-')}")

    st.divider()

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

    # 量化测压
    st.subheader("📊 量化测压")
    col1, col2 = st.columns(2)
    with col1:
        premium = row.get("预期开盘溢价%", 0)
        if premium != "-" and premium is not None:
            color = "#52c41a" if premium > 0 else "#ff4d4f"
            st.markdown(f"**预期开盘溢价**：<span style='color:{color};font-size:24px;'>{premium:+.2f}%</span>", unsafe_allow_html=True)
        else:
            st.markdown("**预期开盘溢价**：<span style='color:#999;'>-</span>", unsafe_allow_html=True)
    with col2:
        pl_ratio = row.get("盈亏比", 0)
        if pl_ratio != "-" and pl_ratio is not None:
            color = "#52c41a" if pl_ratio > 2 else "#faad14"
            st.markdown(f"**盈亏比**：<span style='color:{color};font-size:24px;'>{pl_ratio:.2f}</span>", unsafe_allow_html=True)
        else:
            st.markdown("**盈亏比**：<span style='color:#999;'>-</span>", unsafe_allow_html=True)

    st.divider()

    # 资金流向
    st.subheader("💰 近10日资金流向")

    with st.spinner("正在获取资金流向数据..."):
        fund_data = get_fund_flow_summary(str(symbol))

    if fund_data.get("has_data", False):
        col1, col2, col3 = st.columns(3)

        with col1:
            inst = fund_data["institution"]
            st.markdown(f"""
            <div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px;">
                <h4 style="margin:0 0 8px 0;">🏦 机构资金（超大单）</h4>
                <table style="width:100%;border-collapse:collapse;">
                    <tr><td style="padding:4px 0;">流入</td><td style="padding:4px 0;text-align:right;font-weight:bold;color:#52c41a;">{inst['流入']:.2f} 亿</td></tr>
                    <tr><td style="padding:4px 0;">流出</td><td style="padding:4px 0;text-align:right;font-weight:bold;color:#ff4d4f;">{inst['流出']:.2f} 亿</td></tr>
                    <tr><td style="padding:4px 0;border-top:1px solid #e0e0e0;">净额</td>
                        <td style="padding:4px 0;text-align:right;font-weight:bold;border-top:1px solid #e0e0e0;color:{'#52c41a' if inst['净额'] >= 0 else '#ff4d4f'};">{inst['净额']:+.2f} 亿</td></tr>
                    <tr><td style="padding:4px 0;">状态</td>
                        <td style="padding:4px 0;text-align:right;font-weight:bold;color:{'#52c41a' if inst['净额'] >= 0 else '#ff4d4f'};">{inst['status']}</td></tr>
                </table>
            </div>
            """, unsafe_allow_html=True)

        with col2:
            big = fund_data["big_trader"]
            st.markdown(f"""
            <div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px;">
                <h4 style="margin:0 0 8px 0;">👤 大户资金（大单）</h4>
                <table style="width:100%;border-collapse:collapse;">
                    <tr><td style="padding:4px 0;">流入</td><td style="padding:4px 0;text-align:right;font-weight:bold;color:#52c41a;">{big['流入']:.2f} 亿</td></tr>
                    <tr><td style="padding:4px 0;">流出</td><td style="padding:4px 0;text-align:right;font-weight:bold;color:#ff4d4f;">{big['流出']:.2f} 亿</td></tr>
                    <tr><td style="padding:4px 0;border-top:1px solid #e0e0e0;">净额</td>
                        <td style="padding:4px 0;text-align:right;font-weight:bold;border-top:1px solid #e0e0e0;color:{'#52c41a' if big['净额'] >= 0 else '#ff4d4f'};">{big['净额']:+.2f} 亿</td></tr>
                    <tr><td style="padding:4px 0;">状态</td>
                        <td style="padding:4px 0;text-align:right;font-weight:bold;color:{'#52c41a' if big['净额'] >= 0 else '#ff4d4f'};">{big['status']}</td></tr>
                </table>
            </div>
            """, unsafe_allow_html=True)

        with col3:
            retail = fund_data["retail"]
            st.markdown(f"""
            <div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px;">
                <h4 style="margin:0 0 8px 0;">🧑 散户资金（小单）</h4>
                <table style="width:100%;border-collapse:collapse;">
                    <tr><td style="padding:4px 0;">流入</td><td style="padding:4px 0;text-align:right;font-weight:bold;color:#52c41a;">{retail['流入']:.2f} 亿</td></tr>
                    <tr><td style="padding:4px 0;">流出</td><td style="padding:4px 0;text-align:right;font-weight:bold;color:#ff4d4f;">{retail['流出']:.2f} 亿</td></tr>
                    <tr><td style="padding:4px 0;border-top:1px solid #e0e0e0;">净额</td>
                        <td style="padding:4px 0;text-align:right;font-weight:bold;border-top:1px solid #e0e0e0;color:{'#52c41a' if retail['净额'] >= 0 else '#ff4d4f'};">{retail['净额']:+.2f} 亿</td></tr>
                    <tr><td style="padding:4px 0;">状态</td>
                        <td style="padding:4px 0;text-align:right;font-weight:bold;color:{'#52c41a' if retail['净额'] >= 0 else '#ff4d4f'};">{retail['status']}</td></tr>
                </table>
            </div>
            """, unsafe_allow_html=True)

        st.caption(f"📌 数据来源：{fund_data.get('source', '东方财富')} | 近10日统计")

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
    st.caption("基于 AlphaFeed Pro 数据源 + 综合评分系统 + 主力分析")

    # AlphaFeed 健康检查
    try:
        test = af.quotes.get(universes="CN_Stock", limit=1)
        if not test:
            st.error("❌ AlphaFeed 连接失败，请检查 API Key 和网络")
            st.stop()
    except Exception:
        st.error("❌ AlphaFeed 连接异常，请检查 API Key 和网络")
        st.stop()

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

        st.subheader("📊 筛选条件")
        pct_min = st.slider("涨跌幅下限 (%)", 0.0, 10.0, 2.0, 0.5, key="cfg_pct_min")
        pct_max = st.slider("涨跌幅上限 (%)", 0.0, 10.0, 7.0, 0.5, key="cfg_pct_max")
        vol_ratio_min = st.slider("量比下限", 0.5, 5.0, 1.2, 0.1, key="cfg_vol_ratio_min")
        turnover_min = st.slider("换手率下限 (%)", 0.0, 30.0, 3.0, 0.5, key="cfg_turnover_min")
        turnover_max = st.slider("换手率上限 (%)", 0.0, 30.0, 15.0, 0.5, key="cfg_turnover_max")
        amount_min = st.number_input("成交额下限 (亿)", 0.0, 10.0, 1.0, 0.5, key="cfg_amount_min_raw")
        st.session_state["cfg_amount_min"] = amount_min * 1e8

        st.divider()

        enable_rush = st.checkbox(
            "🔍 启用尾盘抢筹分析",
            value=tail_time and trading_day,
            disabled=not (tail_time and trading_day),
        )
        if not (tail_time and trading_day):
            st.caption("ℹ️ 抢筹分析已禁用（非尾盘时段）")

        max_stocks = st.number_input("📋 最多显示候选股数", 10, 100, 30, 5, key="cfg_max_stocks")

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
        st.caption("数据来源：AlphaFeed Pro + 东方财富")

    # ---- 自动运行逻辑 ----
    # 检查是否有缓存结果，如果没有则自动运行
    df_result, cached_ts = load_last_results()
    
    # 检查今日是否有结果
    today = today_str()
    df_today = load_daily_results(today)
    
    # 如果是交易日且无今日结果，自动运行选股
    if is_trading_day() and (df_today is None or df_today.empty):
        with st.spinner("📈 正在自动运行选股逻辑，请稍候..."):
            # 使用侧边栏参数运行
            enable_rush_temp = tail_time and trading_day
            df_result = run_selection(enable_rush_temp, st.session_state.get("cfg_max_stocks", 30))
            if df_result is not None:
                save_daily_results(df_result)
                st.rerun()

    if st.session_state.get("selected_stock") is not None:
        render_stock_detail(st.session_state["selected_stock"])
        return

    render_summary_panel()

    if df_result is not None and not df_result.empty:
        st.subheader(f"📊 候选股票列表（共 {len(df_result)} 只）")
        if cached_ts:
            st.caption(f"⏱️ 缓存时间戳：{cached_ts}")

        display_cols = ["代码", "名称", "最新价", "涨跌幅%", "量比", "换手率%", "综合评分", "主力阶段", "操作建议", "预期开盘溢价%", "盈亏比"]

        # 为代码列添加链接
        df_display = df_result.copy()
        df_display["代码链接"] = df_display["代码"].apply(
            lambda x: f'<a href="https://quote.eastmoney.com/s/{x}.html" target="_blank">{x}</a>'
        )

        st.markdown(
            df_display[["代码链接", "名称", "最新价", "涨跌幅%", "量比", "换手率%", "综合评分", "主力阶段", "操作建议", "预期开盘溢价%", "盈亏比"]].to_html(escape=False, index=False),
            unsafe_allow_html=True
        )

        st.subheader("⭐ 高分推荐（评分 ≥ 70）")
        high_score_df = df_result[df_result["综合评分"] >= 70]
        if not high_score_df.empty:
            high_display = high_score_df.copy()
            high_display["代码链接"] = high_display["代码"].apply(
                lambda x: f'<a href="https://quote.eastmoney.com/s/{x}.html" target="_blank">{x}</a>'
            )
            st.markdown(
                high_display[["代码链接", "名称", "最新价", "涨跌幅%", "量比", "换手率%", "综合评分", "主力阶段", "操作建议"]].to_html(escape=False, index=False),
                unsafe_allow_html=True
            )
            st.caption("💡 评分 ≥ 70 分的股票已用绿色背景高亮")

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
