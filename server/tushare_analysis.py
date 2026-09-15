"""Offline descriptive factors from verified checkpoints; never requests market APIs."""
import argparse
import html
import re
from collections import Counter
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from tushare_sync import read, save, sha, check_day


def calculate(prices, adjustments, benchmark=None):
    if len(prices) != 21 or len(adjustments) != 21:
        raise ValueError('21 aligned sessions required')
    adjusted = [Decimal(str(p)) * Decimal(str(a)) for p, a in zip(prices, adjustments)]
    if any(not x.is_finite() or x <= 0 for x in adjusted):
        raise ValueError('Invalid adjusted close')
    result = (adjusted[-1] / adjusted[0] - 1) * 100
    ma = sum(adjusted[-20:]) / 20
    fmt = lambda n: str(n.quantize(Decimal('0.01')))
    out = {'return20_pct': fmt(result), 'ma20_deviation_pct': fmt((adjusted[-1] / ma - 1) * 100), 'excess20_pp': None}
    if benchmark is not None:
        if len(benchmark) != 21:
            raise ValueError('Benchmark dates must align')
        b = [Decimal(str(x)) for x in benchmark]
        if any(not x.is_finite() or x <= 0 for x in b):
            raise ValueError('Invalid benchmark close')
        br = (b[-1] / b[0] - 1) * 100
        out.update(benchmark20_pct=fmt(br), excess20_pp=fmt(result-br))
    return out


def build(history, now):
    root = history / 'tushare_data'
    r = {'status': 'waiting_data', 'generated_at': now.isoformat(), 'rankings': [], 'issues': [], 'sources': {}, 'excluded': {}}
    def source(path):
        payload = read(path)
        r['sources'][str(path.relative_to(history))] = sha(payload)
        return payload
    calendar_path = root / 'trade_cal.json'
    if not calendar_path.exists():
        r['issues'].append('等待完整沪深交易日历；不以工作日替代交易日。')
        return r
    cal = source(calendar_path)['calendars']
    opens = {e: sorted(x['cal_date'] for x in cal[e] if str(x['is_open']) == '1' and x['cal_date'] < now.strftime('%Y%m%d')) for e in ('SSE', 'SZSE')}
    if opens['SSE'] != opens['SZSE'] or len(opens['SSE']) < 21:
        r['issues'].append('沪深日历不一致或不足21个日期。')
        return r
    dates = opens['SSE'][-21:]
    r.update(start_date=dates[0], end_date=dates[-1])
    missing = [d for d in dates if not (root / 'days' / (d + '.json')).exists()]
    r['missing_dates'] = missing
    if missing:
        r['issues'].append(f'目标窗口缺少{len(missing)}个完整日线/因子检查点，暂不排名。')
        return r
    master = root / 'stock_basic.json'
    if not master.exists():
        r['issues'].append('等待股票基础信息，暂不输出无名称和状态核验的排名。')
        return r
    stocks = source(master)['rows']
    prices, adjustments = [], []
    for day in dates:
        data = source(root / 'days' / (day + '.json'))
        check_day(day, data['daily'], data['adj_factor'])
        prices.append({x['ts_code']: x['close'] for x in data['daily']})
        adjustments.append({x['ts_code']: x['adj_factor'] for x in data['adj_factor']})
    benchmark = None
    for path in sorted((history / 'research').glob('*.json'), reverse=True):
        data = read(path)
        for item in data.get('daily', []):
            if item.get('symbol') == 'sh000300' and item.get('status') == 'success' and item.get('adjustment') == 'none':
                bars = {b['date'].replace('-', ''): b['close'] for b in item['bars']}
                if all(d in bars for d in dates):
                    benchmark = [bars[d] for d in dates]
                    source(path)
                    break
        if benchmark is not None:
            break
    if benchmark is None:
        r['issues'].append('缺少同一21交易日窗口的沪深300，超额收益留空。')
    excluded = Counter()
    for stock in stocks:
        code = stock['ts_code']
        if stock['list_status'] != 'L' or stock['exchange'] not in ('SSE', 'SZSE'):
            excluded['非沪深在市'] += 1
            continue
        if 'ST' in stock['name'].upper() or '退' in stock['name']:
            excluded['名称风险标记'] += 1
            continue
        if any(code not in p or code not in a for p, a in zip(prices, adjustments)):
            excluded['窗口缺数'] += 1
            continue
        r['rankings'].append({'code': code, 'name': stock['name'], **calculate([p[code] for p in prices], [a[code] for a in adjustments], benchmark)})
    r['rankings'].sort(key=lambda x: (-Decimal(x['return20_pct']), x['code']))
    r['excluded'] = dict(excluded)
    r['status'] = ('ready' if benchmark is not None else 'partial') if r['rankings'] else 'waiting_data'
    return r


def render(r):
    esc = lambda v: html.escape(str(v)).replace('|', '&#124;').replace('\n', ' ')
    lines = ['# Tushare 20日研究报告', '', f'状态：{r["status"]} · 生成时间：{r["generated_at"]}', '',
        f'窗口：{r.get("start_date", "待确定")} — {r.get("end_date", "待确定")}；有效股票：{len(r["rankings"])}', '',
        '只读取已保存数据，本任务不请求行情接口，不消耗Tushare调用额度。', '',
        '20日收益使用21个交易日收盘价×同日复权因子的比值；MA20也使用相同调整序列。超额为相对沪深300价格指数的百分点差，非风险调整Alpha或严格总回报比较。', '',
        '以下为历史涨幅排序前30名，不代表预测、胜率或买入建议。采用当前股票名单，存在幸存者偏差；未完成停牌、涨跌停及交易资格核验。', '',
        '| 股票 | 代码 | 20日收益% | 超额百分点 | MA20偏离% |', '|---|---|---:|---:|---:|']
    for x in r['rankings'][:30]:
        lines.append('| ' + ' | '.join(esc(x[k] if x[k] is not None else '待补充') for k in ('name','code','return20_pct','excess20_pp','ma20_deviation_pct')) + ' |')
    lines += ['', '排除统计：' + esc(r['excluded']), '', '数据状态：']
    lines += ['- ' + esc(i) for i in r['issues']] or ['- 窗口数据检查通过。']
    if r.get('missing_dates'):
        lines += ['', '缺失日期：' + '、'.join(r['missing_dates'])]
    return '\n'.join(lines) + '\n'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--history', type=Path, required=True)
    p.add_argument('--run-id', required=True)
    p.add_argument('--verify-only', action='store_true')
    args = p.parse_args()
    if not re.fullmatch(r'\d+-\d+', args.run_id):
        raise ValueError('Invalid run ID')
    dest = args.history / 'tushare_analysis' / (args.run_id + '.json')
    if args.verify_only:
        report = read(dest)
        for path, expected in report['sources'].items():
            if sha(read(args.history / path)) != expected:
                raise ValueError('Source checksum mismatch')
        regenerated = build(args.history, datetime.fromisoformat(report['generated_at']))
        if regenerated != report or render(report) != dest.with_suffix('.md').read_text():
            raise ValueError('Report cannot be reproduced')
        print('Verified report; data status:', report['status'])
    else:
        if dest.exists():
            raise ValueError('Report already exists')
        report = build(args.history, datetime.now(timezone(timedelta(hours=8))))
        save(dest, report)
        dest.with_suffix('.md').write_text(render(report))
        (dest.parent / 'README.md').write_text(render(report))
        print('Offline report saved; data status:', report['status'])


if __name__ == '__main__':
    main()
