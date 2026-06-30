import time
import json
import requests
import pandas as pd
import streamlit as st
from datetime import datetime, timedelta

# ============================================================
# 工具函数
# ============================================================
def beijing_now():
    return datetime.now() + timedelta(hours=8)

def today_str():
    return beijing_now().strftime("%Y-%m-%d")

def is_trading_day():
    return beijing_now().weekday() < 5

def _is_market_open():
    now = beijing_now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return (930 <= t <= 1130) or (1300 <= t <= 1500)

# ============================================================
# 数据源：三级回退（东方财富 → 新浪 → akshare）
# ============================================================
EASTMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
}
SINA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn",
    "Accept": "*/*",
}

def _em_fetch(url, params):
    try:
        resp = requests.get(url, params=params, headers=EASTMONEY_HEADERS, timeout=15)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None

def _fetch_eastmoney() -> pd.DataFrame | None:
    """数据源1：东方财富"""
    try:
        all_rows = []
        page = 1
        while True:
            params = {
                "pn": page, "pz": 5000, "po": 1, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2, "fid": "f3",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f2,f3,f4,f5,f6,f7,f8,f10,f12,f14,f15,f16,f17,f18,f20,f21",
                "_": int(time.time() * 1000),
            }
            data = _em_fetch("https://push2.eastmoney.com/api/qt/clist/get", params)
            if data is None:
                break
            diff_list = data.get("data", {}).get("diff", [])
            if not diff_list:
                break
            all_rows.extend(diff_list)
            total = data.get("data", {}).get("total", 0)
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
        return pd.DataFrame(records)
    except Exception:
        return None

def _sina_fetch_list(max_pages: int = 80) -> list[dict]:
    """新浪：分页获取全A股列表"""
    all_stocks = []
    for page in range(1, max_pages + 1):
        try:
            resp = requests.get(
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
                params={"page": page, "num": 80, "sort": "symbol", "asc": 1,
                        "node": "hs_a", "symbol": "", "_s_r_a": "init"},
                headers=SINA_HEADERS, timeout=15,
            )
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
    """新浪：批量获取实时行情（每次最多400只）"""
    result = {}
    batch_size = 400
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        try:
            symbols = []
            for code in batch:
                prefix = "sh" if code.startswith(("6", "9")) else "sz"
                symbols.append(f"{prefix}{code}")
            resp = requests.get(
                f"https://hq.sinajs.cn/list={','.join(symbols)}",
                headers=SINA_HEADERS, timeout=20,
            )
            if resp.status_code != 200:
                continue
            for line in resp.text.strip().split("\n"):
                if "=" not in line:
                    continue
                parts = line.split("=", 1)
                if len(parts) != 2:
                    continue
                key_part = parts[0]
                val_part = parts[1].rstrip('";\n')
                code = key_part.replace("var hq_str_", "").replace("sh", "").replace("sz", "")
                fields = val_part.split(",")
                if len(fields) < 32:
                    continue
                name = fields[0]
                open_p = float(fields[1]) if fields[1] else None
                pre_close = float(fields[2]) if fields[2] else None
                price = float(fields[3]) if fields[3] else None
                high = float(fields[4]) if fields[4] else None
                low = float(fields[5]) if fields[5] else None
                volume = float(fields[8]) if fields[8] else 0
                amount = float(fields[9]) if fields[9] else 0
                if price and pre_close and pre_close > 0:
                    chg_pct = round((price - pre_close) / pre_close * 100, 2)
                else:
                    chg_pct = None
                result[code] = {
                    "名称": name, "最新价": price, "涨跌幅": chg_pct,
                    "今开": open_p, "昨收": pre_close, "最高": high, "最低": low,
                    "成交额": amount, "量比": None, "换手率": None,
                    "市盈率": None, "总市值": None,
                }
        except Exception:
            continue
    return result

def _fetch_sina() -> pd.DataFrame | None:
    """数据源2：新浪财经"""
    try:
        stock_list = _sina_fetch_list()
        if not stock_list:
            return None
        code_list = [s["code"] for s in stock_list if s.get("code")]
        quotes = _sina_fetch_batch_quotes(code_list)
        records = []
        for s in stock_list:
            code = s.get("code", "")
            if not code or not code.isdigit() or len(code) != 6:
                continue
            q = quotes.get(code, {})
            try:
                trade = float(s["trade"]) if s.get("trade") and s["trade"] != "0.000" else q.get("最新价")
                chg_pct = float(s["changepercent"]) if s.get("changepercent") else q.get("涨跌幅")
                open_p = float(s["open"]) if s.get("open") else q.get("今开")
                high = float(s["high"]) if s.get("high") else q.get("最高")
                low = float(s["low"]) if s.get("low") else q.get("最低")
                pre_close = float(s["settlement"]) if s.get("settlement") else q.get("昨收")
                amount_val = float(s["amount"]) if s.get("amount") else q.get("成交额", 0)
                turnover = float(s["turnoverratio"]) if s.get("turnoverratio") else q.get("换手率")
                pe = float(s["per"]) if s.get("per") else q.get("市盈率")
                mktcap = float(s["mktcap"]) if s.get("mktcap") else q.get("总市值")
                if trade is not None and pre_close is not None and pre_close > 0:
                    chg_amount = round(trade - pre_close, 2)
                else:
                    chg_amount = None
                records.append({
                    "代码": code, "名称": s.get("name", q.get("名称", "")),
                    "涨跌幅": chg_pct, "最新价": trade, "量比": None,
                    "成交额": amount_val, "换手率": turnover,
                    "涨跌额": chg_amount, "最高": high, "最低": low,
                    "今开": open_p, "昨收": pre_close,
                    "市盈率": pe, "总市值": mktcap,
                })
            except Exception:
                continue
        return pd.DataFrame(records) if records else None
    except Exception:
        return None

def _fetch_akshare() -> pd.DataFrame | None:
    """数据源3：akshare 终极回退"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return None
        col_map = {
            "代码": "代码", "名称": "名称", "最新价": "最新价", "涨跌幅": "涨跌幅",
            "涨跌额": "涨跌额", "成交额": "成交额",
            "今开": "今开", "昨收": "昨收", "最高": "最高", "最低": "最低",
            "换手率": "换手率", "量比": "量比",
            "市盈率-动态": "市盈率", "总市值": "总市值",
        }
        existing = {}
        for src, dst in col_map.items():
            if src in df.columns:
                existing[dst] = df[src]
        if "代码" not in existing:
            return None
        return pd.DataFrame(existing)
    except Exception:
        return None

@st.cache_data(ttl=600)
def fetch_realtime_quotes():
    """三级回退获取全A股实时行情"""
    df = None
    source_name = "none"

    # 数据源1：东方财富
    try:
        df = _fetch_eastmoney()
        if df is not None and len(df) >= 100:
            valid_pct = df["涨跌幅"].notna().sum() if "涨跌幅" in df.columns else 0
            if valid_pct / len(df) > 0.5:
                source_name = "eastmoney"
            else:
                df = None
    except Exception:
        pass

    # 数据源2：新浪
    if df is None or len(df) < 100:
        try:
            df_sina = _fetch_sina()
            if df_sina is not None and len(df_sina) >= 100:
                df = df_sina
                source_name = "sina"
        except Exception:
            pass

    # 数据源3：akshare
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

    # 数值转换
    numeric_cols = ["涨跌幅", "最新价", "量比", "成交额", "换手率", "涨跌额",
                    "最高", "最低", "今开", "昨收", "市盈率", "总市值"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 过滤无效数据
    df = df.dropna(subset=["代码", "名称", "涨跌幅", "最新价"])
    df = df[df["代码"].str.match(r"^[0-9]{6}$")]
    df = df.reset_index(drop=True)

    # 数据质量诊断（量比补全后重新统计）
    total_raw = len(df)
    st.session_state["data_source"] = source_name
    st.session_state["data_quality"] = {
        "total": total_raw,
        "valid_pct": int(df["涨跌幅"].notna().sum()),
        "valid_vol": int(df["量比"].notna().sum()),
        "valid_amount": int(df["成交额"].notna().sum()),
        "valid_turnover": int(df["换手率"].notna().sum()),
    }

    # 量比补全：新浪数据源不返回量比字段，从新浪K线数据计算（当日量/5日均量）
    vol_na_mask = df["量比"].isna()
    if vol_na_mask.any():
        _vol_fill_count = 0
        _vol_fill_max = 200  # 最多补全200只，避免耗时过长（每只约0.2s）
        # 先尝试东方财富K线（更快），失败后回退到新浪K线
        for idx in df[vol_na_mask].index:
            if _vol_fill_count >= _vol_fill_max:
                break
            try:
                sym = str(df.loc[idx, "代码"]).zfill(6)
                # 尝试东方财富K线
                klines = fetch_daily_kline(sym, days=30)
                # 如果东方财富失败（收盘后可能不可用），回退到新浪K线
                if not klines or len(klines) < 6:
                    klines = _fetch_sina_kline(sym, days=30)
                if klines and len(klines) >= 6:
                    vols = [k["volume"] for k in klines]
                    avg_5d = sum(vols[-6:-1]) / 5
                    if avg_5d > 0:
                        df.loc[idx, "量比"] = round(vols[-1] / avg_5d, 2)
                        _vol_fill_count += 1
            except Exception:
                pass
        st.session_state["data_quality"]["vol_filled"] = _vol_fill_count

    return df

# ============================================================
# K线数据（日线，用于均线计算）
# ============================================================
@st.cache_data(ttl=3600)
def fetch_daily_kline(symbol: str, days: int = 120) -> list[dict]:
    """获取日K线数据（东方财富），返回 [{date, open, close, high, low, volume}, ...]"""
    try:
        secid = f"1.{symbol}" if symbol.startswith("6") else f"0.{symbol}"
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "secid": secid, "klt": 101, "fqt": 1,  # 日线，前复权
            "end": "20500101", "lmt": days,
            "ut": "fa5fd1943c7b386f172d6893dbfba10b",
            "_": int(time.time() * 1000),
        }
        data = _em_fetch(url, params)
        if data is None:
            return []
        klines = data.get("data", {}).get("klines", [])
        result = []
        for item in klines:
            parts = item.split(",")
            if len(parts) >= 7:
                result.append({
                    "date": parts[0],
                    "open": float(parts[1]), "close": float(parts[2]),
                    "high": float(parts[3]), "low": float(parts[4]),
                    "volume": float(parts[5]), "amount": float(parts[6]),
                })
        return result
    except Exception:
        return []

@st.cache_data(ttl=3600)
def _fetch_sina_kline(symbol: str, days: int = 30) -> list[dict]:
    """获取日K线数据（新浪财经），返回 [{date, open, close, high, low, volume}, ...]
    收盘后仍可用，作为东方财富K线不可用时的回退方案"""
    try:
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
        params = {"symbol": f"{prefix}{symbol}", "scale": 240, "ma": "no", "datalen": days}
        resp = requests.get(url, params=params, headers=SINA_HEADERS, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
        if not data or not isinstance(data, list):
            return []
        result = []
        for item in data:
            try:
                result.append({
                    "date": item.get("day", ""),
                    "open": float(item.get("open", 0)),
                    "close": float(item.get("close", 0)),
                    "high": float(item.get("high", 0)),
                    "low": float(item.get("low", 0)),
                    "volume": float(item.get("volume", 0)),
                    "amount": 0,
                })
            except Exception:
                continue
        return result
    except Exception:
        return []

def calc_mas(klines: list[dict]) -> dict:
    """从K线数据计算均线指标"""
    if len(klines) < 60:
        return {"ma5": None, "ma10": None, "ma20": None, "ma60": None,
                "ma60_5d_ago": None, "ma_bullish": False, "high_10d": None,
                "atr_14": None}
    closes = [k["close"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]

    def sma(data, n):
        if len(data) < n:
            return None
        return sum(data[-n:]) / n

    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    # MA60 5日前
    ma60_5d_ago = sma(closes[:-5], 60) if len(closes) > 65 else None
    # 均线多头：MA5 > MA10 > MA20
    ma_bullish = (ma5 and ma10 and ma20 and ma5 > ma10 > ma20)
    # MA60 向上
    ma60_up = (ma60 and ma60_5d_ago and ma60 > ma60_5d_ago)
    # 10日最高价
    high_10d = max(highs[-10:]) if len(highs) >= 10 else None
    # ATR(14) 用于止损计算
    tr_list = []
    for i in range(1, len(highs)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i-1])
        lc = abs(lows[i] - closes[i-1])
        tr_list.append(max(hl, hc, lc))
    atr_14 = sum(tr_list[-14:]) / 14 if len(tr_list) >= 14 else None

    return {
        "ma5": round(ma5, 2) if ma5 else None,
        "ma10": round(ma10, 2) if ma10 else None,
        "ma20": round(ma20, 2) if ma20 else None,
        "ma60": round(ma60, 2) if ma60 else None,
        "ma_bullish": ma_bullish,
        "ma60_up": ma60_up,
        "high_10d": round(high_10d, 2) if high_10d else None,
        "atr_14": round(atr_14, 2) if atr_14 else None,
    }

# ============================================================
# 分时数据（抢筹分析）
# ============================================================
@st.cache_data(ttl=60)
def fetch_intraday_minute(symbol: str) -> list[dict]:
    """获取当日分时数据"""
    try:
        secid = f"1.{symbol}" if symbol.startswith("6") else f"0.{symbol}"
        url = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
        params = {
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "secid": secid, "ut": "fa5fd1943c7b386f172d6893dbfba10b",
            "_": int(time.time() * 1000),
        }
        data = _em_fetch(url, params)
        if data is None:
            return []
        trends = data.get("data", {}).get("trends", [])
        result = []
        for item in trends:
            parts = item.split(",")
            if len(parts) >= 8:
                result.append({
                    "time": parts[0], "price": float(parts[1]),
                    "volume": float(parts[5]) if parts[5] else 0,
                })
        return result
    except Exception:
        return []

def calc_intraday_rush(minutes: list[dict]) -> dict:
    """计算尾盘抢筹评分（0-100）"""
    if not minutes or len(minutes) < 10:
        return {"label": "数据不足", "score": 0, "detail": ""}
    try:
        last_30 = minutes[-30:] if len(minutes) >= 30 else minutes
        last_5 = minutes[-5:] if len(minutes) >= 5 else minutes
        if not last_30:
            return {"label": "无数据", "score": 0, "detail": ""}

        base_price = last_30[0]["price"]
        end_price = last_30[-1]["price"]
        price_rise = (end_price - base_price) / base_price * 100 if base_price else 0

        vol_last = sum(m["volume"] for m in last_5)
        vol_prev = sum(m["volume"] for m in last_30[:5])
        vol_surge = (vol_last - vol_prev) / max(vol_prev, 1) * 100

        score = 0
        if price_rise > 0.3:
            score += 30
        elif price_rise > 0:
            score += 15
        if vol_surge > 50:
            score += 30
        elif vol_surge > 20:
            score += 15
        if end_price >= last_30[-1]["price"]:
            score += 20
        if len(last_30) >= 3:
            prices = [m["price"] for m in last_30[-3:]]
            if prices[-1] >= prices[-2] >= prices[-3]:
                score += 20

        if score >= 70:
            label = "🔥 强烈抢筹"
        elif score >= 50:
            label = "⚡ 中度抢筹"
        elif score >= 30:
            label = "💡 轻度抢筹"
        else:
            label = "⚠️ 无明显抢筹"
        return {"label": label, "score": min(score, 100), "detail": f"尾盘涨幅{price_rise:.2f}%，量能激增{vol_surge:.0f}%"}
    except Exception:
        return {"label": "异常", "score": 0, "detail": ""}

# ============================================================
# 选股策略1：实时选股（原有逻辑）
# ============================================================
def run_realtime_selection(config: dict, enable_rush: bool, max_stocks: int) -> pd.DataFrame | None:
    """实时选股：涨幅 + 量比 + 换手率 + 成交额"""
    with st.spinner("📡 获取全A股实时行情..."):
        df = fetch_realtime_quotes()
    if df is None or df.empty:
        st.error("⚠️ 获取行情失败，请稍后重试")
        return None

    total = len(df)
    progress = st.progress(0.0, text="🔍 实时选股分析中...")
    status_text = st.empty()
    results = []
    diag = {"total": total, "pct_fail": 0, "vol_fail": 0, "turnover_fail": 0, "amount_fail": 0, "nan_count": 0, "pass": 0}
    rush_cache = {}

    for i, (idx, row) in enumerate(df.iterrows()):
        if i % 20 == 0:
            progress.progress(i / total, text=f"🔍 正在分析 {i+1}/{total} ...")
            status_text.text(f"📊 已筛选 {len(results)} 只候选股（已处理 {i+1}/{total}）")
        try:
            symbol = str(row["代码"]).zfill(6)
            name = str(row["名称"])
            if pd.isna(row["涨跌幅"]) or pd.isna(row["最新价"]):
                diag["nan_count"] += 1
                continue
            chg = float(row["涨跌幅"])
            close = float(row.get("最新价", 0))
            vol_ratio = float(row["量比"]) if pd.notna(row.get("量比")) else None
            amount = float(row["成交额"]) if pd.notna(row.get("成交额")) else None
            turnover = float(row["换手率"]) if pd.notna(row.get("换手率")) else None

            if vol_ratio is None and amount is None and turnover is None:
                diag["nan_count"] += 1
                continue

            # 涨幅过滤
            if not (config["pct_min"] < chg < config["pct_max"]):
                diag["pct_fail"] += 1
                continue
            diag["pct_pass"] += 1

            # 量比过滤
            if vol_ratio is not None and (_is_market_open() or vol_ratio > 0):
                if vol_ratio < config["vol_ratio_min"]:
                    diag["vol_fail"] += 1
                    continue
            diag["vol_pass"] += 1

            # 换手率过滤
            if turnover is not None:
                if not (config["turnover_min"] < turnover < config["turnover_max"]):
                    diag["turnover_fail"] += 1
                    continue
            diag["turnover_pass"] += 1

            # 成交额过滤
            if amount is not None:
                if amount < config["amount_min"]:
                    diag["amount_fail"] += 1
                    continue
            diag["amount_pass"] += 1

            # 抢筹分析
            rush_label = "未分析"
            rush_score = 0
            if enable_rush:
                if symbol not in rush_cache:
                    rush_cache[symbol] = calc_intraday_rush(fetch_intraday_minute(symbol))
                rush = rush_cache[symbol]
                rush_label = rush["label"]
                rush_score = rush["score"]

            diag["pass"] += 1
            results.append({
                "代码": symbol, "名称": name,
                "涨跌幅%": round(chg, 2),
                "量比": round(vol_ratio, 2) if vol_ratio is not None else None,
                "换手率%": round(turnover, 2) if turnover is not None else None,
                "成交额亿": round(amount / 1e8, 2) if amount is not None else None,
                "最新价": round(close, 2),
                "抢筹": rush_label, "抢筹评分": rush_score,
                "_sort_key": vol_ratio if vol_ratio is not None else 0,
            })
        except Exception:
            diag["nan_count"] += 1
            continue

    progress.progress(1.0, text="✅ 实时选股完成！")
    st.session_state["filter_diag"] = diag
    st.session_state["filter_total"] = total

    if not results:
        st.warning(f"⚠️ 未找到符合条件的股票")
        return None

    results.sort(key=lambda x: x["_sort_key"], reverse=True)
    df_result = pd.DataFrame(results[:max_stocks])
    df_result = df_result.drop(columns=["_sort_key"])

    st.session_state["last_summary"] = {
        "mode": "实时选股",
        "total_stocks": total, "passed": len(results), "displayed": min(len(results), max_stocks),
        "avg_pct": round(df_result["涨跌幅%"].mean(), 2),
        "max_vol_ratio": df_result["量比"].max() if "量比" in df_result.columns else 0,
        "errors": diag["nan_count"],
        "rush_dist": df_result["抢筹"].value_counts().to_dict() if "抢筹" in df_result.columns else {},
    }
    return df_result

# ============================================================
# 选股策略2：五维选股法
# 技术面30% + 资金面25% + 消息面15% + 基本面15% + 市场情绪15%
# ============================================================
def _calc_5d_score(row: pd.Series, df_all: pd.DataFrame, cfg: dict) -> dict:
    """计算五维评分（满分100）"""
    detail = {"tech": 0, "fund": 0, "news": 0, "basic": 0, "sent": 0}

    # ---- 技术面（30分）----
    chg = row.get("涨跌幅") or 0
    vol_ratio = row.get("量比")
    vol_ratio_val = vol_ratio if pd.notna(vol_ratio) else 0
    price = row.get("最新价") or 0
    pre_close = row.get("昨收") or 0

    tech = 0
    # 上涨趋势（10分）
    if chg > 0:
        tech += 10
    if chg > 2:
        tech += 5
    # 量价配合（10分）
    if vol_ratio_val >= 1.5 and chg > 0:
        tech += 10
    elif vol_ratio_val >= 1.2:
        tech += 5
    # 突破昨收（5分）
    if price > pre_close and pre_close > 0:
        tech += 5
    # KDJ未超买（5分）：量比不高说明未过热
    if vol_ratio_val < 3:
        tech += 5
    detail["tech"] = min(tech, 30)

    # ---- 资金面（25分）----
    amount = row.get("成交额") or 0
    turnover = row.get("换手率")
    turnover_val = turnover if pd.notna(turnover) else 0

    fund = 0
    # 量比（8分）
    if vol_ratio_val >= cfg.get("fund_vol_ratio", 1.5):
        fund += 8
    elif vol_ratio_val >= 1.0:
        fund += 4
    # 换手率（8分）
    if turnover_val >= cfg.get("fund_turnover_min", 3.0):
        fund += 8
    elif turnover_val >= 2.0:
        fund += 4
    # 成交额（9分）
    if amount >= cfg.get("fund_amount_min", 1e8):
        fund += 9
    elif amount >= 5e7:
        fund += 4
    detail["fund"] = min(fund, 25)

    # ---- 消息面（15分，简化版）----
    news = 0
    # 温和上涨 + 放量 = 可能有正面消息
    if 1.0 < chg < 5.0 and vol_ratio_val >= 1.5:
        news += 8
    elif chg >= 5.0 and vol_ratio_val >= 2.0:
        news += 4  # 涨太多可能有过热消息，减半
    elif 0 < chg <= 1.0:
        news += 5
    # 稳定上涨（连续趋势）
    if 0 < chg < 3.0:
        news += 7
    detail["news"] = min(news, 15)

    # ---- 基本面（15分）----
    basic = 0
    pe = row.get("市盈率") or 0
    mktcap = row.get("总市值") or 0
    pe_min = cfg.get("fund_pe_min", 0)
    pe_max = cfg.get("fund_pe_max", 100)
    mktcap_min = cfg.get("fund_mktcap_min", 50e8)
    mktcap_max = cfg.get("fund_mktcap_max", 500e8)

    if pe_min < pe < pe_max:
        basic += 7
    if mktcap_min < mktcap < mktcap_max:
        basic += 8
    detail["basic"] = min(basic, 15)

    # ---- 市场情绪（15分）----
    sent = 0
    avg_chg = df_all["涨跌幅"].mean() if "涨跌幅" in df_all.columns else 0
    # 大盘情绪（7分）
    if cfg.get("senti_market", True) and avg_chg > 0:
        sent += 7
    elif avg_chg > -1:
        sent += 3
    # 板块热度：换手率高 = 活跃（8分）
    if cfg.get("senti_hot", True) and turnover_val > 5.0:
        sent += 8
    elif turnover_val > 3.0:
        sent += 4
    detail["sent"] = min(sent, 15)

    total = detail["tech"] + detail["fund"] + detail["news"] + detail["basic"] + detail["sent"]
    return {"total": total, "detail": detail}

def run_5d_selection(cfg: dict, max_stocks: int) -> pd.DataFrame | None:
    """五维选股法"""
    with st.spinner("📡 获取全A股实时行情（五维选股）..."):
        df = fetch_realtime_quotes()
    if df is None or df.empty:
        st.error("⚠️ 获取行情失败")
        return None

    total = len(df)
    progress = st.progress(0.0, text="🎯 五维评分中...")
    status_text = st.empty()
    results = []
    diag = {"total": total, "scored": 0, "low_score": 0, "errors": 0}

    for i, (idx, row) in enumerate(df.iterrows()):
        if i % 50 == 0:
            progress.progress(i / total, text=f"🎯 五维评分 {i+1}/{total} ...")
            status_text.text(f"📊 已评分 {len(results)} 只（已处理 {i+1}/{total}）")
        try:
            symbol = str(row["代码"]).zfill(6)
            name = str(row["名称"])
            if pd.isna(row["涨跌幅"]) or pd.isna(row["最新价"]):
                diag["errors"] += 1
                continue
            chg = float(row["涨跌幅"])
            price = float(row["最新价"])

            # 基础过滤：排除ST、涨跌幅异常
            if not (-10 < chg < 22):
                diag["low_score"] += 1
                continue

            score_info = _calc_5d_score(row, df, cfg)
            total_score = score_info["total"]
            if total_score < cfg.get("min_score", 55):
                diag["low_score"] += 1
                continue

            diag["scored"] += 1
            d = score_info["detail"]
            results.append({
                "代码": symbol, "名称": name,
                "涨跌幅%": round(chg, 2), "最新价": round(price, 2),
                "量比": round(row.get("量比"), 2) if pd.notna(row.get("量比")) else None,
                "换手率%": round(row.get("换手率"), 2) if pd.notna(row.get("换手率")) else None,
                "成交额亿": round((row.get("成交额") or 0) / 1e8, 2),
                "五维总分": total_score,
                "技术面": d["tech"], "资金面": d["fund"],
                "消息面": d["news"], "基本面": d["basic"], "市场情绪": d["sent"],
            })
        except Exception:
            diag["errors"] += 1
            continue

    progress.progress(1.0, text="✅ 五维选股完成！")
    st.session_state["filter_diag"] = diag
    st.session_state["filter_total"] = total

    if not results:
        st.warning(f"⚠️ 未找到符合条件的股票（总分>={cfg.get('min_score', 55)}）。淘汰：{diag['low_score']} 只")
        return None

    results.sort(key=lambda x: x["五维总分"], reverse=True)
    df_result = pd.DataFrame(results[:max_stocks])

    st.session_state["last_summary"] = {
        "mode": "五维选股",
        "total_stocks": total, "passed": len(results), "displayed": min(len(results), max_stocks),
        "avg_score": round(df_result["五维总分"].mean(), 1),
        "top_score": df_result["五维总分"].max(),
        "errors": diag["errors"],
    }
    return df_result

# ============================================================
# 选股策略3：尾盘选股法（次日高开概率优化版）
# 固定公式：6大条件
# ============================================================
TAIL_FORMULA = {
    "pct_min": 3.0, "pct_max": 5.0,
    "vol_ratio_min": 1.2,
    "turnover_min": 5.0, "turnover_max": 10.0,
    "mktcap_min": 50e8, "mktcap_max": 200e8,
}

def run_tail_selection(cfg: dict, max_stocks: int) -> pd.DataFrame | None:
    """尾盘选股法 — 次日高开概率优化版"""
    now = beijing_now()

    # 时段检查
    if cfg.get("tail_only_tail", True):
        is_tail = ((now.hour == 14 and now.minute >= 30) or
                    (now.hour == 15 and now.minute == 0))
        if not is_tail and not _is_market_open():
            st.info("ℹ️ 当前已收盘，使用今日数据选股（尾盘策略回测模式）")

    with st.spinner("📡 获取实时行情（尾盘选股）..."):
        df = fetch_realtime_quotes()
    if df is None or df.empty:
        st.error("⚠️ 获取行情失败")
        return None

    total = len(df)
    progress = st.progress(0.0, text="🕐 尾盘选股分析中...")
    status_text = st.empty()
    results = []
    kline_cache: dict[str, dict] = {}      # {symbol: mas_dict} 均线指标缓存
    kline_raw_cache: dict[str, list] = {}   # {symbol: [klines]} 原始K线缓存（用于量比计算）
    diag = {
        "total": total,
        "pct_fail": 0, "vol_fail": 0, "turnover_fail": 0,
        "mktcap_fail": 0, "ma_fail": 0, "high_fail": 0,
        "pass": 0, "kline_fail": 0, "errors": 0,
    }

    for i, (idx, row) in enumerate(df.iterrows()):
        if i % 20 == 0:
            progress.progress(i / total, text=f"🕐 尾盘分析 {i+1}/{total} ...")
            status_text.text(f"📊 已筛选 {len(results)} 只（已处理 {i+1}/{total}）")
        try:
            symbol = str(row["代码"]).zfill(6)
            name = str(row["名称"])
            if pd.isna(row["涨跌幅"]) or pd.isna(row["最新价"]):
                diag["errors"] += 1
                continue

            chg = float(row["涨跌幅"])
            price = float(row["最新价"])
            vol_ratio_raw = row.get("量比")
            vol_ratio = float(vol_ratio_raw) if pd.notna(vol_ratio_raw) else None
            # 量比为空时，尝试从K线数据计算（新浪数据源无量比字段）
            if vol_ratio is None:
                if symbol not in kline_raw_cache:
                    klines_30 = fetch_daily_kline(symbol, days=30)
                    # 东方财富K线失败时回退到新浪K线
                    if not klines_30 or len(klines_30) < 6:
                        klines_30 = _fetch_sina_kline(symbol, days=30)
                    kline_raw_cache[symbol] = klines_30
                klines_30 = kline_raw_cache.get(symbol)
                if klines_30 and len(klines_30) >= 6:
                    vols = [k["volume"] for k in klines_30]
                    vol_5d_avg = sum(vols[-6:-1]) / 5
                    if vol_5d_avg > 0:
                        vol_ratio = round(vols[-1] / vol_5d_avg, 2)
            turnover = float(row["换手率"]) if pd.notna(row.get("换手率")) else None
            amount = float(row["成交额"]) if pd.notna(row.get("成交额")) else None
            mktcap = float(row["总市值"]) if pd.notna(row.get("总市值")) else None

            # ---- 条件1：涨幅 3% <= chg <= 5% ----
            if not (TAIL_FORMULA["pct_min"] <= chg <= TAIL_FORMULA["pct_max"]):
                diag["pct_fail"] += 1
                continue

            # ---- 条件2：量比 > 1.2 ----
            if vol_ratio is not None and vol_ratio <= TAIL_FORMULA["vol_ratio_min"]:
                diag["vol_fail"] += 1
                continue

            # ---- 条件3：换手率 5% <= turnover <= 10% ----
            if turnover is not None:
                if not (TAIL_FORMULA["turnover_min"] <= turnover <= TAIL_FORMULA["turnover_max"]):
                    diag["turnover_fail"] += 1
                    continue

            # ---- 条件4：流通市值 50亿 <= mktcap <= 200亿 ----
            if mktcap is not None:
                if not (TAIL_FORMULA["mktcap_min"] <= mktcap <= TAIL_FORMULA["mktcap_max"]):
                    diag["mktcap_fail"] += 1
                    continue

            # ---- 条件5 & 6：K线技术分析 ----
            if symbol not in kline_cache:
                klines = fetch_daily_kline(symbol, days=120)
                if not klines or len(klines) < 60:
                    kline_cache[symbol] = None
                else:
                    kline_cache[symbol] = calc_mas(klines)
            mas = kline_cache.get(symbol)

            if mas is None:
                diag["kline_fail"] += 1
                continue

            # 条件5：均线多头（MA5 > MA10 > MA20）且 MA60 向上
            if not (mas["ma_bullish"] and mas["ma60_up"]):
                diag["ma_fail"] += 1
                continue

            # 条件6：尾盘创新高（10日新高）且收盘 > MA20
            if mas["high_10d"] is None or mas["ma20"] is None:
                diag["high_fail"] += 1
                continue
            if not (price >= mas["high_10d"] and price > mas["ma20"]):
                diag["high_fail"] += 1
                continue

            # ---- 全部通过 ----
            diag["pass"] += 1

            # 计算建议购入价和止损价
            atr = mas.get("atr_14", 0) or 0
            suggest_buy = round(price * 0.995, 2)  # 回踩0.5%购入
            stop_loss = round(max(price - 2 * atr, price * 0.97), 2)  # 2倍ATR或3%止损
            target_price = round(price * 1.03, 2)  # 目标3%止盈

            # 走势预判
            if mas["ma_bullish"] and mas["ma60_up"] and vol_ratio and vol_ratio > 1.5:
                trend = "📈 强势看多 — 均线多头+量价配合，次日高开概率高"
                recommend = "✅ 强烈推荐"
                recommend_score = 90
            elif mas["ma_bullish"] and mas["ma60_up"]:
                trend = "📊 偏多 — 均线多头，关注次日量能配合"
                recommend = "👍 推荐"
                recommend_score = 75
            elif mas["ma_bullish"]:
                trend = "📉 谨慎 — 均线多头但MA60走平，次日需观察"
                recommend = "⚠️ 谨慎推荐"
                recommend_score = 60
            else:
                trend = "❓ 观望 — 技术形态不完整，建议观望"
                recommend = "⛔ 不推荐"
                recommend_score = 30

            results.append({
                "代码": symbol, "名称": name,
                "涨跌幅%": round(chg, 2),
                "最新价": round(price, 2),
                "量比": round(vol_ratio, 2) if vol_ratio is not None else None,
                "换手率%": round(turnover, 2) if turnover is not None else None,
                "成交额亿": round(amount / 1e8, 2) if amount is not None else None,
                "总市值亿": round(mktcap / 1e8, 2) if mktcap is not None else None,
                "MA5": mas["ma5"], "MA10": mas["ma10"], "MA20": mas["ma20"],
                "MA60": mas["ma60"], "MA60向上": "✅" if mas["ma60_up"] else "❌",
                "10日新高": "✅" if price >= (mas["high_10d"] or 0) else "❌",
                "ATR(14)": mas.get("atr_14"),
                "建议购入价": suggest_buy,
                "止损价": stop_loss,
                "目标价": target_price,
                "走势预判": trend,
                "推荐": recommend,
                "_score": recommend_score,
            })
        except Exception:
            diag["errors"] += 1
            continue

    progress.progress(1.0, text="✅ 尾盘选股完成！")
    st.session_state["filter_diag"] = diag
    st.session_state["filter_total"] = total

    if not results:
        st.warning(f"⚠️ 未找到符合条件的尾盘候选股")
        return None

    results.sort(key=lambda x: x["_score"], reverse=True)
    df_result = pd.DataFrame(results[:max_stocks])
    df_result = df_result.drop(columns=["_score"])

    st.session_state["last_summary"] = {
        "mode": "尾盘选股",
        "total_stocks": total, "passed": len(results), "displayed": min(len(results), max_stocks),
        "avg_pct": round(df_result["涨跌幅%"].mean(), 2),
        "max_vol": df_result["量比"].max() if "量比" in df_result.columns else 0,
        "recommend_count": len(df_result[df_result["推荐"].str.contains("强烈|推荐")]) if "推荐" in df_result.columns else 0,
    }
    return df_result

# ============================================================
# 结果保存 & 加载
# ============================================================
def save_daily_results(df: pd.DataFrame):
    today = today_str()
    if "history_results" not in st.session_state:
        st.session_state["history_results"] = {}
    st.session_state["history_results"][today] = {
        "data": df.to_dict(orient="records"),
        "timestamp": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": st.session_state.get("current_mode", "未知"),
    }
    if len(st.session_state["history_results"]) > 30:
        sorted_dates = sorted(st.session_state["history_results"].keys())
        for old_date in sorted_dates[:-30]:
            del st.session_state["history_results"][old_date]
    st.session_state["last_results"] = df
    st.session_state["last_results_ts"] = beijing_now().strftime("%Y-%m-%d %H:%M:%S")

# ============================================================
# UI 渲染
# ============================================================
def render_filter_funnel():
    """筛选漏斗 + 数据源诊断"""
    diag = st.session_state.get("filter_diag")
    total = st.session_state.get("filter_total", 0)
    data_source = st.session_state.get("data_source", "unknown")
    data_quality = st.session_state.get("data_quality", {})

    # 数据源标识
    source_labels = {
        "eastmoney": ("✅", "东方财富"),
        "sina": ("⚠️", "新浪财经（K线补全量比）"),
        "akshare": ("⚠️", "akshare（备用源）"),
        "none": ("❌", "无可用数据源"),
    }
    label = source_labels.get(data_source, ("❓", data_source))
    st.caption(f"{label[0]} 数据源：**{label[1]}**")

    # 数据质量诊断
    if data_quality and data_quality.get("total", 0) > 0:
        dq = data_quality
        total_s = dq["total"]
        c = st.columns(5)
        c[0].metric("总股票数", total_s)
        c[1].metric("有效涨跌幅", f"{dq['valid_pct']}", f"{dq['valid_pct']/max(total_s,1)*100:.0f}%")
        c[2].metric("有效量比", f"{dq['valid_vol']}", f"{dq['valid_vol']/max(total_s,1)*100:.0f}%")
        c[3].metric("有效成交额", f"{dq['valid_amount']}", f"{dq['valid_amount']/max(total_s,1)*100:.0f}%")
        c[4].metric("有效换手率", f"{dq['valid_turnover']}", f"{dq['valid_turnover']/max(total_s,1)*100:.0f}%")

    if diag is None or total == 0:
        return

    # 漏斗进度条
    st.subheader("🔻 筛选漏斗")
    c = st.columns(6)
    # 通用漏斗
    if "pct_fail" in diag:
        pass_cnt = diag.get("pass", 0)
        # 尾盘漏斗有更多阶段
        if "ma_fail" in diag:
            c[0].metric("总股票", total)
            c[1].metric("通过筛选", pass_cnt)
            c[2].metric("涨幅淘汰", diag.get("pct_fail", 0))
            c[3].metric("量比淘汰", diag.get("vol_fail", 0))
            c[4].metric("换手率淘汰", diag.get("turnover_fail", 0))
            c[5].metric("市值淘汰", diag.get("mktcap_fail", 0))
            c2 = st.columns(5)
            c2[0].metric("均线淘汰", diag.get("ma_fail", 0))
            c2[1].metric("新高淘汰", diag.get("high_fail", 0))
            c2[2].metric("K线缺失", diag.get("kline_fail", 0))
            c2[3].metric("数据异常", diag.get("errors", 0))
        else:
            c[0].metric("总股票", total)
            c[1].metric("通过筛选", pass_cnt)
            c[2].metric("涨幅淘汰", diag.get("pct_fail", 0))
            c[3].metric("量比淘汰", diag.get("vol_fail", 0))
            c[4].metric("成交额淘汰", diag.get("amount_fail", 0))
            c[5].metric("数据异常", diag.get("nan_count", diag.get("errors", 0)))
        if total > 0:
            pct = pass_cnt / total
            st.progress(pct, text=f"筛选通过率：{pct*100:.1f}%")
    # 五维漏斗
    elif "scored" in diag:
        c[0].metric("总股票", total)
        c[1].metric("及格分数", diag.get("scored", 0))
        c[2].metric("分数不足", diag.get("low_score", 0))
        c[3].metric("数据异常", diag.get("errors", 0))
        if total > 0:
            pct = diag.get("scored", 0) / total
            st.progress(pct, text=f"五维及格率：{pct*100:.1f}%")

def render_summary_panel():
    """统计摘要面板"""
    summary = st.session_state.get("last_summary")
    if summary is None:
        return
    st.subheader("📊 选股统计摘要")
    mode = summary.get("mode", "")
    if mode == "五维选股":
        c = st.columns(5)
        c[0].metric("总股票数", summary["total_stocks"])
        c[1].metric("通过筛选", f"{summary['passed']} 只")
        c[2].metric("展示数量", f"{summary['displayed']} 只")
        c[3].metric("平均分", f"{summary['avg_score']}")
        c[4].metric("最高分", f"{summary['top_score']}")
    elif mode == "尾盘选股":
        c = st.columns(5)
        c[0].metric("总股票数", summary["total_stocks"])
        c[1].metric("通过筛选", f"{summary['passed']} 只")
        c[2].metric("展示数量", f"{summary['displayed']} 只")
        c[3].metric("平均涨幅", f"{summary['avg_pct']}%")
        c[4].metric("推荐数量", f"{summary['recommend_count']} 只")
    else:
        c = st.columns(6)
        c[0].metric("总股票数", summary["total_stocks"])
        c[1].metric("通过筛选", f"{summary['passed']} 只")
        c[2].metric("展示数量", f"{summary['displayed']} 只")
        c[3].metric("平均涨幅", f"{summary['avg_pct']}%")
        c[4].metric("最大量比", summary["max_vol_ratio"])
        c[5].metric("数据异常", summary["errors"])
        if summary.get("rush_dist"):
            rush_str = " | ".join([f"{k}:{v}" for k, v in summary["rush_dist"].items()])
            st.caption(f"🏷️ 抢筹分布：{rush_str}")
    st.divider()

def render_results_table(df: pd.DataFrame, mode: str):
    """渲染结果表格 + 个股详情"""
    st.subheader("📋 候选股列表")
    if mode == "五维选股":
        display_cols = ["代码", "名称", "涨跌幅%", "最新价", "五维总分", "技术面", "资金面", "消息面", "基本面", "市场情绪"]
    elif mode == "尾盘选股":
        display_cols = ["代码", "名称", "涨跌幅%", "最新价", "量比", "换手率%", "总市值亿",
                        "MA5", "MA20", "MA60", "MA60向上", "10日新高",
                        "建议购入价", "止损价", "走势预判", "推荐"]
    else:
        display_cols = ["代码", "名称", "涨跌幅%", "量比", "换手率%", "成交额亿", "最新价", "抢筹"]
    display_cols = [c for c in display_cols if c in df.columns]

    # 使用 selectbox 实现"点击查看详情"
    st.caption("💡 在下方选择股票代码查看详情，或点击数据框列头排序")
    st.dataframe(df[display_cols], use_container_width=True, hide_index=True)

    # 个股详情入口
    st.divider()
    stock_codes = df["代码"].tolist()
    stock_names = df["名称"].tolist() if "名称" in df.columns else stock_codes
    options = [f"{c} - {n}" for c, n in zip(stock_codes, stock_names)]
    selected = st.selectbox("🔍 选择股票查看详情", options, key="detail_select")

    if selected:
        code = selected.split(" - ")[0]
        row = df[df["代码"] == code].iloc[0]
        render_stock_detail(row, mode)

    # 下载按钮
    csv_data = df.to_csv(index=False, encoding="utf-8-sig")
    st.download_button(
        "📥 下载选股结果 (CSV)",
        csv_data, f"选股结果_{mode}_{today_str()}.csv", "text/csv",
        use_container_width=True,
    )

def render_stock_detail(row: pd.Series, mode: str):
    """个股详情页：现价、建议购入价、止损价、走势预判、推荐"""
    code = str(row["代码"]).zfill(6)
    name = str(row.get("名称", code))
    price = row.get("最新价", 0)
    chg = row.get("涨跌幅%", 0)

    st.subheader(f"📊 {code} {name} 个股详情")

    # ---- 核心数据卡片 ----
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("现价", f"¥{price:.2f}", f"{chg:+.2f}%")
    c2.metric("今开", f"¥{row.get('今开', '-')}" if pd.notna(row.get('今开')) else "-")
    c3.metric("昨收", f"¥{row.get('昨收', '-')}" if pd.notna(row.get('昨收')) else "-")
    c4.metric("最高", f"¥{row.get('最高', '-')}" if pd.notna(row.get('最高')) else "-")
    c5.metric("最低", f"¥{row.get('最低', '-')}" if pd.notna(row.get('最低')) else "-")

    # ---- 交易建议 ----
    st.subheader("💡 交易建议")
    suggest_buy = row.get("建议购入价")
    stop_loss = row.get("止损价")
    target_price = row.get("目标价")

    bc1, bc2, bc3 = st.columns(3)
    if suggest_buy is not None and pd.notna(suggest_buy):
        bc1.metric("建议购入价", f"¥{suggest_buy:.2f}",
                    f"{(suggest_buy / price - 1) * 100:+.1f}%" if price else "")
    else:
        bc1.metric("建议购入价", "—")

    if stop_loss is not None and pd.notna(stop_loss):
        bc2.metric("止损价", f"¥{stop_loss:.2f}",
                    f"{(stop_loss / price - 1) * 100:+.1f}%" if price else "",
                    delta_color="inverse")
    else:
        bc2.metric("止损价", "—")

    if target_price is not None and pd.notna(target_price):
        bc3.metric("目标价", f"¥{target_price:.2f}",
                    f"{(target_price / price - 1) * 100:+.1f}%" if price else "")
    else:
        bc3.metric("目标价", "—")

    # ---- 走势预判 & 推荐 ----
    trend = row.get("走势预判", "暂无数据")
    recommend = row.get("推荐", "暂无数据")

    st.subheader("🔮 走势预判")
    st.markdown(f"**{trend}**")

    st.subheader("⭐ 是否推荐购入")
    if "强烈推荐" in str(recommend):
        st.success(f"**{recommend}**")
    elif "推荐" in str(recommend):
        st.info(f"**{recommend}**")
    elif "谨慎" in str(recommend):
        st.warning(f"**{recommend}**")
    else:
        st.error(f"**{recommend}**")

    # ---- 均线详情（尾盘模式） ----
    if mode == "尾盘选股":
        st.subheader("📈 均线系统")
        mc1, mc2, mc3, mc4 = st.columns(4)
        mc1.metric("MA5", row.get("MA5", "-"))
        mc2.metric("MA10", row.get("MA10", "-"))
        mc3.metric("MA20", row.get("MA20", "-"))
        mc4.metric("MA60", row.get("MA60", "-"))

        ac1, ac2, ac3 = st.columns(3)
        ac1.metric("MA60方向", row.get("MA60向上", "-"))
        ac2.metric("10日新高", row.get("10日新高", "-"))
        atr_val = row.get("ATR(14)")
        ac3.metric("ATR(14)", f"{atr_val:.2f}" if atr_val and pd.notna(atr_val) else "-")

        # 技术指标说明
        st.caption("""
        **技术指标说明：**
        - **均线多头**：MA5 > MA10 > MA20，短期趋势向上
        - **MA60向上**：中期趋势确认，减少假突破风险
        - **10日新高**：突破近期压力位，多头力量强势
        - **收盘 > MA20**：股价站稳均线上方，有支撑
        - **ATR(14)**：平均真实波幅，用于计算止损距离
        """)

    # ---- K线预览 ----
    with st.expander("📉 查看K线数据（近30日）", expanded=False):
        klines = fetch_daily_kline(code, days=30)
        if klines:
            kdf = pd.DataFrame(klines)
            kdf = kdf[["date", "open", "close", "high", "low", "volume"]]
            kdf.columns = ["日期", "开盘", "收盘", "最高", "最低", "成交量"]
            st.dataframe(kdf.tail(10), use_container_width=True, hide_index=True)
        else:
            st.caption("暂无K线数据")

# ============================================================
# Streamlit 主页面
# ============================================================
st.set_page_config(
    page_title="智能选股工具",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("📈 智能选股工具")
st.caption("五维选股 · 尾盘选股 · 实时选股（A股专用）")

# ---- 选股模式选择 ----
mode = st.sidebar.radio(
    "🎯 选股模式",
    ["实时选股", "五维选股", "尾盘选股"],
    index=0,
    help="实时选股：涨幅+量比+换手率+成交额\n五维选股：技术+资金+消息+基本面+市场情绪\n尾盘选股：14:30-15:00尾盘抢筹策略",
)

# ---- 侧边栏参数 ----
with st.sidebar:
    st.header("⚙️ 参数设置")
    now = beijing_now()
    trading_day = is_trading_day()
    is_trading_hours = trading_day and _is_market_open()

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
        st.warning("⚠️ 今日非交易日")

    # ---- 各模式参数 ----
    config = {}
    enable_rush = False

    if mode == "实时选股":
        st.subheader("📊 实时选股参数")
        enable_rush = st.checkbox("🔍 启用抢筹分析", value=True)
        pct_min = st.number_input("涨幅下限(%)", 0.0, 10.0, 2.0, 0.5)
        pct_max = st.number_input("涨幅上限(%)", 0.0, 10.0, 7.0, 0.5)
        vol_ratio_min = st.number_input("量比下限", 1.0, 5.0, 1.2, 0.1)
        turnover_min = st.number_input("换手率下限(%)", 1.0, 20.0, 3.0, 0.5)
        turnover_max = st.number_input("换手率上限(%)", 1.0, 20.0, 15.0, 0.5)
        amount_min = st.number_input("成交额下限(亿)", 0.5, 10.0, 1.0, 0.5) * 1e8
        max_stocks = st.number_input("📋 最多显示候选股数", 10, 100, 30, 5)
        config = {
            "pct_min": pct_min, "pct_max": pct_max,
            "vol_ratio_min": vol_ratio_min,
            "turnover_min": turnover_min, "turnover_max": turnover_max,
            "amount_min": amount_min,
        }

    elif mode == "五维选股":
        st.subheader("🎯 五维选股参数")
        st.caption("技术面30% + 资金面25% + 消息面15% + 基本面15% + 市场情绪15%")
        min_score = st.number_input("最低总分（满分100）", 30, 100, 55, 5)
        with st.expander("📊 技术面（权重30%）", expanded=True):
            tech_ma = st.checkbox("均线多头排列", value=True)
            tech_macd = st.checkbox("MACD金叉/多头", value=True)
            tech_kdj = st.checkbox("KDJ未超买（K<80）", value=True)
        with st.expander("💰 资金面（权重25%）", expanded=True):
            fund_vol_ratio = st.number_input("量比下限", 1.0, 5.0, 1.5, 0.1)
            fund_turnover_min = st.number_input("换手率下限(%)", 1.0, 20.0, 3.0, 0.5)
            fund_amount_min = st.number_input("成交额下限(亿)", 0.5, 10.0, 1.0, 0.5)
        with st.expander("📰 消息面（权重15%）", expanded=False):
            st.caption("简化版：基于涨跌幅+量比估算，无需API")
        with st.expander("📋 基本面（权重15%）", expanded=False):
            fund_pe_min = st.number_input("市盈率下限", 0.0, 100.0, 0.0, 1.0)
            fund_pe_max = st.number_input("市盈率上限", 0.0, 200.0, 100.0, 5.0)
            fund_mktcap_min = st.number_input("最低市值(亿)", 20.0, 1000.0, 50.0, 10.0)
            fund_mktcap_max = st.number_input("最高市值(亿)", 50.0, 5000.0, 500.0, 10.0)
        with st.expander("😊 市场情绪（权重15%）", expanded=False):
            senti_market = st.checkbox("大盘上涨时加分", value=True)
            senti_hot = st.checkbox("板块热度加分", value=True)
        max_stocks = st.number_input("📋 最多显示候选股数", 10, 100, 30, 5)
        config = {
            "min_score": min_score,
            "tech_ma": tech_ma, "tech_macd": tech_macd, "tech_kdj": tech_kdj,
            "fund_vol_ratio": fund_vol_ratio, "fund_turnover_min": fund_turnover_min,
            "fund_amount_min": fund_amount_min * 1e8,
            "fund_pe_min": fund_pe_min, "fund_pe_max": fund_pe_max,
            "fund_mktcap_min": fund_mktcap_min * 1e8, "fund_mktcap_max": fund_mktcap_max * 1e8,
            "senti_market": senti_market, "senti_hot": senti_hot,
        }

    elif mode == "尾盘选股":
        st.subheader("🕐 尾盘选股参数")
        st.caption("固定公式：次日高开概率优化版")
        st.markdown("""
        | 条件 | 阈值 |
        |------|------|
        | 涨幅 | 3% ~ 5% |
        | 量比 | > 1.2 |
        | 换手率 | 5% ~ 10% |
        | 流通市值 | 50亿 ~ 200亿 |
        | 均线 | MA5 > MA10 > MA20，MA60↑ |
        | 尾盘 | 10日新高 + 收盘 > MA20 |
        """)
        tail_only_tail = st.checkbox("仅尾盘时段运行（14:30-15:00）", value=True)
        max_stocks = st.number_input("📋 最多显示候选股数", 10, 100, 20, 5)
        config = {
            "tail_only_tail": tail_only_tail,
        }

    st.divider()

    # 运行按钮
    if st.button("🔄 运行选股", use_container_width=True, type="primary"):
        st.session_state["current_mode"] = mode
        with st.spinner("正在运行选股逻辑..."):
            if mode == "实时选股":
                df = run_realtime_selection(config, enable_rush, max_stocks)
            elif mode == "五维选股":
                df = run_5d_selection(config, max_stocks)
            elif mode == "尾盘选股":
                df = run_tail_selection(config, max_stocks)
            else:
                df = None
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
    st.caption("数据来源：东方财富 / 新浪财经 / akshare")
    # 量比补全提示
    dq = st.session_state.get("data_quality", {})
    if dq.get("vol_filled", 0) > 0:
        st.caption(f"⚡ 量比补全：已从K线数据为 {dq['vol_filled']} 只股票计算量比（当日量/5日均量）")

# ---- 主页面内容 ----
render_summary_panel()
render_filter_funnel()

# 加载最近一次结果并显示
df_last, ts_last = st.session_state.get("last_results"), st.session_state.get("last_results_ts")
if df_last is not None:
    mode_disp = st.session_state.get("current_mode", "实时选股")
    st.subheader(f"📋 最近选股结果（{mode_disp}）")
    if ts_last:
        st.caption(f"生成时间：{ts_last}")
    render_results_table(df_last, mode_disp)
else:
    st.info("👆 请在左侧边栏设置参数后点击「运行选股」")
