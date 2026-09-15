"""Auditable industry breadth and fixed-sample historical relative strength."""
import argparse
import hashlib
import html
import json
import math
import os
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

from collect_quotes import CST
from daily_data import request, parse_daily
from report_pipeline import read_history, digest
from universe_data import BASE


def industries(raw):
    text = raw.decode('gb18030')
    start, end = text.find('{'), text.rfind('}')
    obj = json.loads(text[start:end + 1])
    result = []
    for key, value in obj.items():
        fields = value.split(',')
        if not re.fullmatch(r'new_[a-z0-9]+', key) or len(fields) < 3:
            raise ValueError('invalid industry definition')
        count = int(fields[2])
        if not 0 <= count <= 2000:
            raise ValueError('invalid industry count')
        result.append({'node': key, 'name': fields[1], 'expected': count})
    if not 1 <= len(result) <= 150:
        raise ValueError('invalid industry list size')
    return result


def factors(stock, benchmark, cutoff):
    s = [b for b in stock if b['date'] <= cutoff]
    b = [b for b in benchmark if b['date'] <= cutoff]
    if len(s) < 21 or len(b) < 21 or s[-1]['date'] != cutoff or b[-1]['date'] != cutoff:
        raise ValueError('不足21条或截止日期不一致')
    if [x['date'] for x in s[-21:]] != [x['date'] for x in b[-21:]]:
        raise ValueError('20日窗口交易日期不齐，可能停牌或缺数')
    closes = [Decimal(x['close']) for x in s]
    bench = [Decimal(x['close']) for x in b]
    ret = (closes[-1] / closes[-21] - 1) * 100
    br = (bench[-1] / bench[-21] - 1) * 100
    ma = sum(closes[-20:]) / 20
    rounded = lambda x: str(x.quantize(Decimal('0.01')))
    return {'end_date': cutoff, 'start_date': s[-21]['date'], 'return20_pct': rounded(ret),
            'benchmark20_pct': rounded(br), 'excess20_pp': rounded(ret - br),
            'ma20_deviation_pct': rounded((closes[-1] / ma - 1) * 100)}


def build(source):
    now = datetime.now(CST)
    data = {'id': '', 'version': 'research-0.5', 'generated_at': now.isoformat(),
            'source_id': source['id'], 'source_generated_at': source['generated_at'],
            'requests': [], 'errors': [], 'industries': [], 'daily': [], 'rankings': []}
    started = time.monotonic()

    def get(url):
        if time.monotonic() - started > 360:
            raise TimeoutError('research request budget exceeded')
        raw = request(url)
        data['requests'].append({'url': url, 'fetched_at': datetime.now(CST).isoformat(),
                                 'sha256': hashlib.sha256(raw).hexdigest()})
        time.sleep(0.25)
        return raw

    def sector(item):
        item = dict(item)
        try:
            symbols = []
            for page in range(1, math.ceil(item['expected'] / 80) + 1):
                url = BASE + 'Market_Center.getHQNodeData?' + urlencode({
                    'page': page, 'num': 80, 'sort': 'symbol', 'asc': 1, 'node': item['node']})
                rows = json.loads(get(url))
                if not isinstance(rows, list):
                    raise ValueError('invalid industry page')
                symbols.extend(r['symbol'] for r in rows)
            if len(symbols) != item['expected'] or len(set(symbols)) != len(symbols):
                raise ValueError('行业成员条数不符或重复')
            if any(not re.fullmatch(r'(sh|sz|bj)\d{6}', s) for s in symbols):
                raise ValueError('行业成员代码异常')
            item.update(symbols=symbols, status='success')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            item.update(symbols=[], status='failed', error=str(exc))
        return item

    try:
        definitions = industries(get('https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php'))
        with ThreadPoolExecutor(max_workers=3) as pool:
            data['industries'] = list(pool.map(sector, definitions))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        data['errors'].append('行业分类: ' + str(exc))
    print('Industry requests completed', flush=True)
    members = {m['symbol']: m for m in source['universe']['members']}
    membership = {}
    for item in data['industries']:
        for symbol in item['symbols']:
            if symbol in members:
                membership.setdefault(symbol, []).append(item['node'])
    unique = {s: nodes[0] for s, nodes in membership.items() if len(nodes) == 1}
    data['mapping'] = {'universe_count': len(members), 'mapped_count': len(unique),
                       'conflicts': [s for s, nodes in membership.items() if len(nodes) > 1],
                       'unmapped': sorted(set(members) - unique.keys()), 'symbols': unique}
    quotes = {q['symbol']: q for q in source['universe']['quotes']}
    quote_dates = {q['quote_at'][:10] for q in quotes.values()}
    data['industry_quote_date'] = next(iter(quote_dates)) if len(quote_dates) == 1 else None
    data['industry_ranking'] = []
    if len(quote_dates) == 1:
        for item in data['industries']:
            eligible = [s for s in item['symbols'] if unique.get(s) == item['node']]
            values = [float(quotes[s]['change_pct']) for s in eligible if s in quotes]
            coverage = len(values) / len(eligible) if eligible else 0
            if len(values) >= 5 and coverage >= 0.9:
                data['industry_ranking'].append({'name': item['name'], 'node': item['node'],
                    'count': len(values), 'eligible': len(eligible), 'source_members': item['expected'],
                    'coverage_pct': round(coverage * 100, 2),
                    'median_change_pct': round(statistics.median(values), 2),
                    'up_pct': round(sum(v > 0 for v in values) * 100 / len(values), 2)})
        data['industry_ranking'].sort(key=lambda r: (-r['median_change_pct'], r['node']))
    # Stable code hash sample is reproducible, but not representative or a recommendation.
    selected = sorted(members, key=lambda s: hashlib.sha256(s.encode()).hexdigest())[:120]
    data['sample'] = selected

    def daily(symbol):
        adjustment = 'none' if symbol == 'sh000300' else 'qfq'
        url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?' + urlencode({
            'param': f'{symbol},day,,{now.date().isoformat()},100,' + ('qfq' if adjustment == 'qfq' else '')})
        item = {'symbol': symbol, 'adjustment': adjustment, 'source_url': url}
        try:
            raw = get(url)
            item.update(bars=parse_daily(raw, symbol, adjustment, now.date()), status='success')
        except (OSError, ValueError, KeyError, TypeError, IndexError, ArithmeticError) as exc:
            item.update(bars=[], status='failed', error=str(exc))
        return item

    with ThreadPoolExecutor(max_workers=3) as pool:
        data['daily'] = list(pool.map(daily, ['sh000300'] + selected))
    print('Daily requests completed', flush=True)
    benchmark = data['daily'][0]['bars']
    successful = [s for s in data['daily'][1:] if s['bars']]
    # Use one shared sample date, then reject laggards instead of mixing dates.
    cutoff = max((s['bars'][-1]['date'] for s in successful), default=None)
    data['factor_cutoff'] = cutoff
    for item in data['daily'][1:]:
        try:
            item['factors'] = factors(item['bars'], benchmark, cutoff)
            data['rankings'].append({'symbol': item['symbol'], 'name': members[item['symbol']]['name'],
                                     **item['factors']})
        except (ValueError, TypeError, ArithmeticError) as exc:
            item['factor_error'] = str(exc)
    data['rankings'].sort(key=lambda r: (-Decimal(r['excess20_pp']), r['symbol']))
    data['status'] = 'success' if data['industry_ranking'] and len(data['rankings']) >= 108 else 'partial'
    data['finished_at'] = datetime.now(CST).isoformat()
    return data


def render(d):
    safe = lambda s: html.escape(str(s)).replace('|', '&#124;').replace('\n', ' ')
    m = d['mapping']
    lines = ['# 行业与样本相对强度 · 研究验证', '',
             f'报告 {d["id"]} · 状态 {d["status"]} · 生成时间 {d["generated_at"]}', '',
             f'行情来源：历史报告 {d["source_id"]}，采集于 {d["source_generated_at"]}；本次没有重新采集全市场报价。', '',
             f'行业分类来源：新浪行业，当前映射 {m["mapped_count"]}/{m["universe_count"]} 只；未映射或冲突 {len(m["unmapped"])} 只。不是申万分类，也不是历史成分库。', '',
             '## 行业内单日表现', '',
             f'报价日期：{d["industry_quote_date"]}。按已映射、同日期股票涨跌幅中位数排序；至少5只有效报价，组内覆盖率至少90%。', '',
             '| 行业 | 有效/纳入/源成员 | 涨跌幅中位数 | 上涨占比 |', '|---|---:|---:|---:|']
    for r in d['industry_ranking'][:15]:
        lines.append(f'| {safe(r["name"])} | {r["count"]}/{r["eligible"]}/{r["source_members"]} | {r["median_change_pct"]}% | {r["up_pct"]}% |')
    lines += ['', '这是单日价格表现，不是已验证的 Money Effect Score。未映射股票不参与计算，当前行业成分可能不完整或陈旧。', '',
              '## 120只固定样本内的20日相对强度', '',
              f'按代码SHA-256排序取前120只，不按收益挑样本；有效因子 {len(d["rankings"])}/{len(d["sample"])}。统一截止日：{d["factor_cutoff"]}。', '',
              '20日收益=(末日收盘/20个交易间隔前收盘−1)×100%；超额=股票收益−沪深300同期价格指数收益，单位为百分点。两者21个日期必须完全对齐。', '',
              '| 股票 | 代码 | 20日收益 | 相对沪深300 | 偏离20日均线 |', '|---|---|---:|---:|---:|']
    for r in d['rankings'][:15]:
        lines.append(f'| {safe(r["name"])} | {r["symbol"]} | {r["return20_pct"]}% | {r["excess20_pp"]}个百分点 | {r["ma20_deviation_pct"]}% |')
    lines += ['', '## 边界与缺失', '',
              '- 这是样本内历史描述，不是全市场龙头排名、胜率预测或买入建议。',
              '- 个股使用本次获取的前复权日线；基准为价格指数，收益口径并非严格总回报对齐，也未扣交易成本。',
              '- 前复权历史可能随除权变化；当前名单存在幸存者偏差，不用于宣布策略Alpha。',
              '- 未过滤ST、停牌、退市或无法成交状态；缺失日期拒绝计算，不补价。',
              '- 行业快照与日线可能截止于不同日期，上文分别列出；不将其拼成交易信号。',
              '- 全量映射、因子、日线、失败原因及请求摘要保存在同编号JSON。', '']
    lines += ['- ' + safe(e) for e in d['errors']]
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--history', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'\d+-\d+', args.run_id):
        raise ValueError('invalid run ID')
    root = args.history / 'research'
    root.mkdir(exist_ok=True)
    path = root / (args.run_id + '.json')
    mdpath = path.with_suffix('.md')
    if args.verify_only:
        envelope = json.loads(path.read_text())
        d = envelope['payload']
        if envelope['sha256'] != digest(d) or mdpath.read_text() != render(d):
            raise ValueError('research integrity mismatch')
        print(f'Restored research {d["id"]}: {len(d["rankings"])} factors; status={d["status"]}')
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            Path(os.environ['GITHUB_STEP_SUMMARY']).write_text(render(d))
        return 0 if d['status'] == 'success' else 1
    if path.exists() or mdpath.exists():
        raise ValueError('cannot overwrite research history')
    sources = [r for r in read_history(args.history) if r.get('universe', {}).get('list_verified')]
    if not sources:
        raise ValueError('no validated universe snapshot')
    source = sources[-1]
    d = build(source)
    d['id'] = args.run_id
    with path.open('x') as f:
        json.dump({'sha256': digest(d), 'payload': d}, f, ensure_ascii=False, indent=2)
    with mdpath.open('x') as f:
        f.write(render(d))
    index = ['# 行业与样本研究报告', '', '独立于市场快照保存；均为研究验证，不是交易建议。', '']
    index += [f'- [{p.stem}]({p.name})' for p in sorted(root.glob('*.md'), reverse=True) if p.name != 'README.md']
    (root / 'README.md').write_text('\n'.join(index) + '\n')
    print(f'Saved research: {d["status"]}, mapped={d["mapping"]["mapped_count"]}, factors={len(d["rankings"])}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
