#!/usr/bin/env python3
"""次日关注的兑现结算：昨天规则筛出来的那批票，今天实际走成什么样了。

**这个机制和 next_day_watch 打分必须同时存在，不能只发名单不回看**——名单一发出去就没人
管它对不对，AI 写的「思路/风险」有没有兑现也无从对照。哲学和 scenario_ledger.py 一样：
机械记录，不靠印象；无法下结论的样本单独归类，绝不硬塞进「命中」或「失败」。

**基准价 = 次日开盘价**，因为名单是收盘后发的，次日开盘是最早能实际成交的价格；
同时保留相对昨收的 close_pct 作为对照口径（那是涨跌停池/行情本来就有的口径），两个都存,
不用其中一个替代另一个。

判定分两层：
- result：客观形态——继续涨停(limit_up)/一字(one_word)/炸板(broke)/跌停(limit_down)，
  查不到池子里就按收盘相对开盘的涨跌分 up/flat/down。全部来自 T+1 的 market_review pools
  和 live_quote 快照，不猜。
- verdict：是否「有参考价值」——一字板是 unbuyable（开盘即封死，名单读者根本买不进，
  这类样本不计入命中率，就像 scenario_ledger 里 triggered_unresolved 不计入一样）；
  其余按「收盘价相对开盘价」分 hit/flat/miss；数据不全给 unknown。

取数三级降级（成本从低到高）：
1. T+1 的 pools（market_review 已经抓的，零额外请求）→ result 分类。
2. live_quote 批量快照（一次请求，只支持 sh/sz——bj 代码没有这个接口，直接标 quote_missing）
   → open/high/low/close 相对昨收/开盘的涨跌幅。
3. minute_data 逐只 best-effort（同样只 sh/sz）→ 一句日内路径描述，单只失败不影响其它只。
"""
import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

import market_review
from collect_quotes import CST

PROMPT_VERSION = 'nextday-settle-1'
MIN_N_FOR_RATE = 20            # 沿用 scenario_ledger 的口径：已结算样本不足这个数，不给百分比
ONE_WORD_FIRST_SEAL = '09:26:00'
ONE_WORD_TURNOVER = 2.0
RESULT_LABEL = {'one_word': '一字板', 'limit_up': '再涨停', 'broke': '炸板', 'limit_down': '跌停',
                'up': '收涨', 'flat': '横盘', 'down': '收跌'}
VERDICT_LABEL = {'unbuyable': '买不进', 'hit': '接得住', 'miss': '未接住', 'flat': '持平', 'unknown': '未知'}
LIMITATIONS = ['以次日开盘价为参与成本；一字板开盘即封死，正常买不进，单独计数、不计入命中率。',
               '样本不足 %d 只时只给计数，不给百分比。' % MIN_N_FOR_RATE,
               '结算口径独立于打分口径版本（rules_version），不同版本的兑现率不放在一起比。']


def _f(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


# --- 分类 -----------------------------------------------------------------------------

def _is_one_word(row):
    return (row.get('open_times') == 0 and (row.get('first_seal') or '99') <= ONE_WORD_FIRST_SEAL
            and row.get('turnover') is not None and row['turnover'] < ONE_WORD_TURNOVER)


def classify_pool(symbol, t1_pools):
    """T+1 pools 里这只票的客观形态 → (result, pool_row)。都不在池子里返回 (None, None)。"""
    def rows(kind):
        p = (t1_pools or {}).get(kind)
        return (p or {}).get('rows') or []
    zt_row = next((r for r in rows('zt') if r.get('symbol') == symbol), None)
    if zt_row:
        return ('one_word' if _is_one_word(zt_row) else 'limit_up'), zt_row
    zb_row = next((r for r in rows('zb') if r.get('symbol') == symbol), None)
    if zb_row:
        return 'broke', zb_row
    dt_row = next((r for r in rows('dt') if r.get('symbol') == symbol), None)
    if dt_row:
        return 'limit_down', dt_row
    return None, None


def build_outcome(symbol, t1_pools, quote, minute_path):
    """→ 单只的结算结果 dict。quote 是 live_quote 快照里这只的行（或 None）。"""
    result, prow = classify_pool(symbol, t1_pools)
    out = {'result': result, 'verdict': None, 'open_pct': None, 'high_pct': None, 'low_pct': None,
           'close_pct': None, 'close_vs_open': None, 'high_vs_open': None, 'turnover': None,
           'boards': None, 'quote_missing': quote is None, 'path': minute_path}
    if prow:
        out['boards'] = prow.get('boards')
        out['first_seal'] = prow.get('first_seal')
        out['open_times'] = prow.get('open_times')
        out['turnover'] = prow.get('turnover')
        out['close_pct'] = prow.get('pct')
    if quote:
        last, prev = _f(quote.get('last')), _f(quote.get('previous_close'))
        open_, high, low = _f(quote.get('open')), _f(quote.get('high')), _f(quote.get('low'))
        if prev:
            if last is not None:
                out['close_pct'] = round((last / prev - 1) * 100, 2)
            if high is not None:
                out['high_pct'] = round((high / prev - 1) * 100, 2)
            if low is not None:
                out['low_pct'] = round((low / prev - 1) * 100, 2)
            if open_ is not None:
                out['open_pct'] = round((open_ / prev - 1) * 100, 2)
        if open_ and last is not None:
            out['close_vs_open'] = round((last / open_ - 1) * 100, 2)
        if open_ and high is not None:
            out['high_vs_open'] = round((high / open_ - 1) * 100, 2)
        turnover = _f(quote.get('turnover_pct'))
        if turnover is not None:
            out['turnover'] = turnover
    if result is None and out['close_vs_open'] is not None:
        cv = out['close_vs_open']
        result = 'up' if cv >= 2 else 'down' if cv <= -2 else 'flat'
    out['result'] = result
    if result == 'one_word':
        out['verdict'] = 'unbuyable'
    elif out['close_vs_open'] is not None:
        cv = out['close_vs_open']
        out['verdict'] = 'hit' if cv >= 0 else 'miss' if cv <= -3 else 'flat'
    else:
        out['verdict'] = 'unknown'
    return out


# --- 取数 ------------------------------------------------------------------------------

def fetch_quotes(symbols, http=None):
    """live_quote 只支持 sh/sz；其余（如北交所 bj）直接不请求，调用方按 quote_missing 处理。"""
    import live_quote
    supported = [s for s in symbols if re.fullmatch(r'(sh|sz)\d{6}', s or '')]
    if not supported:
        return {}
    snap = live_quote.snapshot(supported)
    return {q['symbol']: q for q in snap.get('quotes', [])}


def fetch_minute_paths(symbols, http=None, now=None):
    """逐只、尽力而为的日内路径一句话。单只失败只是那一只没有路径，不影响其它只或整体结算。"""
    import minute_data
    out = {}
    for symbol in symbols:
        if not re.fullmatch(r'(sh|sz)\d{6}', symbol or ''):
            continue
        try:
            m = minute_data.fetch_minute(symbol, now)
            if not minute_data.is_today(m, now):
                continue
        except Exception:
            continue
        out[symbol] = _describe_path(m)
    return out


def _describe_path(m):
    bars = m.get('bars') or []
    if not bars:
        return None
    open_price = m['open']
    high_bar = max(bars, key=lambda b: b['price'])
    if open_price <= 0:
        return None
    high_pct = round((high_bar['price'] / open_price - 1) * 100, 1)
    close_pct = round((m['last'] / open_price - 1) * 100, 1)
    if abs(high_pct - close_pct) < 0.5:
        return '%s 开盘后走势平稳，收盘较开盘 %+.1f%%' % (high_bar['t'][:2] + ':' + high_bar['t'][2:], close_pct)
    return '%s:%s 冲高至开盘 %+.1f%%，尾盘收于开盘 %+.1f%%' % (
        high_bar['t'][:2], high_bar['t'][2:], high_pct, close_pct)


# --- AI 复盘 ---------------------------------------------------------------------------

SCHEMA = {
    'type': 'object',
    'properties': {
        'market_view': {'type': 'string', 'description': '结合昨天的情绪指标和今天的整体结果，120 字以内概括这批名单整体兑现得怎样'},
        'items': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {
                'symbol': {'type': 'string'},
                'review': {'type': 'string', 'description': '一句话复盘，60 字以内，必须引用今天的具体数值，并对照昨天写的思路/风险是否兑现'},
            },
            'required': ['symbol', 'review'], 'additionalProperties': False}},
    },
    'required': ['market_view', 'items'], 'additionalProperties': False,
}

SYSTEM = """你是一个A股短线情绪复盘助手。用户给你昨天「次日关注」规则筛出的若干只票——包括昨天的打分理由、
风险提示、以及（如果有）AI 当时写的思路和风险——和今天这些票实际走出来的客观数据。

硬性要求：
1. 只能依据输入里的数据。输入**不含任何新闻、公告、研报、业绩、政策**，禁止编造消息面。
2. review 必须引用今天的具体数值（今日涨跌幅、相对开盘的涨跌、是否一字/炸板/跌停等）。
3. **必须明确对照昨天写的思路/风险是否兑现**——写清楚昨天说的条件今天有没有出现，而不是脱离昨天的判断
   单独描述今天的走势。没有昨天 AI 判断的（只有规则理由的）就对照规则理由和风险提示。
4. 不得写"早就说了""不出所料"这类马后炮式的自我表扬或自我辩护；客观陈述兑现与否即可。
5. 不出现任何胜率/概率数字，不给具体价位。
6. items 里每一只都要给判断，symbol 原样照抄。用中文，措辞克制，这是研究参考，不是投资建议。"""


def build_payload(prev_date, sentiment, items):
    stocks = []
    for it in items:
        yesterday = {k: it.get(k) for k in ('score', 'bucket', 'tags', 'reasons', 'risks')}
        if it.get('ai'):
            yesterday['ai_verdict'] = it['ai'].get('verdict')
            yesterday['ai_plan'] = it['ai'].get('plan')
            yesterday['ai_risk'] = it['ai'].get('risk')
        today = {k: it['outcome'].get(k) for k in ('result', 'verdict', 'open_pct', 'high_pct', 'low_pct',
                                                     'close_pct', 'close_vs_open', 'turnover', 'path')}
        stocks.append({'symbol': it['symbol'], 'name': it['name'], 'yesterday': yesterday, 'today': today})
    return {'prev_date': prev_date, 'prev_sentiment': sentiment, 'stocks': stocks}


def apply_ai(items, data):
    sent = {it['symbol'] for it in items}
    by, issues = {}, []
    for r in data.get('items') or []:
        if not isinstance(r, dict) or r.get('symbol') not in sent:
            issues.append('模型返回了未送入的代码，已剔除：%s' % (r.get('symbol') if isinstance(r, dict) else r))
            continue
        by[r['symbol']] = str(r.get('review') or '')
    missing = sorted(sent - set(by))
    if missing:
        issues.append('模型未对以下代码给出复盘：%s' % ','.join(missing))
    for it in items:
        if it['symbol'] in by:
            it['outcome']['ai_review'] = by[it['symbol']]
    return issues, str(data.get('market_view') or '')


def analyze(prev_date, sentiment, items):
    """调用 AI 复盘。失败不抛出：降级成纯规则结算。返回 (market_view, meta)。"""
    import claude_client
    meta = {'prompt_version': PROMPT_VERSION}
    if not items:
        meta.update(status='skipped', error='没有可复盘的候选')
        return '', meta
    user = '以下是昨天的判断和今天的结算数据，请按 schema 给出复盘。\n\n' + json.dumps(
        build_payload(prev_date, sentiment, items), ensure_ascii=False, indent=1)
    try:
        data, call_meta = claude_client.complete_json(SYSTEM, user, SCHEMA,
                                 model=os.environ.get('NEXTDAY_MODEL') or None)
    except claude_client.ClaudeError as exc:
        meta.update(status=exc.status, error=exc.message)
        return '', meta
    issues, market_view = apply_ai(items, data)
    meta.update(call_meta, status='ok', validation_issues=issues)
    return market_view, meta


# --- 汇总统计 --------------------------------------------------------------------------

def summarize(items):
    n = len(items)
    counts = {r: 0 for r in RESULT_LABEL}
    verdicts = {v: 0 for v in VERDICT_LABEL}
    close_vs_open, close_pct = [], []
    for it in items:
        o = it['outcome']
        if o.get('result') in counts:
            counts[o['result']] += 1
        if o.get('verdict') in verdicts:
            verdicts[o['verdict']] += 1
        if o.get('close_vs_open') is not None:
            close_vs_open.append(o['close_vs_open'])
        if o.get('close_pct') is not None:
            close_pct.append(o['close_pct'])
    buyable = n - verdicts.get('unbuyable', 0)
    return {'n': n, 'result_counts': counts, 'verdict_counts': verdicts, 'buyable_n': buyable,
            'avg_close_vs_open': round(sum(close_vs_open) / len(close_vs_open), 2) if close_vs_open else None,
            'avg_close_pct': round(sum(close_pct) / len(close_pct), 2) if close_pct else None}


def stats(directory=None, days=30, rules_version=None):
    """扫最近 days 个交易日文件，按 rules_version 分组聚合已结算的兑现结果。

    命中率只在 hit/miss（已有结论）里算，flat/unknown/unbuyable 各自单独计数，不参与分母。
    样本不足 MIN_N_FOR_RATE 只给计数不给百分比。
    """
    import tushare_sync
    d = Path(directory) if directory else market_review.data_dir()
    if not d.is_dir():
        return {}
    by_version = {}
    files = sorted((p for p in d.glob('*.json') if re.fullmatch(r'\d{8}', p.stem)), reverse=True)[:days]
    for p in files:
        try:
            data = tushare_sync.read(p)
        except (OSError, ValueError, KeyError):
            continue
        nw = data.get('next_day_watch') or {}
        items = nw.get('items') or []
        rv = nw.get('rules_version')
        if not rv:
            continue
        for it in items:
            o = it.get('outcome')
            if not o:
                continue
            g = by_version.setdefault(rv, {'hit': 0, 'miss': 0, 'flat': 0, 'unbuyable': 0, 'unknown': 0, 'n': 0})
            g['n'] += 1
            g[o.get('verdict') or 'unknown'] = g.get(o.get('verdict') or 'unknown', 0) + 1
    out = {}
    for rv, g in by_version.items():
        resolved = g['hit'] + g['miss']
        out[rv] = dict(g, hit_rate_pct=round(g['hit'] / resolved * 100, 1) if resolved >= MIN_N_FOR_RATE else None,
                       resolved_n=resolved)
    return out


# --- 入口 ------------------------------------------------------------------------------

def prior_watch_file(directory, before_trade_date, scan_days=market_review.SCAN_DAYS):
    """最近一个严格早于 before_trade_date、且带有 next_day_watch.items 的落盘文件。"""
    import tushare_sync
    d = Path(directory) if directory else market_review.data_dir()
    if not d.is_dir():
        return None
    files = sorted((p for p in d.glob('*.json') if re.fullmatch(r'\d{8}', p.stem)), reverse=True)
    checked = 0
    for p in files:
        if p.stem >= before_trade_date:
            continue
        checked += 1
        if checked > scan_days:
            break
        try:
            data = tushare_sync.read(p)
        except (OSError, ValueError, KeyError):
            continue
        if (data.get('next_day_watch') or {}).get('items'):
            return data
    return None


def settle(prior, today_pools, ai=False, now=None, http=None):
    """结算 prior（昨天落盘的 review，含 next_day_watch）→ prev_watch 结果 dict。不落盘，纯函数。"""
    nw = prior['next_day_watch']
    items = [dict(it) for it in nw['items']]
    symbols = [it['symbol'] for it in items]
    quotes = fetch_quotes(symbols, http)
    paths = fetch_minute_paths(symbols, http, now)
    for it in items:
        it['outcome'] = build_outcome(it['symbol'], today_pools, quotes.get(it['symbol']), paths.get(it['symbol']))
    market_view, meta = ('', {'status': 'skipped', 'error': 'AI 未启用'})
    if ai:
        market_view, meta = analyze(prior['date'], nw.get('sentiment'), items)
    result = {'date': prior['date'], 'rules_version': nw.get('rules_version'), 'settled_at': (now or datetime.now(CST)).isoformat(),
              'items': [{k: it[k] for k in ('symbol', 'name', 'score', 'bucket', 'tags', 'reasons', 'risks', 'outcome')
                        if k in it} | ({'ai': it['ai']} if it.get('ai') else {}) for it in items],
              'summary': summarize(items), 'ai_market_view': market_view, 'ai_meta': meta,
              'limitations': LIMITATIONS}
    return result


def run(directory=None, ai=False, now=None, http=None):
    """定时任务入口：结算「今天之前最近一份带名单的文件」，写回两处：
    - 那份旧文件自己（items[].outcome 归档，供 stats() 重算）
    - 今天的文件（prev_watch，供页面一次性取到）
    找不到待结算的旧文件（首次上线/长假）就什么也不做，正常返回 None。
    """
    now = now or datetime.now(CST)
    today = market_review.load_latest(directory)
    if today is None:
        raise market_review.ReviewError('还没有落盘的市场复盘数据，先运行 market_review.py')
    prior = prior_watch_file(directory, today['trade_date'])
    if prior is None:
        return None
    result = settle(prior, today.get('pools') or {}, ai=ai, now=now, http=http)
    prior['next_day_watch']['outcome_summary'] = result['summary']
    prior['next_day_watch']['outcome_meta'] = {'settled_at': result['settled_at'], 'ai_meta': result['ai_meta']}
    for it, out_it in zip(prior['next_day_watch']['items'], result['items']):
        it['outcome'] = out_it.get('outcome')
    market_review.save_review(prior, directory)
    today['prev_watch'] = result
    market_review.save_review(today, directory)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ai', action='store_true', help='同时调用大模型复盘（需要已配置 key）')
    p.add_argument('--dir', type=Path)
    a = p.parse_args()
    if a.ai:
        import llm_settings
        llm_settings.apply()
    try:
        r = run(a.dir, ai=a.ai)
    except market_review.ReviewError as exc:
        print('兑现结算失败: %s' % exc)
        return 1
    if r is None:
        print('没有待结算的名单（首次上线或长假），跳过')
        return 0
    s = r['summary']
    print('兑现结算 %s：%d 只 · 接得住 %d · 未接住 %d · 一字买不进 %d · AI %s' % (
        r['date'], s['n'], s['verdict_counts']['hit'], s['verdict_counts']['miss'],
        s['verdict_counts']['unbuyable'], r['ai_meta'].get('status') or '未调用'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
