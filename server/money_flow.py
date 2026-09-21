"""分时资金流与盘口事实。Python 3.9+，只用标准库。

两类数据，口径不同，都只作为**事实展示**，不参与任何触发判断（触发规则见 sentinel_rules.py）：

1. **东方财富分钟级资金流**（非官方接口，免 key）：
   push2.eastmoney.com/api/qt/stock/fflow/kline/get?lmt=0&klt=1&secid=<1|0>.<代码>&fields2=f51..f56
   返回当天逐分钟的**累计净流入（元）**，每行 `时间,主力,小单,中单,大单,超大单`。实测
   `主力 = 大单 + 超大单`（sz300458 收盘：-1.488亿 + -0.757亿 = -2.245亿）。
   它按单笔成交额分档估算，不是交易所披露的数据；接口随时可能限流或改版，所以取不到就返回原因，
   调用方必须能在没有它的情况下照常工作。
2. **腾讯行情里现成的字段**（live_quote.snapshot 已经在取）：外盘/内盘（当日累计的主动买/主动卖手数）、
   委比。永远和行情一起到，不需要额外请求。

单位一律折成"元"，展示时才转"亿/万"。
"""
import json
import re
from datetime import datetime
from urllib.request import Request, urlopen

from collect_quotes import CST

ENDPOINT = 'https://push2.eastmoney.com/api/qt/stock/fflow/kline/get'
FIELDS = ('main', 'small', 'mid', 'large', 'xlarge')          # 与接口 f52..f56 的顺序一致
LABEL = {'main': '主力', 'xlarge': '超大单', 'large': '大单', 'mid': '中单', 'small': '小单'}
SOURCE_NOTE = '东方财富按单笔成交额分档估算，非交易所披露；“主力”＝超大单＋大单。'
DEFAULT_TIMEOUT_S = 4
MAX_BYTES = 200_000


class FlowError(ValueError):
    """资金流取数或解析失败。原因会展示给用户，所以要写人话。"""


def secid(symbol):
    m = re.fullmatch(r'(sh|sz)(\d{6})', symbol or '')
    if not m:
        raise FlowError('资金流只支持沪深个股代码: %s' % symbol)
    return '%s.%s' % ('1' if m.group(1) == 'sh' else '0', m.group(2))


def _http_get(url, timeout):
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://quote.eastmoney.com/'})
    with urlopen(req, timeout=timeout) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise FlowError('资金流响应过大')
    return raw


def _num(text):
    try:
        v = float(text)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float('inf') else None


def parse_flow(raw, symbol, fetched_at=None):
    """把接口响应整理成 {rows, trade_date, ...派生量}。任何一行格式不对就整体报错——
    半截数据比没有数据更容易误导。全是 '-' 的行（开盘前）跳过。"""
    fetched_at = fetched_at or datetime.now(CST)
    try:
        body = json.loads(raw)
        klines = body['data']['klines']
    except (ValueError, KeyError, TypeError) as exc:
        raise FlowError('资金流响应结构异常（%s）' % type(exc).__name__) from exc
    if not isinstance(klines, list):
        raise FlowError('资金流响应结构异常（klines 不是列表）')
    rows, trade_date = [], None
    for line in klines:
        parts = str(line).split(',')
        m = re.fullmatch(r'(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2})', parts[0]) if parts else None
        if not m or len(parts) < 6:
            raise FlowError('资金流行格式异常: %r' % line)
        values = [_num(p) for p in parts[1:6]]
        if any(v is None for v in values):
            continue
        day = m.group(1)
        trade_date = trade_date or day
        if day != trade_date:
            raise FlowError('资金流跨日期: %s 与 %s' % (trade_date, day))
        row = {'t': m.group(2) + m.group(3)}
        row.update(zip(FIELDS, values))
        rows.append(row)
    if not rows:
        raise FlowError('资金流暂无数据（可能尚未开盘或该股无数据）')
    return {'symbol': symbol, 'trade_date': trade_date, 'rows': rows, 'as_of': rows[-1]['t'],
            'fetched_at': fetched_at.isoformat(), 'source': 'eastmoney', **derive(rows)}


def derive(rows):
    """从累计序列派生：当前累计、近 N 个交易分钟变化、累计峰值与回落、最近一次正负转向。"""
    cur = rows[-1]
    out = {k: cur[k] for k in FIELDS}

    def change(n):
        base = rows[-1 - n]['main'] if len(rows) > n else 0.0
        return cur['main'] - base

    out['main_5m'], out['main_30m'] = change(5), change(30)
    peak = max(rows, key=lambda r: r['main'])
    out['peak_main'], out['peak_time'] = peak['main'], peak['t']
    out['from_peak'] = cur['main'] - peak['main']

    sign = 1 if cur['main'] > 0 else -1 if cur['main'] < 0 else 0
    flip = None
    if sign:
        for i in range(len(rows) - 1, -1, -1):
            s = 1 if rows[i]['main'] > 0 else -1 if rows[i]['main'] < 0 else 0
            if s == -sign:          # 恰好为 0 的分钟不算转向，穿过 0 才算
                flip = {'at': rows[i + 1]['t'], 'from': '净流入' if s > 0 else '净流出',
                        'to': '净流入' if sign > 0 else '净流出'}
                break
    out['flip'] = flip
    return out


def fetch_flow(symbol, now=None, timeout=DEFAULT_TIMEOUT_S, http_get=None):
    """取今天的分钟资金流。失败一律抛 FlowError（含人话原因）。数据日期不是今天也算失败：
    盘前/非交易日接口返回的是上一交易日，不能当今天的资金流展示。"""
    now = now or datetime.now(CST)
    url = '%s?lmt=0&klt=1&secid=%s&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56' % (ENDPOINT, secid(symbol))
    try:
        raw = (http_get or _http_get)(url, timeout)
    except FlowError:
        raise
    except OSError as exc:
        raise FlowError('资金流请求失败（%s）' % type(exc).__name__) from exc
    flow = parse_flow(raw, symbol, now)
    if flow['trade_date'] != now.date().isoformat():
        raise FlowError('资金流数据日期是 %s，不是今天' % flow['trade_date'])
    return flow


def book_facts(quote):
    """腾讯行情里的内外盘与委比。字段缺失的项不出现（不写 0）。"""
    out = {}
    outer, inner = _num(quote.get('outer_vol')), _num(quote.get('inner_vol'))
    if outer is not None and inner is not None and outer + inner > 0:
        out['outer_pct'] = round(outer / (outer + inner) * 100, 1)
    ratio = _num(quote.get('bid_ask_ratio'))
    if ratio is not None:
        out['bid_ask_ratio'] = ratio
    return out


# --- 展示 -------------------------------------------------------------------

def fmt_money(v):
    """元 → 带符号的亿/万。"""
    if v is None:
        return '—'
    sign = '+' if v > 0 else '-' if v < 0 else ''
    a = abs(v)
    return '%s%.2f亿' % (sign, a / 1e8) if a >= 1e8 else '%s%.0f万' % (sign, a / 1e4)


def flow_line(flow, book):
    """告警文字里的一行。没有任何可用数据返回空串。每一项取不到就省略，不写 0。"""
    parts = []
    if flow:
        m30 = '（近30分钟 %s）' % fmt_money(flow['main_30m'])
        parts.append('主力 %s%s' % (fmt_money(flow['main']), m30))
        parts.append('大单 %s' % fmt_money(flow['large']))
    if book and 'outer_pct' in book:
        parts.append('外盘占比 %.1f%%' % book['outer_pct'])
    if book and 'bid_ask_ratio' in book:
        parts.append('委比 %+.1f%%' % book['bid_ask_ratio'])
    return '资金｜' + '｜'.join(parts) if parts else ''


def compact(flow):
    """存进告警/留存文件的精简版（不带整天序列）。"""
    if not flow:
        return None
    keys = ('as_of', 'main', 'xlarge', 'large', 'mid', 'small', 'main_5m', 'main_30m', 'peak_main', 'peak_time',
            'from_peak', 'flip')
    return {k: flow.get(k) for k in keys}


def half_hour_table(rows):
    """把整天序列抽成每 30 个交易分钟一行（含最后一行），给界面做数据表。"""
    if not rows:
        return []
    picks = [rows[i] for i in range(29, len(rows), 30)]
    if picks[-1:] != [rows[-1]]:
        picks.append(rows[-1])
    return [{'t': '%s:%s' % (r['t'][:2], r['t'][2:]), **{k: r[k] for k in FIELDS}} for r in picks]
