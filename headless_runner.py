# -*- coding: utf-8 -*-
"""
headless_runner.py - 无界面选股脚本（用于 GitHub Actions 定时执行 + 企业微信推送）
"""
import akshare as ak
import pandas as pd
import numpy as np
import requests
import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
def get_config():
    return {
        "pct_min": 2.0,
        "pct_max": 7.0,
        "vol_ratio_min": 1.2,
        "turnover_min": 3.0,
        "turnover_max": 15.0,
        "amount_min": 1e8,
        "max_stocks": 30,
        "wecom": {
            "corpid": os.environ.get("WECOM_CORPID", "wwab9a5075f240347d"),
            "agentid": os.environ.get("WECOM_AGENTID", "1000002"),
            "secret": os.environ.get("WECOM_SECRET", "jnwWrisTzy-ni2iFUOpdciihlGfs4DHQyhOqpj1AM_o"),
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
# 数据获取
# ============================================================
def fetch_realtime_quotes():
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_spot_em()
            if df is None or df.empty:
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
        except Exception:
            if attempt < 2:
                time.sleep(2)
            continue
    return None

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
# 综合评分系统
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

def analyze_main_force_stage(vol_ratio: float, pct: float, turnover: float, close: float, ma20: float | None) -> dict:
    is_high_volume = vol_ratio >= 2.0
    is_high_pct = pct >= 3
    is_above_ma20 = ma20 is not None and close > ma20
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
    elif not is_high_volume and is_high_pct and deviation < 3:
        stage = "📈 主力建仓"
        detail = "温和放量上涨，主力在悄悄收集筹码"
        confidence = "中"
    elif vol_ratio < 1.5 and turnover < 3:
        stage = "⏸️ 横盘整理"
        detail = "缩量横盘，方向不明"
        confidence = "低"
    else:
        stage = "🔀 方向不明"
        detail = "量价关系不明确，建议观望"
        confidence = "低"

    return {"stage": stage, "detail": detail, "confidence": confidence, "deviation": round(deviation, 2)}

def predict_trend(score: int, stage_info: dict, pct: float) -> dict:
    if score >= 80 and "拉升" in stage_info["stage"]:
        trend = "📈 短期看涨"
        suggestion = "✅ 强烈推荐关注"
        reason = "综合评分高，主力处于拉升阶段"
    elif score >= 70 and "建仓" in stage_info["stage"]:
        trend = "📈 中期看涨"
        suggestion = "✅ 建议关注"
        reason = "主力在建仓，评分良好"
    elif score >= 60 and "震荡" in stage_info["stage"]:
        trend = "➡️ 震荡偏多"
        suggestion = "⏳ 等待突破"
        reason = "震荡洗盘阶段，等待放量突破"
    elif score >= 70 and "出货" in stage_info["stage"]:
        trend = "📉 短期看跌"
        suggestion = "⚠️ 建议回避"
        reason = "主力出货迹象，风险较高"
    elif score < 60:
        trend = "📉 短期看跌"
        suggestion = "⚠️ 建议观望"
        reason = "综合评分较低，暂不介入"
    else:
        trend = "➡️ 方向不明"
        suggestion = "⏳ 建议观望"
        reason = "技术指标不明确"
    return {"trend": trend, "suggestion": suggestion, "reason": reason, "confidence": stage_info.get("confidence", "低")}

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

# ============================================================
# 资金流向获取
# ============================================================
def fetch_fund_flow(symbol: str) -> dict:
    result = {
        "institution": {"流入": 0, "流出": 0, "净额": 0},
        "big_trader": {"流入": 0, "流出": 0, "净额": 0},
        "available": False,
        "source": None
    }
    try:
        df = ak.stock_lhb_em(symbol=symbol)
        if df is not None and not df.empty:
            df = df.head(10)
            if "买方席位名称" in df.columns:
                inst_mask = df["买方席位名称"].str.contains("机构", na=False) | df["卖方席位名称"].str.contains("机构", na=False)
                inst_data = df[inst_mask]
                if not inst_data.empty:
                    inst_in = inst_data[inst_data["买卖方向"] == "买入"]["成交额"].sum() / 1e8
                    inst_out = inst_data[inst_data["买卖方向"] == "卖出"]["成交额"].sum() / 1e8
                    result["institution"]["流入"] = round(inst_in, 2)
                    result["institution"]["流出"] = round(inst_out, 2)
                    result["institution"]["净额"] = round(inst_in - inst_out, 2)
                    result["available"] = True
                    result["source"] = "龙虎榜"
                non_inst_mask = ~df["买方席位名称"].str.contains("机构", na=False) & ~df["卖方席位名称"].str.contains("机构", na=False)
                bt_data = df[non_inst_mask]
                if not bt_data.empty:
                    bt_in = bt_data[bt_data["买卖方向"] == "买入"]["成交额"].sum() / 1e8
                    bt_out = bt_data[bt_data["买卖方向"] == "卖出"]["成交额"].sum() / 1e8
                    result["big_trader"]["流入"] = round(bt_in, 2)
                    result["big_trader"]["流出"] = round(bt_out, 2)
                    result["big_trader"]["净额"] = round(bt_in - bt_out, 2)
                    result["available"] = True
                    result["source"] = "龙虎榜"
    except Exception:
        pass
    return result

def get_fund_flow_summary(symbol: str) -> dict:
    data = fetch_fund_flow(symbol)
    if not data["available"]:
        return {"has_data": False, "message": "暂无近10日资金流向数据"}
    inst = data["institution"]
    big = data["big_trader"]
    inst_status = "净流入" if inst["净额"] > 0 else ("净流出" if inst["净额"] < 0 else "持平")
    big_status = "净流入" if big["净额"] > 0 else ("净流出" if big["净额"] < 0 else "持平")
    return {
        "has_data": True,
        "source": data.get("source", "龙虎榜"),
        "institution": {"流入": inst["流入"], "流出": inst["流出"], "净额": inst["净额"], "status": inst_status},
        "big_trader": {"流入": big["流入"], "流出": big["流出"], "净额": big["净额"], "status": big_status}
    }

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
        return None
    except Exception:
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
        print("⚠️ 企业微信配置不完整")
        return False

    token = get_wecom_access_token(corpid, secret)
    if token is None:
        print("⚠️ 获取 access_token 失败")
        return False

    now = beijing_now()
    top_stocks = df.head(5)

    msg_lines = [
        f"📈 尾盘智能选股结果",
        f"🕐 时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"📊 共筛选出 {len(df)} 只候选股",
        f"📈 平均涨幅：{summary.get('avg_pct', 0)}%",
        f"⭐ 最高评分：{summary.get('max_score', 0)}分",
        "",
        "🏆 TOP5 精选",
    ]

    for i, (_, row) in enumerate(top_stocks.iterrows(), 1):
        code = row.get("代码", "-")
        name = row.get("名称", "-")
        chg = row.get("涨跌幅%", 0)
        vol = row.get("量比", 0)
        score = row.get("综合评分", 0)
        stage = row.get("主力阶段", "-")
        emoji = "🚀" if chg > 5 else ("🔥" if chg > 3 else "📌")
        msg_lines.append(f"{i}. {emoji} {name}（{code}）")
        msg_lines.append(f"   涨幅：{chg:+.2f}% ｜ 量比：{vol:.2f} ｜ 评分：{score}分")
        msg_lines.append(f"   {stage}")

    msg_lines.append("")
    msg_lines.append("⚠️ 以上内容仅供参考，不构成投资建议")

    msg = "\n".join(msg_lines)
    print(msg)

    try:
        send_url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
        payload = {
            "touser": touser,
            "msgtype": "text",
            "agentid": int(agentid),
            "text": {"content": msg}
        }
        resp = requests.post(send_url, json=payload, timeout=10)
        data = resp.json()
        if data.get("errcode") == 0:
            return True
        else:
            print(f"⚠️ 发送失败: {data.get('errmsg', '未知错误')}")
            return False
    except Exception as e:
        print(f"⚠️ 发送异常: {e}")
        return False

# ============================================================
# 主选股逻辑
# ============================================================
def run_and_push():
    print(f"🕐 {beijing_now().strftime('%Y-%m-%d %H:%M:%S')} 开始执行选股...")

    if not is_trading_day():
        print("⚠️ 今日非交易日，跳过执行")
        return

    if not is_tail_time():
        print("⚠️ 当前非尾盘时段（14:30-15:00），跳过执行")
        return

    print("📊 获取实时行情...")
    df = fetch_realtime_quotes()
    if df is None:
        print("❌ 获取实时行情失败")
        return

    config = get_config()
    total = len(df)
    results = []

    print("🔍 执行选股筛选...")
    for _, row in df.iterrows():
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
            fund_summary = get_fund_flow_summary(symbol)

            # 资金流向对评分的修正
            score_boost = 0
            if fund_summary.get("has_data"):
                inst_net = fund_summary["institution"]["净额"]
                big_net = fund_summary["big_trader"]["净额"]
                if inst_net > 0 and big_net > 0:
                    score_boost = 10
                elif inst_net > 0 or big_net > 0:
                    score_boost = 5
                elif inst_net < 0 and big_net < 0:
                    score_boost = -10
            adjusted_score = min(100, composite_score + score_boost)
            trend_info = predict_trend(adjusted_score, stage_info, chg)

            results.append({
                "代码": symbol,
                "名称": name,
                "涨跌幅%": round(chg, 2),
                "量比": round(vol_ratio, 2),
                "换手率%": round(turnover, 2),
                "成交额亿": round(amount / 1e8, 2),
                "最新价": round(close, 2),
                "MA20": round(ma20, 2) if ma20 else "-",
                "综合评分": adjusted_score,
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
                "_sort_key": adjusted_score,
            })
        except Exception as e:
            continue

    if not results:
        print("⚠️ 未找到符合条件的股票")
        return

    results.sort(key=lambda x: x["_sort_key"], reverse=True)
    df_result = pd.DataFrame(results[:config["max_stocks"]])
    df_result = df_result.drop(columns=["_sort_key"])

    summary = {
        "total_stocks": total,
        "passed": len(results),
        "displayed": min(len(results), config["max_stocks"]),
        "avg_pct": round(df_result["涨跌幅%"].mean(), 2),
        "max_vol_ratio": round(df_result["量比"].max(), 2),
        "max_score": int(df_result["综合评分"].max()),
        "errors": 0,
    }

    print(f"✅ 选股完成！共 {len(df_result)} 只候选股")
    print(df_result[["代码", "名称", "涨跌幅%", "量比", "综合评分"]].to_string(index=False))

    print("📱 推送到企业微信...")
    success = send_wecom_message(df_result, summary)
    if success:
        print("✅ 推送成功！")
    else:
        print("❌ 推送失败，请检查企业微信配置")

    df_result.to_csv(f"result_{today_str()}.csv", index=False, encoding="utf-8-sig")
    print(f"📁 结果已保存到 result_{today_str()}.csv")

if __name__ == "__main__":
    run_and_push()
