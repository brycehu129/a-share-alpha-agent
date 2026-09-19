"""大盘环境：指数、外围市场、全市场宽度、行业联动。只用标准库。

宽度数据**不重新抓全市场行情**——15:35 的日线流程里 report_pipeline 已经通过
universe_data.collect_universe() 抓过一遍全部沪深A股并带校验和归档到 records/，
这里直接读那份已核验的归档。这样盘后分析零额外请求就拿到真实的全市场涨跌家数，
而不是拿几十只样本去冒充全市场宽度。

代价是宽度的时间戳是那次归档的时间，不是此刻——所以输出里带 `source_generated_at`
和覆盖率，调用方必须把它当"那个时点的全市场快照"，不能说成实时宽度。
"""
import argparse
import json
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import live_quote
from collect_quotes import CST
from dashboard_export import latest

CN_INDICES = ['sh000001', 'sz399001', 'sz399006', 'sh000300', 'sh000905', 'sh000852']
OVERSEAS = ['hkHSI', 'hkHSTECH', 'usDJI', 'usIXIC', 'usINX']

INDEX_LABEL = {'sh000001': '上证指数', 'sz399001': '深证成指', 'sz399006': '创业板指',
               'sh000300': '沪深300', 'sh000905': '中证500', 'sh000852': '中证1000',
               'hkHSI': '恒生指数', 'hkHSTECH': '恒生科技', 'usDJI': '道琼斯',
               'usIXIC': '纳斯达克', 'usINX': '标普500'}

# 涨跌停只能按涨跌幅近似判断：universe 归档里没有逐股涨跌停价。
# 创业板(sz30)/科创板(sh688)/北交所是 20%，其余主板 10%；ST 股 5% 这里识别不出来，
# 会被算进普通涨幅，所以这两个数字明确标为"近似"。
def _limit_threshold(symbol):
    return 19.5 if symbol.startswith(('sz30', 'sh688')) else 9.8


def breadth_from_universe(history):
    """从最近一次已校验的全市场归档算涨跌家数。"""
    path, record = latest(history, 'records', compact=True)
    if record is None:
        return {'status': 'missing', 'note': '尚无全市场行情归档，无法给出市场宽度'}
    universe = record.get('universe') or {}
    quotes = universe.get('quotes') or []
    if not quotes:
        return {'status': 'empty', 'source_generated_at': record.get('generated_at'),
                'note': '最近一次归档没有有效的全市场报价'}
    dates = Counter(q['quote_at'][:10] for q in quotes)
    up = down = flat = limit_up = limit_down = 0
    invalid = 0
    for q in quotes:
        try:
            change = (Decimal(str(q['last'])) / Decimal(str(q['previous_close'])) - 1) * 100
        except (InvalidOperation, ZeroDivisionError, KeyError, TypeError):
            invalid += 1
            continue
        threshold = _limit_threshold(q['symbol'])
        if change > 0:
            up += 1
            if change >= threshold:
                limit_up += 1
        elif change < 0:
            down += 1
            if change <= -threshold:
                limit_down += 1
        else:
            flat += 1
    total = up + down + flat
    return {
        'status': 'ready' if len(dates) == 1 else 'mixed_dates',
        'source_id': path.stem, 'source_generated_at': record.get('generated_at'),
        'quote_dates': dict(dates), 'counted': total, 'invalid': invalid,
        'listed': len(universe.get('members', [])), 'coverage_pct': universe.get('coverage_pct'),
        'up': up, 'down': down, 'flat': flat,
        'up_pct': round(up / total * 100, 2) if total else None,
        'limit_up_approx': limit_up, 'limit_down_approx': limit_down,
        'note': '涨跌停家数按涨跌幅近似判定（主板≥9.8%、创业板/科创板≥19.5%），'
                'ST股5%的限制无法识别，因此是近似值而非交易所口径；'
                '数据时点是该次归档的时间，不是此刻的实时宽度。',
    }


def industry_moves(check_rows, min_members=2):
    """按行业汇总今日涨跌幅中位数。只覆盖本次复核到的股票（候选池+持仓+自选），
    是**样本内**的行业表现，不是全行业统计。"""
    buckets = {}
    for row in check_rows:
        industry = ((row.get('strategy') or {}).get('industry'))
        change = row['quote'].get('change_pct')
        if not industry or change is None:
            continue
        buckets.setdefault(industry, []).append(float(change))
    out = []
    for industry, values in buckets.items():
        if len(values) < min_members:
            continue
        ordered = sorted(values)
        mid = len(ordered) // 2
        median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
        out.append({'industry': industry, 'members': len(values),
                    'median_change_pct': round(median, 2),
                    'positive_pct': round(sum(v > 0 for v in values) / len(values) * 100, 1)})
    out.sort(key=lambda r: -r['median_change_pct'])
    return {'rows': out, 'scope': '仅统计本次复核的股票（候选池+持仓+自选），'
                                 '不是全行业成分统计，且每个行业至少%d只才列出' % min_members}


def collect(history, check_rows=None, snapshot=None):
    """snapshot 传入已取好的指数/外围快照可避免重复请求；不传则现取。"""
    snapshot = snapshot or live_quote.snapshot(CN_INDICES + OVERSEAS)
    by_symbol = {q['symbol']: q for q in snapshot['quotes']}

    def pick(symbols):
        rows = []
        for s in symbols:
            q = by_symbol.get(s)
            if not q:
                continue
            rows.append({'symbol': s, 'label': INDEX_LABEL.get(s, q['name']),
                         'last': q['last'], 'change_pct': q['change_pct'],
                         'quote_date': q['quote_date'], 'quote_at': q['quote_at'],
                         'timezone_verified': q['timezone_verified'],
                         'amount_wan': q.get('amount_wan')})
        return rows

    return {
        'generated_at': datetime.now(CST).isoformat(),
        'session': snapshot['session'],
        'session_label': live_quote.SESSION_LABEL.get(snapshot['session'], snapshot['session']),
        'indices': pick(CN_INDICES),
        'overseas': pick(OVERSEAS),
        'breadth': breadth_from_universe(history),
        'industries': industry_moves(check_rows or []),
        'quote_failures': snapshot['failures'],
        'caveats': [
            '美股时间戳的时区腾讯未明确说明，本系统不据此计算延迟，'
            '外围数据只用于判断隔夜方向，不作精确时点对齐。',
            '指数行情与全市场宽度来自不同时点（宽度来自已归档的全市场采集），不是同一截面。',
        ],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--history', type=Path, required=True)
    p.add_argument('--json', action='store_true')
    a = p.parse_args()
    data = collect(a.history)
    if a.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print('%s · %s' % (data['generated_at'], data['session_label']))
    print('\n-- A股指数 --')
    for r in data['indices']:
        print('  %-8s %-8s %12s  %7s%%  (%s)' % (r['symbol'], r['label'], r['last'], r['change_pct'], r['quote_date']))
    print('\n-- 外围 --')
    for r in data['overseas']:
        tz = '' if r['timezone_verified'] else ' [时区未核验]'
        print('  %-9s %-8s %12s  %7s%%  (%s)%s' % (r['symbol'], r['label'], r['last'], r['change_pct'], r['quote_date'], tz))
    b = data['breadth']
    print('\n-- 全市场宽度 (%s) --' % b.get('status'))
    if b.get('counted'):
        print('  上涨 %d / 下跌 %d / 平 %d  上涨占比 %s%%  近似涨停 %d 跌停 %d' % (
            b['up'], b['down'], b['flat'], b['up_pct'], b['limit_up_approx'], b['limit_down_approx']))
        print('  来源 %s · 报价日期 %s · 覆盖 %s%%' % (
            b.get('source_generated_at'), list(b.get('quote_dates', {})), b.get('coverage_pct')))
    else:
        print('  ' + b.get('note', ''))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
