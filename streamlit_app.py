"""
尾盘猎手 — A股尾盘选股神器 (Streamlit版)
数据源：新浪财经 + 东方财富 + AlphaFeed 三级回退
"""
import streamlit as st
import pandas as pd
import requests
import time
import re
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from urllib.parse import quote

# ============================================================
# 页面配置
# ============================================================
st.set_page_config(
    page_title="尾盘猎手 — A股尾盘选股",
    page_icon="🔥",
    layout="wide",
    initial_sidebar_state="expanded"
)

# 自定义CSS
st.markdown("""
<style>
    /* 红涨绿跌 */
    .cell-red { color: #ff3b3b; font-weight: 700; }
    .cell-green { color: #00c851; font-weight: 700; }
    .cell-gold { color: #ffd700; font-weight: 700; }
    .cell-blue { color: #58a6ff; }
    .score-bar { height: 6px; border-radius: 3px; background: linear-gradient(90deg, #ff3b3b, #ffd700); margin-top: 4px; }
    .tag-rush { background: rgba(255,59,59,.2); color: #ff6b6b; padding: 2px 6px; border-radius: 4px; font-size: 11px; }
    .tag-vol { background: rgba(255,215,0,.15); color: #ffd700; padding: 2px 6px; border-radius: 4px; font-size: 11px; }
    .tag-trend { background: rgba(88,166,255,.15); color: #58a6ff; padding: 2px 6px; border-radius: 4px; font-size: 11px; }
    .tag-break { background: rgba(0,200,81,.15); color: #39d353; padding: 2px 6px; border-radius: 4px; font-size: 11px; }
    .tag-bear { background: rgba(0,200,81,.12); color: #00c851; padding: 2px 6px; border-radius: 4px; font-size: 11px; }
    .metric-box { background: #1c2230; border: 1px solid #30363d; border-radius: 8px; padding: 12px; text-align: center; }
    .metric-value { font-size: 22px; font-weight: 700; }
    .metric-label { font-size: 11px; color: #8b949e; }
    .stock-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 10px; }
    .buy-box { background: rgba(255,59,59,.08); border: 1px solid rgba(255,59,59,.3); border-radius: 8px; padding: 14px; margin: 10px 0; }
    .strategy-box { background: #1c2230; border-radius: 8px; padding: 14px; margin: 10px 0; }
    /* 隐藏 Streamlit 默认样式 */
    #MainMenu { visibility: hidden; }
    footer { visibility: hidden; }
    .stApp { background-color: #0d1117; }
</style>
""", unsafe_allow_html=True)

# ============================================================
# 常量
# ============================================================
MARKET_OPEN = {"hour": 9, "min": 30}
MARKET_CLOSE = {"hour": 15, "min": 0}
AF_API_KEY = "sk_2aad58f5ac7741b287a0dfe8c2791514"
AF_BASE = "https://api.alphafeed.org"

# 统一的HTTP请求头，降低被WAF拦截概率
EM_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0',
    'Accept': 'application/json, text/javascript, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Referer': 'https://quote.eastmoney.com/',
}

# ============================================================
# Session State 初始化
# ============================================================
if 'scan_results' not in st.session_state:
    st.session_state.scan_results = []
if 'scan_history' not in st.session_state:
    st.session_state.scan_history = []
if 'watchlist' not in st.session_state:
    st.session_state.watchlist = []
if 'alerts' not in st.session_state:
    st.session_state.alerts = []
if 'index_data' not in st.session_state:
    st.session_state.index_data = {}
if 'scan_count' not in st.session_state:
    st.session_state.scan_count = 0
if 'last_scan_time' not in st.session_state:
    st.session_state.last_scan_time = None
if 'af_quota_exhausted' not in st.session_state:
    st.session_state.af_quota_exhausted = False

# ============================================================
# 数据层
# ============================================================

@st.cache_data(ttl=30)
def fetch_index_quotes():
    """获取三大指数实时行情，带多源回退"""
    # 尝试东方财富
    try:
        url = "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2&fields=f1,f2,f3,f4,f12,f13,f14&secids=1.000001,0.399001,0.399006"
        resp = requests.get(url, headers=EM_HEADERS, timeout=10)
        data = resp.json()
        diffs = data.get('data', {}).get('diff', [])
        result = {}
        for d in diffs:
            code = d.get('f12', '')
            name = d.get('f14', '')
            price = (d.get('f2', 0) or 0)
            chg = (d.get('f3', 0) or 0)
            result[code] = {'name': name, 'price': price, 'chg': chg}
        if result:
            return result
    except Exception:
        pass

    # 回退：新浪财经
    try:
        url = "https://hq.sinajs.cn/list=sh000001,sz399001,sz399006"
        headers = {'Referer': 'https://finance.sina.com.cn', 'User-Agent': 'Mozilla/5.0'}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.encoding = 'gb2312'
        result = {}
        codes = ['000001', '399001', '399006']
        idx = 0
        for line in resp.text.split(';'):
            line = line.strip()
            if not line or 'var hq_str_' not in line:
                continue
            quote = line.split('"')[1]
            parts = quote.split(',')
            if len(parts) < 4:
                continue
            code = codes[idx] if idx < len(codes) else ''
            idx += 1
            name = parts[0]
            current = float(parts[3]) if parts[3] else 0
            prev_close = float(parts[2]) if parts[2] else 0
            chg = ((current - prev_close) / prev_close * 100) if prev_close else 0
            result[code] = {'name': name, 'price': current, 'chg': chg}
        if result:
            return result
    except Exception:
        pass

    # 回退：腾讯财经
    try:
        url = "https://qt.gtimg.cn/q=sh000001,sz399001,sz399006"
        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        resp.encoding = 'gb2312'
        result = {}
        for line in resp.text.split(';'):
            line = line.strip()
            if not line or 'v_sh' not in line and 'v_sz' not in line:
                continue
            quote = line.split('"')[1]
            parts = quote.split('~')
            if len(parts) < 6:
                continue
            code = parts[1]
            name = parts[0]
            price = float(parts[2]) if parts[2] else 0
            chg = float(parts[5]) if parts[5] else 0
            result[code] = {'name': name, 'price': price, 'chg': chg}
        if result:
            return result
    except Exception:
        pass

    st.warning("指数数据获取失败: 所有数据源均不可用")
    return {}

@st.cache_data(ttl=120)
def fetch_alpha_feed_quotes():
    """AlphaFeed 全市场实时行情（额度受限，作为备用源）"""
    # 如果已知额度已耗尽，直接跳过，避免反复触发 429
    if st.session_state.get('af_quota_exhausted', False):
        return []

    try:
        url = f"{AF_BASE}/v1/quotes?universes=CN_Stock"
        resp = requests.get(url, headers={'X-API-Key': AF_API_KEY, 'User-Agent': 'Mozilla/5.0'}, timeout=30)

        if resp.status_code == 429:
            st.session_state.af_quota_exhausted = True
            st.warning("AlphaFeed 额度已用尽（429），已自动降级到东方财富数据源")
            return []

        if resp.status_code != 200:
            return []

        json_data = resp.json()
        data_list = json_data.get('data', [])
        if not isinstance(data_list, list):
            return []
        results = []
        for d in data_list:
            ext = d.get('ext', {}) or {}
            symbol = d.get('symbol', '')
            code = re.sub(r'\.(SH|SZ|BJ)$', '', symbol)
            results.append({
                'code': code,
                'name': ext.get('name', ''),
                'current': d.get('last_price', 0) or 0,
                'prevClose': d.get('prev_close', 0) or 0,
                'changePercent': (ext.get('change_pct', 0) or 0) * 100,
                'change': ext.get('change_amount', 0) or 0,
                'open': d.get('open', 0) or 0,
                'high': d.get('high', 0) or 0,
                'low': d.get('low', 0) or 0,
                'volume': d.get('volume', 0) or 0,
                'amount': d.get('amount', 0) or 0,
                'turnover': (ext.get('turnover_rate', 0) or 0) * 100,
                'amplitude': (ext.get('amplitude', 0) or 0) * 100,
                'float_mktcap': None,
                'vol_ratio': None,
                'sector_flow': None,
                'score': 0,
                'signals': [],
                '_symbol': symbol,
            })
        return results
    except Exception as e:
        st.warning(f"AlphaFeed行情获取失败: {e}")
        return []

@st.cache_data(ttl=120)
def fetch_em_list(board='all', page=1, page_size=100):
    """东方财富全市场列表（带重试）"""
    fs_map = {
        'sh': 'm:1+t:2,m:1+t:23',
        'sz': 'm:0+t:6,m:0+t:80',
        'kc': 'm:1+t:23',
        'bj': 'm:0+t:81',
        'all': 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23'
    }
    fs = fs_map.get(board, fs_map['all'])
    fields = 'f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f14,f15,f16,f17,f18,f20,f21,f23,f62'

    for attempt in range(2):
        try:
            url = (
                f"https://push2.eastmoney.com/api/qt/clist/get"
                f"?pn={page}&pz={page_size}&po=1&np=1&ut=&fltt=2&invt=2&fid=f3"
                f"&fs={quote(fs, safe=':,+')}"
                f"&fields={fields}"
            )
            resp = requests.get(url, headers=EM_HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            diffs = data.get('data', {}).get('diff', [])
            if not diffs:
                return []
            results = []
            for d in diffs:
                f10 = d.get('f10', 0)
                f10_val = float(f10) if f10 and f10 != '-' else None
                results.append({
                    'code': d.get('f12', ''),
                    'name': d.get('f14', ''),
                    'current': d.get('f2', 0) or 0,
                    'changePercent': d.get('f3', 0) or 0,
                    'change': d.get('f4', 0) or 0,
                    'volume': d.get('f5', 0) or 0,
                    'amount': d.get('f6', 0) or 0,
                    'amplitude': d.get('f7', 0) or 0,
                    'turnover': d.get('f8', 0) or 0,
                    'vol_ratio': f10_val,
                    'open': d.get('f17', 0) or 0,
                    'prevClose': d.get('f18', 0) or 0,
                    'high': d.get('f15', 0) or 0,
                    'low': d.get('f16', 0) or 0,
                    'pe': d.get('f9', 0) or 0,
                    'float_mktcap': d.get('f21', 0) or 0,
                    'mktcap': d.get('f20', 0) or 0,
                    'sector_flow': (d.get('f62') / 1e4) if d.get('f62') else None,
                    'score': 0,
                    'signals': [],
                })
            return results
        except Exception as e:
            if attempt == 0:
                time.sleep(0.5)
                continue
            st.warning(f"东方财富数据获取失败: {e}")
            return []


def fetch_em_flow_batch(codes: List[str]) -> Dict[str, float]:
    """批量获取主力资金流向 - 通过东方财富API（带重试）"""
    if not codes:
        return {}

    secids = []
    for c in codes:
        if c.startswith('6') or c.startswith('9'):
            secids.append(f"1.{c}")
        elif c.startswith('0') or c.startswith('3'):
            secids.append(f"0.{c}")
        elif c.startswith('8') or c.startswith('4'):
            secids.append(f"0.{c}")
    if not secids:
        return {}

    secids_str = ','.join(secids[:200])

    for attempt in range(2):
        try:
            url = f"https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2&fields=f12,f62,f184&secids={secids_str}"
            resp = requests.get(url, headers=EM_HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            diffs = data.get('data', {}).get('diff', [])
            result = {}
            for d in (diffs or []):
                code = d.get('f12', '')
                flow = d.get('f62')  # 主力净流入(元)
                if flow is not None:
                    result[code] = flow / 1e4  # 转为万元
            return result
        except Exception:
            if attempt == 0:
                time.sleep(0.5)
                continue
            return {}

    return {}


# ============================================================
# 策略评分引擎
# ============================================================

def calc_signals(stock: Dict) -> List[Dict]:
    """计算技术信号"""
    signals = []
    chg = stock.get('changePercent', 0) or 0
    vr = stock.get('vol_ratio')
    turnover = stock.get('turnover')
    amount = stock.get('amount', 0) or 0
    float_mktcap = stock.get('float_mktcap')
    sector_flow = stock.get('sector_flow')

    # 主力资金分析
    if sector_flow is not None and amount:
        flow_yi = sector_flow / 1e4  # 万元->亿元
        amount_yi = amount / 1e8
        flow_ratio = flow_yi / amount_yi if amount_yi > 0 else 0

        if sector_flow > 5000:
            signals.append({'label': '主力净流入', 'type': 'trend', 'weight': 2, 'flow': sector_flow})
        if sector_flow > 1e4 and flow_ratio > 0.1:
            signals.append({'label': '主力拉升', 'type': 'rush', 'weight': 4, 'flow': sector_flow})
        if sector_flow < -5000:
            signals.append({'label': '主力净流出', 'type': 'bear', 'weight': -2, 'flow': sector_flow})
        if sector_flow < -1e4 and flow_ratio < -0.1:
            signals.append({'label': '主力抛货', 'type': 'bear', 'weight': -4, 'flow': sector_flow})

    # 价格信号
    if chg >= 4.5 and chg < 9.9 and vr and vr >= 1.8:
        signals.append({'label': '强势拉升', 'type': 'rush', 'weight': 3})
    if chg >= 3 and vr and vr >= 1.2:
        signals.append({'label': '量价齐升', 'type': 'vol', 'weight': 2})

    # 缩量整理突破
    if 3 <= chg <= 5.5 and vr and 1.2 <= vr < 1.8 and turnover and 3 <= turnover <= 12:
        signals.append({'label': '缩量突破', 'type': 'vol', 'weight': 2})

    # 换手率
    if turnover and 3 <= turnover <= 12:
        signals.append({'label': '换手活跃', 'type': 'trend', 'weight': 1})
    elif turnover and turnover > 12:
        signals.append({'label': '换手过热', 'type': 'bear', 'weight': -1})
    elif turnover and turnover < 3:
        signals.append({'label': '换手低迷', 'type': 'bear', 'weight': -1})

    # 市值
    if float_mktcap:
        mc = float_mktcap / 1e8
        if 30 <= mc <= 250:
            signals.append({'label': '市值适中', 'type': 'break', 'weight': 1})
        elif mc > 250:
            signals.append({'label': '市值偏大', 'type': 'bear', 'weight': -1})

    # 成交额
    if amount >= 5e8:
        signals.append({'label': '大资金关注', 'type': 'trend', 'weight': 1})

    # 涨幅偏高
    if 5.5 < chg < 9.9:
        signals.append({'label': '涨幅偏高', 'type': 'bear', 'weight': -1})

    return signals


def calc_score(stock: Dict) -> Dict:
    """三维综合评分"""
    signals = calc_signals(stock)
    stock['signals'] = signals
    chg = stock.get('changePercent', 0) or 0
    vr = stock.get('vol_ratio')
    turnover = stock.get('turnover')
    amount = stock.get('amount', 0) or 0
    sector_flow = stock.get('sector_flow')
    high = stock.get('high', 0) or 0
    low = stock.get('low', 0) or 0
    prev_close = stock.get('prevClose', 0) or 0

    # 技术面（40%）
    tech_score = 0
    if 4 <= chg <= 5.5:
        tech_score += 45
    elif 3 <= chg < 4:
        tech_score += 35
    elif chg > 5.5:
        tech_score += 15

    if vr and vr >= 2:
        tech_score += 30
    elif vr and vr >= 1.5:
        tech_score += 25
    elif vr and vr >= 1.2:
        tech_score += 15
    elif not vr:
        tech_score += 10

    if high and prev_close:
        amp = (high - low) / prev_close * 100
        if 2 <= amp <= 6:
            tech_score += 25
        elif amp >= 1.5:
            tech_score += 12
    tech_score = min(100, tech_score)

    # 资金面（35%）
    fund_score = 0
    if turnover and 3 <= turnover <= 12:
        fund_score += 40
    elif turnover and turnover >= 1.5:
        fund_score += 15

    if amount >= 10e8:
        fund_score += 30
    elif amount >= 3e8:
        fund_score += 15

    if sector_flow is not None:
        amount_yi = amount / 1e8 if amount else 0
        flow_yi = sector_flow / 1e4 if sector_flow else 0
        if flow_yi > 0:
            ratio = flow_yi / amount_yi if amount_yi > 0 else 0
            if ratio > 0.15:
                fund_score += 30
            elif ratio > 0.05:
                fund_score += 20
            else:
                fund_score += 10
        elif flow_yi < 0:
            fund_score += 0
        else:
            fund_score += 5
    else:
        fund_score += 5
    fund_score = min(100, fund_score)

    # 情绪面（25%）
    bull_weight = sum(sg['weight'] for sg in signals if sg['weight'] > 0)
    bear_weight = sum(abs(sg['weight']) for sg in signals if sg['weight'] < 0)
    sent_score = max(10, min(100, 50 + bull_weight * 12 - bear_weight * 10))

    total = round(tech_score * 0.4 + fund_score * 0.35 + sent_score * 0.25)
    stock['techScore'] = tech_score
    stock['fundScore'] = fund_score
    stock['sentScore'] = sent_score
    stock['score'] = total
    return stock


def apply_filters(stocks: List[Dict], min_chg: float, max_chg: float,
                  min_vr: float, min_tr: float, max_tr: float,
                  min_mc: float, max_mc: float, strategy: str) -> List[Dict]:
    """应用筛选条件"""
    filtered = []
    for s in stocks:
        chg = s.get('changePercent', 0) or 0
        vr = s.get('vol_ratio')
        tr = s.get('turnover')
        mc = (s.get('float_mktcap') or 0) / 1e8 if s.get('float_mktcap') else 0

        if not (min_chg <= chg <= max_chg):
            continue
        if vr and vr < min_vr:
            continue
        if tr and not (min_tr <= tr <= max_tr):
            continue
        if mc and not (min_mc <= mc <= max_mc):
            continue

        # 策略匹配
        signal_types = [sg['type'] for sg in s.get('signals', [])]
        if strategy == 'rush' and 'rush' not in signal_types:
            continue
        if strategy == 'vol' and vr and not (1.2 <= vr < 2):
            continue
        if strategy == 'rebound' and chg >= 2:
            continue

        filtered.append(s)
    return sorted(filtered, key=lambda x: x.get('score', 0), reverse=True)


def get_strategy_text(stock: Dict) -> str:
    """生成策略分析文本"""
    chg = stock.get('changePercent', 0) or 0
    vr = stock.get('vol_ratio')
    turnover = stock.get('turnover')
    float_mktcap = stock.get('float_mktcap')
    sector_flow = stock.get('sector_flow')
    amount = stock.get('amount', 0) or 0

    mc_str = f"{(float_mktcap / 1e8):.0f}亿" if float_mktcap else '--'
    flow_yi_str = f"{(sector_flow / 1e4):.2f}亿" if sector_flow else '--'
    amt_yi_str = f"{(amount / 1e8):.1f}亿" if amount else '--'
    flow_ratio_str = (
        f"{((sector_flow / 1e4) / (amount / 1e8) * 100):.1f}%"
        if sector_flow and amount else '--'
    )

    lines = []
    lines.append(f"【资金面】成交额 {amt_yi_str}，主力净流入 {flow_yi_str}（占成交额 {flow_ratio_str}）。")

    if sector_flow and sector_flow > 1e4:
        lines.append("主力大幅拉升，资金态度积极，属尾盘强势标的。")
    elif sector_flow and sector_flow > 5000:
        lines.append("主力温和流入，资金关注度高，可适度参与。")
    elif sector_flow and sector_flow < -1e4:
        lines.append(f"⚠️ 主力大幅抛货！净流出 {flow_yi_str}，存在出货风险，建议回避。")
    elif sector_flow and sector_flow < -5000:
        lines.append(f"⚠️ 主力净流出 {flow_yi_str}，有减持迹象，谨慎参与。")
    else:
        lines.append("主力资金方向不明确，关注后续动向。")

    price_line = f"【价格面】当前涨幅 {chg:.2f}%，"
    price_line += f"量比 {vr:.2f}，" if vr else "量比 --，"
    price_line += f"换手 {turnover:.1f}%，" if turnover else "换手 --，"
    price_line += f"流通市值 {mc_str}。"
    lines.append(price_line)

    if 4.5 <= chg <= 5.5 and vr and vr >= 1.8:
        lines.append("尾盘强势拉升特征明显，量能配合良好，属强势尾盘介入标的。")
        lines.append("建议策略：14:30-14:50区间逢低跟进，止损设日内低点-2%，目标隔日冲高3%-5%。")
    elif chg >= 3 and vr and vr >= 1.2:
        lines.append("量价配合正常，属于稳健的尾盘参与标的。")
        lines.append("建议策略：观察14:30后是否持续放量，有回踩不破日内均线可介入，仓位控制在60%以内。")
    else:
        lines.append("涨幅偏低，短期爆发力可能不足。")
        lines.append("建议策略：谨慎参与，若后续放量突破日内高点可小仓位跟进，止损-2.5%。")

    if turnover and 3 <= turnover <= 8:
        lines.append(f"\n【换手分析】换手率 {turnover:.1f}%，处于健康区间，筹码锁定较好，短期抛压可控。")
    elif turnover and turnover > 8 and turnover <= 12:
        lines.append(f"\n【换手分析】换手率 {turnover:.1f}%，换手充分，短线资金活跃，注意次日分歧风险。")

    return "\n".join(lines)


def get_buy_recommendation(stock: Dict) -> Dict:
    """生成买入建议"""
    current = stock.get('current', 0) or 0
    chg = stock.get('changePercent', 0) or 0
    score = stock.get('score', 0)
    low = stock.get('low', 0) or 0
    high = stock.get('high', 0) or 0

    # 建议买入价：当前价下方1%-2%
    buy_price = round(current * 0.985, 2)
    # 止损价：日内低点下方2% 或 当前价下方5%
    stop_loss = round(min(low * 0.98, current * 0.95), 2)
    # 目标价：当前价上方3%-5%
    target_price = round(current * 1.04, 2)
    # 风险比
    risk = (target_price - current) / (current - stop_loss) if (current - stop_loss) > 0 else 0

    if score >= 80:
        rating = "⭐⭐⭐ 强烈推荐"
    elif score >= 65:
        rating = "⭐⭐ 推荐"
    elif score >= 50:
        rating = "⭐ 可关注"
    else:
        rating = "⚠️ 观望"

    return {
        'rating': rating,
        'buy_price': buy_price,
        'stop_loss': stop_loss,
        'target_price': target_price,
        'risk_reward': f"{risk:.1f}:1",
        'advice': get_strategy_text(stock)
    }


# ============================================================
# 扫描逻辑
# ============================================================

def run_scan(board: str, min_chg: float, max_chg: float, min_vr: float,
             min_tr: float, max_tr: float, min_mc: float, max_mc: float,
             strategy: str) -> List[Dict]:
    """执行全市场扫描"""
    progress = st.progress(0, "正在获取全市场数据...")

    # Step 1: 获取全市场行情（东方财富优先，AlphaFeed回退）
    progress.progress(10, "获取东方财富全市场行情...")
    all_stocks = []
    for page in range(1, 8):
        batch = fetch_em_list(board, page, 100)
        if not batch:
            break
        all_stocks.extend(batch)
        progress.progress(10 + page * 8, f"获取第{page}页数据...")

    if not all_stocks:
        progress.progress(10, "东方财富不可用，回退到AlphaFeed...")
        all_stocks = fetch_alpha_feed_quotes()

    if not all_stocks:
        return []

    progress.progress(50, f"已获取 {len(all_stocks)} 只股票，正在初筛...")

    # Step 2: 初筛（涨幅、市值快速过滤）
    candidates = []
    for s in all_stocks:
        chg = s.get('changePercent', 0) or 0
        if not (min_chg - 1 <= chg <= max_chg + 2):
            continue
        mc = (s.get('float_mktcap') or 0) / 1e8 if s.get('float_mktcap') else 0
        if mc and not (min_mc * 0.5 <= mc <= max_mc * 2):
            continue
        candidates.append(s)

    progress.progress(60, f"初筛后 {len(candidates)} 只候选，正在计算评分...")

    # Step 3: 批量获取主力资金流向
    codes = [c['code'] for c in candidates[:200]]
    flow_map = fetch_em_flow_batch(codes)
    for c in candidates:
        if c['code'] in flow_map:
            c['sector_flow'] = flow_map[c['code']]

    # Step 4: 评分计算
    for c in candidates:
        calc_score(c)

    progress.progress(80, "正在应用筛选条件...")

    # Step 5: 精确筛选
    results = apply_filters(candidates, min_chg, max_chg, min_vr, min_tr, max_tr, min_mc, max_mc, strategy)

    progress.progress(100, f"扫描完成，共 {len(results)} 只符合条件")
    return results


# ============================================================
# UI 组件
# ============================================================

def render_index_bar():
    """渲染顶部指数栏"""
    indices = fetch_index_quotes()
    cols = st.columns([2, 2, 2, 1, 2])

    with cols[0]:
        sh = indices.get('000001', {})
        price = sh.get('price', 0)
        chg = sh.get('chg', 0)
        color = "#ff3b3b" if chg >= 0 else "#00c851"
        st.markdown(f"""
        <div class="metric-box">
            <div class="metric-label">上证指数</div>
            <div class="metric-value" style="color:{color}">{price:.2f}</div>
            <div style="font-size:12px;color:{color}">{chg:+.2f}%</div>
        </div>
        """, unsafe_allow_html=True)

    with cols[1]:
        sz = indices.get('399001', {})
        price = sz.get('price', 0)
        chg = sz.get('chg', 0)
        color = "#ff3b3b" if chg >= 0 else "#00c851"
        st.markdown(f"""
        <div class="metric-box">
            <div class="metric-label">深证成指</div>
            <div class="metric-value" style="color:{color}">{price:.2f}</div>
            <div style="font-size:12px;color:{color}">{chg:+.2f}%</div>
        </div>
        """, unsafe_allow_html=True)

    with cols[2]:
        cy = indices.get('399006', {})
        price = cy.get('price', 0)
        chg = cy.get('chg', 0)
        color = "#ff3b3b" if chg >= 0 else "#00c851"
        st.markdown(f"""
        <div class="metric-box">
            <div class="metric-label">创业板指</div>
            <div class="metric-value" style="color:{color}">{price:.2f}</div>
            <div style="font-size:12px;color:{color}">{chg:+.2f}%</div>
        </div>
        """, unsafe_allow_html=True)

    with cols[3]:
        now = datetime.now()
        st.markdown(f"""
        <div class="metric-box">
            <div class="metric-label">当前时间</div>
            <div class="metric-value" style="color:#ffd700;font-size:18px">{now.strftime('%H:%M:%S')}</div>
        </div>
        """, unsafe_allow_html=True)

    with cols[4]:
        scan_count = st.session_state.scan_count
        st.markdown(f"""
        <div class="metric-box">
            <div class="metric-label">今日扫描</div>
            <div class="metric-value" style="color:#ffd700">{scan_count}</div>
            <div style="font-size:10px;color:#8b949e">只符合条件的股票</div>
        </div>
        """, unsafe_allow_html=True)


def render_scan_page():
    """渲染尾盘扫描页"""
    st.markdown("## ⚡ 尾盘扫描")

    # 筛选条件
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        board = st.selectbox("板块", ["沪深两市", "沪市", "深市", "科创板", "北交所"],
                             key="board")
        board_map = {'沪深两市': 'all', '沪市': 'sh', '深市': 'sz', '科创板': 'kc', '北交所': 'bj'}
    with col2:
        min_chg = st.number_input("涨幅下限(%)", 0.0, 20.0, 3.0, 0.1, key="min_chg")
    with col3:
        max_chg = st.number_input("涨幅上限(%)", 0.0, 20.0, 5.5, 0.1, key="max_chg")
    with col4:
        min_vr = st.number_input("量比≥", 0.5, 10.0, 1.2, 0.1, key="min_vr")
    with col5:
        strategy = st.selectbox("策略", ["全部策略", "强势拉升", "缩量突破", "回踩反弹"],
                                key="strategy")
        strategy_map = {'全部策略': 'all', '强势拉升': 'rush', '缩量突破': 'vol', '回踩反弹': 'rebound'}

    col1, col2, col3 = st.columns(3)
    with col1:
        min_tr = st.number_input("换手下限(%)", 0.0, 50.0, 3.0, 0.5, key="min_tr")
    with col2:
        max_tr = st.number_input("换手上限(%)", 0.0, 50.0, 12.0, 0.5, key="max_tr")
    with col3:
        min_mc = st.number_input("市值下限(亿)", 5, 500, 30, 5, key="min_mc")
        max_mc = st.number_input("市值上限(亿)", 5, 500, 250, 10, key="max_mc")

    if st.button("⚡ 开始扫描", type="primary", use_container_width=True):
        with st.spinner("正在扫描全市场..."):
            results = run_scan(
                board_map[board], min_chg, max_chg, min_vr,
                min_tr, max_tr, min_mc, max_mc, strategy_map[strategy]
            )
            st.session_state.scan_results = results
            st.session_state.scan_count += len(results)
            st.session_state.last_scan_time = datetime.now().strftime('%H:%M:%S')

            # 记录历史
            for r in results[:20]:
                st.session_state.scan_history.append({
                    'time': datetime.now().strftime('%m-%d %H:%M'),
                    'code': r['code'],
                    'name': r['name'],
                    'price': r['current'],
                    'score': r['score'],
                    'strategy': ','.join([s['label'] for s in r.get('signals', [])[:3]])
                })

    # 显示结果
    results = st.session_state.scan_results
    if results:
        st.success(f"扫描完成！共找到 {len(results)} 只符合条件的股票")

        # 准备展示数据（无HTML，用于交互式表格）
        display_rows = []
        for i, r in enumerate(results):
            chg = r.get('changePercent', 0) or 0
            flow = r.get('sector_flow')
            display_rows.append({
                '排名': i + 1,
                '代码': r['code'],
                '名称': r['name'],
                '现价': r.get('current', 0) or 0,
                '涨幅(%)': round(chg, 2),
                '量比': r.get('vol_ratio') if r.get('vol_ratio') is not None else 0.0,
                '换手率(%)': r.get('turnover') if r.get('turnover') is not None else 0.0,
                '成交额(亿)': round((r.get('amount') or 0) / 1e8, 2),
                '主力净流入(亿)': round(flow / 1e4, 2) if flow else 0.0,
                '策略信号': ', '.join([s['label'] for s in r.get('signals', [])[:3]]),
                '评分': r.get('score', 0),
            })

        df = pd.DataFrame(display_rows)

        # 使用交互式表格，点击行查看详情
        try:
            event = st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row"
            )
            selected_rows = event.selection.rows if event and hasattr(event, 'selection') else []
            if selected_rows:
                idx = selected_rows[0]
                show_stock_detail(results[idx])
        except Exception as e:
            # 旧版Streamlit不支持on_select，回退到selectbox
            st.warning(f"表格交互需要较新版本Streamlit: {e}")
            stock_names = [f"{r['code']} {r['name']}" for r in results]
            selected = st.selectbox("选择股票查看详情", stock_names)
            if selected:
                idx = stock_names.index(selected)
                show_stock_detail(results[idx])
    else:
        st.info("点击「开始扫描」启动尾盘选股，建议在 14:00 后使用")


def show_stock_detail(stock: Dict):
    """显示个股详情"""
    chg = stock.get('changePercent', 0) or 0
    chg_color = "#ff3b3b" if chg >= 0 else "#00c851"

    st.markdown("---")
    st.markdown(f"### {stock['name']} ({stock['code']})")

    col1, col2 = st.columns([1, 2])
    with col1:
        st.markdown(f"""
        <div style="text-align:center;padding:20px;background:#1c2230;border-radius:8px;">
            <div style="font-size:36px;font-weight:700;color:{chg_color}">{stock.get('current', '--')}</div>
            <div style="font-size:16px;color:{chg_color}">{chg:+.2f}%</div>
        </div>
        """, unsafe_allow_html=True)

    with col2:
        # 核心行情数据
        detail_items = [
            ('开盘价', stock.get('open', '--')),
            ('昨收价', stock.get('prevClose', '--')),
            ('最高价', stock.get('high', '--')),
            ('最低价', stock.get('low', '--')),
            ('量比', f"{stock.get('vol_ratio', '--'):.2f}" if stock.get('vol_ratio') else '--'),
            ('换手率', f"{stock.get('turnover', '--'):.1f}%" if stock.get('turnover') else '--'),
            ('成交额', f"{stock.get('amount', 0)/1e8:.2f}亿" if stock.get('amount') else '--'),
            ('流通市值', f"{stock.get('float_mktcap', 0)/1e8:.0f}亿" if stock.get('float_mktcap') else '--'),
            ('主力净流入', f"{stock.get('sector_flow', 0)/1e4:.2f}亿" if stock.get('sector_flow') else '--'),
        ]
        cols = st.columns(3)
        for i, (key, val) in enumerate(detail_items):
            with cols[i % 3]:
                st.metric(key, val)

    # 三维评分
    st.markdown("#### 综合评分")
    tc = st.columns(3)
    with tc[0]:
        tech = stock.get('techScore', 0)
        st.markdown(f"""
        <div class="metric-box">
            <div class="metric-label">技术面 (40%)</div>
            <div class="metric-value cell-red">{tech}</div>
            <div class="score-bar" style="width:{tech}%"></div>
        </div>
        """, unsafe_allow_html=True)
    with tc[1]:
        fund = stock.get('fundScore', 0)
        st.markdown(f"""
        <div class="metric-box">
            <div class="metric-label">资金面 (35%)</div>
            <div class="metric-value cell-gold">{fund}</div>
            <div class="score-bar" style="width:{fund}%"></div>
        </div>
        """, unsafe_allow_html=True)
    with tc[2]:
        sent = stock.get('sentScore', 0)
        st.markdown(f"""
        <div class="metric-box">
            <div class="metric-label">情绪面 (25%)</div>
            <div class="metric-value cell-blue">{sent}</div>
            <div class="score-bar" style="width:{sent}%"></div>
        </div>
        """, unsafe_allow_html=True)

    # 策略分析
    st.markdown("#### 策略分析")
    strategy_text = get_strategy_text(stock)
    st.markdown(f"""
    <div class="strategy-box">
        <div style="color:#ffd700;font-weight:700;margin-bottom:8px;">📊 尾盘策略分析</div>
        <div style="line-height:1.8;white-space:pre-wrap;">{strategy_text}</div>
    </div>
    """, unsafe_allow_html=True)

    # 信号标签
    signals = stock.get('signals', [])
    if signals:
        signal_html = ''.join([
            f'<span class="tag-{s["type"]}" style="margin:2px">{s["label"]}</span>'
            for s in signals
        ])
        st.markdown(f'<div style="margin:10px 0">{signal_html}</div>', unsafe_allow_html=True)

    # 买入建议
    rec = get_buy_recommendation(stock)
    st.markdown(f"""
    <div class="buy-box">
        <div style="color:#ff6b6b;font-weight:700;margin-bottom:8px;">⚡ 尾盘操作建议</div>
        <table style="width:100%;font-size:13px">
            <tr><td style="color:#8b949e">推荐评级</td><td style="color:#ff3b3b;font-weight:700">{rec['rating']}</td></tr>
            <tr><td style="color:#8b949e">建议买入价</td><td style="color:#ffd700;font-weight:700">¥{rec['buy_price']}</td></tr>
            <tr><td style="color:#8b949e">止损价位</td><td style="color:#00c851;font-weight:700">¥{rec['stop_loss']}</td></tr>
            <tr><td style="color:#8b949e">目标价位</td><td style="color:#ff6b6b;font-weight:700">¥{rec['target_price']}</td></tr>
            <tr><td style="color:#8b949e">风险收益比</td><td style="color:#8b949e">{rec['risk_reward']}</td></tr>
        </table>
    </div>
    """, unsafe_allow_html=True)


def render_watchlist_page():
    """渲染自选股监控页"""
    st.markdown("## 📌 自选股监控")

    # 添加自选股
    col1, col2 = st.columns([3, 1])
    with col1:
        new_code = st.text_input("输入股票代码", placeholder="如 600519", key="watch_code")
    with col2:
        if st.button("+ 添加", use_container_width=True) and new_code:
            if new_code not in [w['code'] for w in st.session_state.watchlist]:
                st.session_state.watchlist.append({
                    'code': new_code,
                    'name': '',
                    'added_at': datetime.now().strftime('%m-%d %H:%M')
                })
                st.rerun()

    if not st.session_state.watchlist:
        st.info("暂无自选股，在上方添加")
        return

    # 刷新自选股数据（优先东方财富，AlphaFeed仅作回退）
    if st.button("🔄 刷新自选股行情"):
        codes = [w['code'] for w in st.session_state.watchlist]
        stock_map = {}

        # 优先使用东方财富
        try:
            em_stocks = []
            for page in range(1, 4):
                batch = fetch_em_list('all', page, 100)
                if not batch:
                    break
                em_stocks.extend(batch)
            stock_map = {s['code']: s for s in em_stocks}
        except Exception as e:
            st.warning(f"东方财富自选股刷新失败: {e}")

        # 东方财富失败或数据不全时，再尝试 AlphaFeed
        if not stock_map:
            all_stocks = fetch_alpha_feed_quotes()
            stock_map = {s['code']: s for s in all_stocks}

        for w in st.session_state.watchlist:
            s = stock_map.get(w['code'])
            if s:
                w['name'] = s.get('name', '')
                w['price'] = s.get('current', 0)
                w['chg'] = s.get('changePercent', 0)

    # 显示自选股列表
    for i, w in enumerate(st.session_state.watchlist):
        chg = w.get('chg', 0)
        chg_color = "#ff3b3b" if chg >= 0 else "#00c851"
        col1, col2, col3 = st.columns([2, 1, 1])
        with col1:
            st.markdown(f"**{w['name'] or '--'}** ({w['code']})")
        with col2:
            st.markdown(f'<span style="font-size:18px;font-weight:700;color:{chg_color}">{w.get("price", "--")}</span>', unsafe_allow_html=True)
        with col3:
            if st.button("删除", key=f"del_watch_{i}"):
                st.session_state.watchlist.pop(i)
                st.rerun()


def render_report_page():
    """渲染今日战报页"""
    st.markdown("## 📊 今日战报")

    # 市场概况
    indices = fetch_index_quotes()
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        sh = indices.get('000001', {})
        st.metric("上证指数", f"{sh.get('price', '--'):.2f}", f"{sh.get('chg', 0):+.2f}%")
    with col2:
        sz = indices.get('399001', {})
        st.metric("深证成指", f"{sz.get('price', '--'):.2f}", f"{sz.get('chg', 0):+.2f}%")
    with col3:
        cy = indices.get('399006', {})
        st.metric("创业板指", f"{cy.get('price', '--'):.2f}", f"{cy.get('chg', 0):+.2f}%")
    with col4:
        st.metric("今日扫描结果", st.session_state.scan_count)

    # 扫描历史
    st.markdown("### 扫描历史记录")
    if st.session_state.scan_history:
        df = pd.DataFrame(st.session_state.scan_history[-50:])
        st.dataframe(df, use_container_width=True)
    else:
        st.info("暂无扫描记录")


# ============================================================
# 主页面
# ============================================================

def main():
    st.title("🔥 尾盘猎手 — A股尾盘选股神器")

    # 顶部指数栏
    render_index_bar()

    # Tab切换
    tab1, tab2, tab3 = st.tabs(["⚡ 尾盘扫描", "📌 自选监控", "📊 今日战报"])

    with tab1:
        render_scan_page()

    with tab2:
        render_watchlist_page()

    with tab3:
        render_report_page()


if __name__ == "__main__":
    main()
