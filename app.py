# app.py - 实时智能选股工具
# -*- coding: utf-8 -*-
import streamlit as st
import pandas as pd
import numpy as np
import requests
import json
import time
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

def _is_market_open() -> bool:
    """判断当前是否在交易时段（9:30-11:30, 13:00-15:00）"""
    now = beijing_now()
    t = now.hour * 60 + now.minute
    return (570 <= t < 690) or (780 <= t < 900)  # 9:30-11:30, 13:00-15:00

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
# 数据源：三级回退机制（东方财富 → 新浪 → akshare）
# ============================================================
# 当前数据源标识 → 写入 session_state 供 UI 展示
# 值：eastmoney | sina | akshare | none

EASTMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

SINA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn",
    "Accept": "*/*",
}

def _em_fetch(url: str, params: dict, max_retries: int = 3) -> dict | None:
    """带重试的东方财富 API 请求"""
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, headers=EASTMONEY_HEADERS, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if data and data.get("data"):
                    return data
            elif resp.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(1.5 * (attempt + 1))
            continue
    return None

def _sina_fetch_list(max_pages: int = 80) -> list[dict]:
    """从新浪获取全A股股票列表（分页，每页最多80只，80页覆盖约6400只）"""
    all_stocks = []
    for page in range(1, max_pages + 1):
        try:
            url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
            params = {
                "page": page,
                "num": 80,
                "sort": "symbol",
                "asc": 1,
                "node": "hs_a",
                "symbol": "",
                "_s_r_a": "init",
            }
            resp = requests.get(url, params=params, headers=SINA_HEADERS, timeout=15)
            if resp.status_code != 200:
                break
            data = resp.json()
            if not data or not isinstance(data, list):
                break
            all_stocks.extend(data)
            if len(data) < 80:
                break
        except Exception:
            break
    return all_stocks

def _sina_fetch_batch_quotes(codes: list[str]) -> dict[str, dict]:
    """批量从新浪获取实时行情（每次最多400只）"""
    result = {}
    batch_size = 400
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        try:
            # 构建新浪请求：sh600519,sz000001,...
            symbols = []
            for code in batch:
                prefix = "sh" if code.startswith(("6", "9")) else "sz"
                symbols.append(f"{prefix}{code}")
            url = f"https://hq.sinajs.cn/list={','.join(symbols)}"
            resp = requests.get(url, headers=SINA_HEADERS, timeout=20)
            if resp.status_code != 200:
                continue
            # 解析 JSONP: var hq_str_sh600519="xxx";
            text = resp.text
            for line in text.strip().split("\n"):
                if "=" not in line:
                    continue
                # 提取股票代码和值
                # 格式: var hq_str_sh600519="...";
                parts = line.split('="', 1)
                if len(parts) != 2:
                    continue
                key_part = parts[0]  # var hq_str_sh600519
                val_part = parts[1].rstrip('";\n')  # 数据部分
                code = key_part.replace("var hq_str_", "").replace("sh", "").replace("sz", "")
                fields = val_part.split(",")
                if len(fields) < 32:
                    continue
                # 新浪字段映射（0-based）:
                # 0:名称 1:今开 2:昨收 3:最新价 4:最高 5:最低
                # 8:成交量(手) 9:成交额(元) 30:日期 31:时间
                # 注意：新浪API没有量比字段！
                name = fields[0]
                open_price = float(fields[1]) if fields[1] else None
                pre_close = float(fields[2]) if fields[2] else None
                price = float(fields[3]) if fields[3] else None
                high = float(fields[4]) if fields[4] else None
                low = float(fields[5]) if fields[5] else None
                volume = float(fields[8]) if fields[8] else 0
                amount = float(fields[9]) if fields[9] else 0
                # 计算涨跌幅
                if price and pre_close and pre_close > 0:
                    chg_pct = round((price - pre_close) / pre_close * 100, 2)
                else:
                    chg_pct = None
                # 换手率：新浪批量接口不直接提供，从列表接口补充
                result[code] = {
                    "名称": name,
                    "最新价": price,
                    "涨跌幅": chg_pct,
                    "今开": open_price,
                    "昨收": pre_close,
                    "最高": high,
                    "最低": low,
                    "成交额": amount,
                    # 以下字段批量接口不提供，由列表接口补充
                    "量比": None,
                    "换手率": None,
                    "市盈率": None,
                    "总市值": None,
                }
        except Exception:
            continue
    return result

def _fetch_eastmoney() -> pd.DataFrame | None:
    """数据源1：东方财富 HTTP API"""
    try:
        all_rows = []
        page = 1
        while True:
            url = "https://push2.eastmoney.com/api/qt/clist/get"
            params = {
                "pn": page,
                "pz": 5000,
                "po": 1,
                "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f2,f3,f4,f5,f6,f7,f8,f10,f12,f14,f15,f16,f17,f18,f20,f21",
                "_": int(time.time() * 1000),
            }
            data = _em_fetch(url, params)
            if data is None:
                break
            diff_list = data["data"].get("diff", [])
            if not diff_list:
                break
            all_rows.extend(diff_list)
            total = data["data"].get("total", 0)
            if len(all_rows) >= total:
                break
            page += 1

        if not all_rows:
            return None

        records = []
        for item in all_rows:
            records.append({
                "代码": str(item.get("f12", "")),
                "名称": str(item.get("f14", "")),
                "涨跌幅": item.get("f3", None),
                "最新价": item.get("f2", None),
                "量比": item.get("f10", None),
                "成交额": item.get("f6", None),
                "换手率": item.get("f8", None),
                "涨跌额": item.get("f4", None),
                "最高": item.get("f15", None),
                "最低": item.get("f16", None),
                "今开": item.get("f17", None),
                "昨收": item.get("f18", None),
                "市盈率": item.get("f20", None),
                "总市值": item.get("f21", None),
            })
        df = pd.DataFrame(records)
        return df
    except Exception:
        return None

def _fetch_sina() -> pd.DataFrame | None:
    """数据源2：新浪财经（列表+批量行情两步获取）"""
    try:
        # 步骤1：获取股票列表（含换手率、市盈率、总市值）
        stock_list = _sina_fetch_list()
        if not stock_list:
            return None
        # 步骤2：提取代码列表并批量获取行情
        code_list = [s["code"] for s in stock_list if s.get("code")]
        quotes = _sina_fetch_batch_quotes(code_list)
        # 步骤3：合并列表字段 + 行情字段
        records = []
        for s in stock_list:
            code = s.get("code", "")
            if not code or not code.isdigit() or len(code) != 6:
                continue
            q = quotes.get(code, {})
            # 列表接口提供的字段
            name = s.get("name", q.get("名称", ""))
            trade = float(s["trade"]) if s.get("trade") and s["trade"] != "0.000" else q.get("最新价")
            chg_pct = float(s["changepercent"]) if s.get("changepercent") else q.get("涨跌幅")
            open_p = float(s["open"]) if s.get("open") else q.get("今开")
            high = float(s["high"]) if s.get("high") else q.get("最高")
            low = float(s["low"]) if s.get("low") else q.get("最低")
            pre_close = float(s["settlement"]) if s.get("settlement") else q.get("昨收")
            volume = float(s["volume"]) if s.get("volume") else 0
            amount_val = float(s["amount"]) if s.get("amount") else q.get("成交额", 0)
            turnover = float(s["turnoverratio"]) if s.get("turnoverratio") else q.get("换手率")
            pe = float(s["per"]) if s.get("per") else q.get("市盈率")
            mktcap = float(s["mktcap"]) if s.get("mktcap") else q.get("总市值")
            # 量比：新浪API不直接提供，留空
            vol_ratio = None
            # 如果没有从行情接口拿到价格，用列表接口的
            if trade is None:
                trade = float(s["trade"]) if s.get("trade") and s["trade"] != "0.000" else None
            if chg_pct is None and trade is not None and pre_close is not None and pre_close > 0:
                chg_pct = round((trade - pre_close) / pre_close * 100, 2)
            # 涨跌额
            chg_amount = round(trade - pre_close, 2) if (trade is not None and pre_close is not None) else None
            records.append({
                "代码": code,
                "名称": name,
                "涨跌幅": chg_pct,
                "最新价": trade,
                "量比": vol_ratio,
                "成交额": amount_val,
                "换手率": turnover,
                "涨跌额": chg_amount,
                "最高": high,
                "最低": low,
                "今开": open_p,
                "昨收": pre_close,
                "市盈率": pe,
                "总市值": mktcap,
            })
        if not records:
            return None
        return pd.DataFrame(records)
    except Exception:
        return None

def _fetch_akshare() -> pd.DataFrame | None:
    """数据源3：akshare 终极回退"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return None
        # 列名映射
        col_map = {
            "代码": "代码", "名称": "名称", "最新价": "最新价", "涨跌幅": "涨跌幅",
            "涨跌额": "涨跌额", "成交量": "成交量", "成交额": "成交额",
            "今开": "今开", "昨收": "昨收", "最高": "最高", "最低": "最低",
            "换手率": "换手率", "量比": "量比", "市盈率-动态": "市盈率",
            "总市值": "总市值",
        }
        existing = {}
        for src, dst in col_map.items():
            if src in df.columns:
                existing[dst] = df[src]
        if "代码" not in existing:
            return None
        result = pd.DataFrame(existing)
        return result
    except Exception:
        return None

@st.cache_data(ttl=600)
def fetch_realtime_quotes():
    """三级回退获取全A股实时行情：东方财富 → 新浪 → akshare"""
    source_name = "none"
    df = None

    # ---- 数据源1：东方财富 ----
    try:
        df = _fetch_eastmoney()
        if df is not None and len(df) >= 100:
            source_name = "eastmoney"
    except Exception:
        pass

    # ---- 数据源2：新浪财经 ----
    if df is None or len(df) < 100:
        try:
            df_sina = _fetch_sina()
            if df_sina is not None and len(df_sina) >= 100:
                df = df_sina
                source_name = "sina"
        except Exception:
            pass

    # ---- 数据源3：akshare ----
    if df is None or len(df) < 100:
        try:
            df_ak = _fetch_akshare()
            if df_ak is not None and len(df_ak) >= 100:
                df = df_ak
                source_name = "akshare"
        except Exception:
            pass

    if df is None or df.empty:
        st.session_state["data_source"] = "none"
        return None

    # 数值类型转换
    numeric_cols = ["涨跌幅", "最新价", "量比", "成交额", "换手率", "涨跌额", "最高", "最低", "今开", "昨收", "市盈率", "总市值"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 过滤无效数据
    df = df.dropna(subset=["代码", "名称", "涨跌幅", "最新价"])
    df = df[df["代码"].str.match(r"^[0-9]{6}$")]
    df = df.reset_index(drop=True)

    # 数据质量诊断 → 写入 session_state 供 UI 展示
    total_raw = len(df)
    st.session_state["data_source"] = source_name
    if total_raw > 0:
        st.session_state["data_quality"] = {
            "total": total_raw,
            "valid_pct": int(df["涨跌幅"].notna().sum()),
            "valid_vol": int((df["量比"].notna() & (df["量比"] > 0)).sum()),
            "valid_amount": int((df["成交额"].notna() & (df["成交额"] > 0)).sum()),
            "valid_turnover": int((df["换手率"].notna() & (df["换手率"] > 0)).sum()),
        }
    return df

@st.cache_data(ttl=60)
def fetch_intraday_minute(symbol: str):
    """通过东方财富 HTTP API 获取当日1分钟分时数据"""
    try:
        # 判断市场前缀：6开头=上证(1)，0/3开头=深证(0)
        market = 1 if symbol.startswith("6") else 0
        secid = f"{market}.{symbol}"

        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "secid": secid,
            "ut": "fa5fd1943c7b386f172d6893dbfba10b",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
            "klt": "1",        # 1分钟K线
            "fqt": "1",        # 前复权
            "end": "20500101",
            "lmt": "240",      # 最多240根（覆盖全天交易）
            "_": int(time.time() * 1000),
        }

        data = _em_fetch(url, params)
        if data is None:
            return None

        klines = data["data"].get("klines", [])
        if not klines:
            return None

        records = []
        for line in klines:
            parts = line.split(",")
            if len(parts) >= 7:
                records.append({
                    "time": parts[0],
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "volume": float(parts[5]),
                    "amount": float(parts[6]),
                })

        if not records:
            return None

        df = pd.DataFrame(records)
        return df.dropna(subset=["volume", "close"])

    except Exception:
        return None

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
    # 抢筹分析状态写入 session_state
    st.session_state["rush_actual_enabled"] = enable_rush
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
    # 筛选漏斗诊断
    diag = {
        "pct_pass": 0, "vol_pass": 0, "turnover_pass": 0, "amount_pass": 0,
        "pct_fail": 0, "vol_fail": 0, "turnover_fail": 0, "amount_fail": 0,
        "nan_count": 0,
    }
    for i, (idx, row) in enumerate(df.iterrows()):
        if i % 10 == 0:
            progress.progress((i + 1) / total, text=f"⏳ 正在分析 {i+1}/{total} ...")
            status_text.text(f"📊 已筛选 {len(results)} 只候选股（已处理 {i+1}/{total}）")
        try:
            symbol = str(row["代码"]).zfill(6)
            name = str(row["名称"])
            # NaN 检测：涨跌幅和最新价必须有，其他字段根据数据源情况放宽
            if pd.isna(row["涨跌幅"]) or pd.isna(row["最新价"]):
                diag["nan_count"] += 1
                continue
            chg = float(row["涨跌幅"])
            close = float(row.get("最新价", 0))
            # 量比/成交额/换手率：允许缺失（数据源可能不提供），缺失时跳过对应筛选条件
            vol_ratio = float(row["量比"]) if not pd.isna(row.get("量比")) else None
            amount = float(row["成交额"]) if not pd.isna(row.get("成交额")) else None
            turnover = float(row["换手率"]) if not pd.isna(row.get("换手率")) else None
            # 如果核心成交数据全部缺失，标记为NaN
            if vol_ratio is None and amount is None and turnover is None:
                diag["nan_count"] += 1
                continue
            # 筛选条件
            if not (config["pct_min"] < chg < config["pct_max"]):
                diag["pct_fail"] += 1
                continue
            diag["pct_pass"] += 1
            # 量比：缺失时跳过，收盘后为0也跳过
            if vol_ratio is not None and (_is_market_open() or vol_ratio > 0):
                if vol_ratio < config["vol_ratio_min"]:
                    diag["vol_fail"] += 1
                    continue
            diag["vol_pass"] += 1
            # 换手率：缺失时跳过
            if turnover is not None:
                if not (config["turnover_min"] < turnover < config["turnover_max"]):
                    diag["turnover_fail"] += 1
                    continue
            diag["turnover_pass"] += 1
            # 成交额：缺失时跳过
            if amount is not None:
                if amount < config["amount_min"]:
                    diag["amount_fail"] += 1
                    continue
            diag["amount_pass"] += 1
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
                "量比": round(vol_ratio, 2) if vol_ratio is not None else None,
                "换手率%": round(turnover, 2) if turnover is not None else None,
                "成交额亿": round(amount / 1e8, 2) if amount is not None else None,
                "最新价": round(close, 2),
                "抢筹": rush["label"],
                "抢筹评分": rush["score"],
                "_sort_key": vol_ratio if vol_ratio is not None else 0,
            })
        except Exception:
            errors += 1
            continue
    progress.progress(1.0, text="✅ 选股完成！")
    status_text.text(f"✅ 选股完成！共找到 {len(results)} 只候选股")
    # 筛选漏斗诊断 → 无论结果是否为空都要写入 session_state
    st.session_state["filter_diag"] = diag
    st.session_state["filter_total"] = total
    if not results:
        st.warning("⚠️ 未找到符合条件的股票，请调整筛选条件或稍后重试（查看下方漏斗图定位瓶颈）")
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
    if summary.get("rush_distribution"):
        rush_str = " | ".join([f"{k}:{v}" for k, v in summary["rush_distribution"].items()])
        st.caption(f"🏷️ 抢筹分布：{rush_str}")
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
    page_title="实时智能选股",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)
st.title("📈 实时智能选股工具")
st.caption("基于东方财富实时数据 + 抢筹分析（A股专用）")

# ---- 侧边栏 ----
with st.sidebar:
    st.header("⚙️ 参数设置")
    now = beijing_now()
    trading_day = is_trading_day()
    # 交易时段检测
    is_trading_hours = trading_day and _is_market_open()
    # 时段提示
    if is_trading_hours:
        st.success("✅ 当前为交易时段，数据实时更新")
    elif trading_day:
        if now.hour < 9 or (now.hour == 9 and now.minute < 30):
            st.info("ℹ️ 盘前时段，数据为昨日收盘价")
        elif now.hour >= 15:
            st.info("ℹ️ 已收盘，数据为今日收盘价")
        else:
            st.info("ℹ️ 午间休市（11:30-13:00）")
    else:
        st.warning("⚠️ 今日非交易日，展示历史数据或手动运行")
    # 抢筹分析开关（始终可用）
    enable_rush = st.checkbox(
        "🔍 启用抢筹分析",
        value=True,
        help="分析尾盘抢筹强度（基于近30分钟分时数据）"
    )
    st.caption(f"✅ 抢筹分析：{'已启用' if enable_rush else '已禁用'}")
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
        for key in ["history_results", "last_results", "last_results_ts", "last_summary"]:
            if key in st.session_state:
                del st.session_state[key]
        st.success("✅ 历史数据已清空")
        st.rerun()
    st.divider()
    st.caption(f"🕐 当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}")
    st.caption("数据来源：东方财富")

def render_filter_funnel():
    """渲染筛选漏斗可视化 + 数据源/质量诊断"""
    diag = st.session_state.get("filter_diag")
    total = st.session_state.get("filter_total", 0)
    data_source = st.session_state.get("data_source", "unknown")
    data_quality = st.session_state.get("data_quality", {})

    # ---- 数据源标识 ----
    source_labels = {
        "eastmoney": ("✅", "东方财富", "green"),
        "sina": ("⚠️", "新浪财经（量比缺失）", "orange"),
        "akshare": ("⚠️", "akshare（备用源）", "orange"),
        "none": ("❌", "无可用数据源", "red"),
    }
    label = source_labels.get(data_source, ("❓", data_source, "gray"))
    st.caption(f"{label[0]} 数据源：**{label[1]}**")

    # ---- 数据质量诊断 ----
    if data_quality and data_quality.get("total", 0) > 0:
        dq = data_quality
        total_stocks = dq["total"]
        qcols = st.columns(5)
        qcols[0].metric("总股票数", total_stocks)
        qcols[1].metric("有效涨跌幅", f"{dq['valid_pct']}", delta=f"{dq['valid_pct']/max(total_stocks,1)*100:.0f}%")
        qcols[2].metric("有效量比", f"{dq['valid_vol']}", delta=f"{dq['valid_vol']/max(total_stocks,1)*100:.0f}%")
        qcols[3].metric("有效成交额", f"{dq['valid_amount']}", delta=f"{dq['valid_amount']/max(total_stocks,1)*100:.0f}%")
        qcols[4].metric("有效换手率", f"{dq['valid_turnover']}", delta=f"{dq['valid_turnover']/max(total_stocks,1)*100:.0f}%")

    if diag is None or total == 0:
        return
    st.subheader("🔍 筛选漏斗分析")
    stages = [
        ("总股票数", total, total, "blue"),
        ("NaN 异常", diag["nan_count"], total - diag["nan_count"], "gray"),
        ("涨幅 2%~7%", diag["pct_fail"], diag["pct_pass"], "green"),
        ("量比 >=1.2", diag["vol_fail"], diag["vol_pass"], "orange"),
        ("换手率 3%~15%", diag["turnover_fail"], diag["turnover_pass"], "purple"),
        ("成交额 >=1亿", diag["amount_fail"], diag["amount_pass"], "red"),
    ]
    cols = st.columns(len(stages))
    for i, (label, fail, pass_cnt, color) in enumerate(stages):
        with cols[i]:
            st.metric(label, pass_cnt if i > 0 else total,
                     delta=f"-{fail}" if i > 0 and fail > 0 else None,
                     delta_color="inverse")
    # 漏斗进度条
    total_n = total if total > 0 else 1
    for label, fail, pass_cnt, color in stages[1:]:
        pct = pass_cnt / total_n * 100
        st.progress(pct / 100, text=f"{label} → {pass_cnt} 只 ({pct:.1f}%)")

# ---- 主页面 ----
# 渲染统计摘要
render_summary_panel()
# 渲染筛选漏斗
render_filter_funnel()
# 加载并显示最近结果
df_result, cached_ts = load_last_results()
if df_result is not None and not df_result.empty:
    st.subheader(f"📊 候选股票列表（共 {len(df_result)} 只）")
    if cached_ts:
        st.caption(f"⏱️ 缓存时间戳：{cached_ts}")
    st.dataframe(
        df_result,
        use_container_width=True,
        hide_index=True,
    )
    # 导出CSV
    csv_data = df_result.to_csv(index=False, encoding="utf-8-sig")
    st.download_button(
        label="📥 导出 CSV",
        data=csv_data,
        file_name=f"实时选股_{now.strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
    )
else:
    st.info("💡 暂无选股结果，请点击侧边栏「运行选股」按钮")
st.divider()
render_yesterday_review()
st.divider()
st.caption(f"🔄 数据更新时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（北京时间）")
st.caption("⚠️ 以上内容仅供参考，不构成投资建议。股市有风险，投资需谨慎。")
