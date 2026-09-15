"""Checkpointed four-endpoint ingestion, with no trading or predictions."""
import argparse
import hashlib
import html
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tushare_probe import probe


def sha(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def save(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps({'sha256': sha(payload), 'payload': payload}, ensure_ascii=False))
    temp.replace(path)


def read(path):
    env = json.loads(path.read_text())
    if env['sha256'] != sha(env['payload']):
        raise ValueError('Saved data checksum mismatch')
    return env['payload']


def fetch_pages(api, params, fields, key, call):
    rows, seen = [], set()
    for offset in range(0, 20000, 2000):
        page = call(api, {**params, 'limit': 2000, 'offset': offset}, fields)
        if len(page) > 2000:
            raise ValueError(api + ': page limit not honoured')
        for row in page:
            identity = tuple(row[k] for k in key)
            if identity in seen:
                raise ValueError(api + ': duplicate or repeated page')
            seen.add(identity)
        rows.extend(page)
        if len(page) < 2000:
            return rows
    raise ValueError(api + ': pagination limit exceeded')


def check_day(day, prices, factors):
    if not prices or not factors:
        raise ValueError('Empty daily or adjustment result')
    for rows in (prices, factors):
        if len({r['ts_code'] for r in rows}) != len(rows):
            raise ValueError('Duplicate symbol')
        if any(r['trade_date'] != day for r in rows):
            raise ValueError('Unexpected trade date')
    adjustments = {r['ts_code']: Decimal(str(r['adj_factor'])) for r in factors}
    if any(not n.is_finite() or n <= 0 for n in adjustments.values()):
        raise ValueError('Invalid adjustment factor')
    for r in prices:
        if r['ts_code'] not in adjustments:
            raise ValueError('Missing adjustment factor for quoted stock')
        o, h, l, c, prev, vol, amount = [Decimal(str(r[k])) for k in ('open', 'high', 'low', 'close', 'pre_close', 'vol', 'amount')]
        if any(not n.is_finite() for n in (o, h, l, c, prev, vol, amount)) or not 0 < l <= min(o, c) <= max(o, c) <= h or prev <= 0 or min(vol, amount) < 0:
            raise ValueError('Invalid daily OHLCV')


def select_days(sessions, existing, batch):
    # Refresh recent sessions for provider corrections; backfill newest missing first.
    missing = [d for d in reversed(sessions) if d not in existing]
    return list(dict.fromkeys(list(reversed(sessions[-2:])) + missing))[:batch]


def cooldowns(root, now):
    deadlines = {}
    for path in (root / 'runs').glob('*.json'):
        for request in read(path).get('requests', []):
            if request.get('status') != 'rate_limited':
                continue
            message = request.get('message', '')
            delay = timedelta(days=1) if '天' in message else timedelta(hours=1) if '小时' in message else timedelta(minutes=1)
            until = datetime.fromisoformat(request['fetched_at']) + delay
            if until > now:
                deadlines[request['api']] = max(until, deadlines.get(request['api'], until))
    return deadlines


def single_batch(api, params, fields, call):
    rows = call(api, {**params, 'limit': 6000}, fields)
    if len(rows) >= 6000:
        raise ValueError(api + ': reached row ceiling; completeness unknown')
    if not rows or len({r['ts_code'] for r in rows}) != len(rows):
        raise ValueError(api + ': empty or duplicate symbols')
    return rows


def render(r):
    safe = lambda x: html.escape(str(x)).replace('|', '&#124;').replace('\n', ' ')
    lines = ['# Tushare 数据同步', '', f'运行：{r["id"]} · 时间：{r["generated_at"]} · 状态：{r["status"]}', '',
             '仅使用 stock_basic、trade_cal、daily、adj_factor 四个已开放接口。', '',
             f'股票基础信息：{r.get("stock_counts", {})}；沪深在市股票行业字段非空：{r.get("industry_count", 0)}/{r.get("active_hs", 0)}。', '',
             '行业字段为 Tushare 基础信息分类，不等同申万分类；当前上市/退市状态不是历史每日状态，也不代表当前可交易。', '',
             f'日历范围：{r.get("calendar_range", "未取得")}；沪深开市日期一致：{r.get("calendar_agree", False)}。', '',
             f'滚动目标最近60个已过去的沪深交易日：已保存 {r.get("saved_days", "待确定")}/{r.get("target_days", "待确定")} 天，尚缺 {r.get("pending_days", "待确定")} 天。', '',
             '| 本次日期 | 状态 | 日线条数 | 复权因子条数 |', '|---|---|---:|---:|']
    lines += [f'| {x["date"]} | {x["status"]} | {x.get("daily", 0)} | {x.get("adj_factor", 0)} |' for x in r['days']]
    lines += ['', '每日请求全体股票，单次上限6000条，触及上限不认定完整；没有日线的股票不能直接认定停牌。北交所可能随接口返回，但本版调度日历仅核验沪深。基础信息暂取在市L状态，退市和暂停上市档案暂缓。', '',
              '每轮最多处理10个日期，优先最近日期；已完成日期作为检查点，最近2日重取以接受源修订。当前文件可更新，旧版本由Git提交历史保留。', '',
              '日线与复权因子分开原样保存，所有有日线的股票必须有同日因子；未直接生成前复权价格或策略收益。日线量额单位沿用接口（成交量：手，成交额：千元）。', '',
              '尚未开启定时运行、交易或Dashboard数据接入。日期计数表示已通过分页及字段检查，不是与交易所逐股核验后的完整率。', '',
              '## 错误', '']
    lines += ['- ' + safe(e) for e in r['errors']] or ['无。']
    return '\n'.join(lines) + '\n'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--history', type=Path, required=True)
    p.add_argument('--run-id', required=True)
    p.add_argument('--verify-only', action='store_true')
    args = p.parse_args()
    if not re.fullmatch(r'\d+-\d+', args.run_id):
        raise ValueError('Invalid run ID')
    root = args.history / 'tushare_data'
    logpath = root / 'runs' / (args.run_id + '.json')
    if args.verify_only:
        report = read(logpath)
        for relative, expected in report['files'].items():
            if sha(read(root / relative)) != expected:
                raise ValueError('Restored file differs from committed manifest')
        if logpath.with_suffix('.md').read_text() != render(report):
            raise ValueError('Report mismatch')
        print('Restored and verified', len(report['files']), 'files; status:', report['status'])
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            Path(os.environ['GITHUB_STEP_SUMMARY']).write_text(render(report))
        return 0 if report['status'] == 'success' else 1
    if logpath.exists():
        raise ValueError('Run already archived')
    token = os.environ.get('TUSHARE_TOKEN', '').strip()
    if not token:
        raise ValueError('Missing TUSHARE_TOKEN')
    now = datetime.now(timezone(timedelta(hours=8)))
    report = {'id': args.run_id, 'generated_at': now.isoformat(), 'days': [], 'errors': [], 'files': {}, 'requests': []}
    started = time.monotonic()
    deadlines = cooldowns(root, now)
    blocked = set()
    last_calendar_call = None

    def call(api, params, fields):
        nonlocal last_calendar_call
        if api in deadlines:
            raise ValueError(api + ': cooling down until ' + deadlines[api].isoformat())
        if api in blocked:
            raise ValueError(api + ': stopped after unsuccessful response in this run')
        if time.monotonic() - started > 480:
            raise ValueError('Request budget exhausted')
        if api == 'trade_cal' and last_calendar_call is not None:
            remaining = 65 - (time.monotonic() - last_calendar_call)
            if remaining > 0:
                if time.monotonic() - started + remaining > 480:
                    raise ValueError('Calendar pacing exceeds request budget')
                while remaining > 0:
                    time.sleep(min(remaining, 30))
                    remaining = 65 - (time.monotonic() - last_calendar_call)
        if api == 'trade_cal':
            last_calendar_call = time.monotonic()
        response = probe(api, params, fields, token)
        report['requests'].append({k: v for k, v in response.items() if k not in ('items', 'fields')})
        time.sleep(2)
        if response['status'] not in ('success', 'empty'):
            blocked.add(api)
            raise ValueError(api + ': ' + response['status'] + ' ' + response['message'])
        if set(fields.split(',')) - set(response['fields']):
            raise ValueError(api + ': missing requested fields')
        return [dict(zip(response['fields'], row)) for row in response['items']]

    def persist(relative, payload):
        payload = {'fetched_at': now.isoformat(), 'source': 'https://api.tushare.pro', **payload}
        save(root / relative, payload)
        report['files'][relative] = sha(payload)

    try:
        master_path = root / 'stock_basic.json'
        previous = read(master_path) if master_path.exists() else {}
        cached = previous and datetime.fromisoformat(previous['fetched_at']).astimezone(now.tzinfo).date() == now.date()
        stocks = previous['rows'] if cached else single_batch('stock_basic', {'list_status': 'L'},
            'ts_code,symbol,name,industry,market,exchange,list_status,list_date,delist_date', call)
        if any(r['list_status'] != 'L' for r in stocks):
            raise ValueError('Unexpected listing status')
        if len({r['ts_code'] for r in stocks}) != len(stocks) or not any(r['list_status'] == 'L' for r in stocks):
            raise ValueError('Stock master is empty or duplicated')
        if not cached:
            persist('stock_basic.json', {'rows': stocks})
        else:
            report['files']['stock_basic.json'] = sha(previous)
        report['stock_counts'] = dict(Counter(r['list_status'] for r in stocks))
        hs = [r for r in stocks if r['list_status'] == 'L' and r['exchange'] in ('SSE', 'SZSE')]
        report['active_hs'] = len(hs)
        report['industry_count'] = sum(bool(r['industry'] and r['industry'] not in ('-', '--')) for r in hs)
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        report['errors'].append(str(exc).replace(token, '[REDACTED]'))
    try:
        start, end = f'{now.year - 1}0101', f'{now.year}1231'
        calendars = {}
        for exchange in ('SSE', 'SZSE'):
            cache = root / 'calendars' / (exchange + '.json')
            old = read(cache) if cache.exists() else {}
            if old.get('start_date') == start and old.get('end_date') == end:
                rows = old['rows']
            else:
                rows = call('trade_cal', {'exchange': exchange, 'start_date': start, 'end_date': end}, 'exchange,cal_date,is_open,pretrade_date')
            first, last = datetime.strptime(start, '%Y%m%d').date(), datetime.strptime(end, '%Y%m%d').date()
            expected = {(first + timedelta(days=i)).strftime('%Y%m%d') for i in range((last-first).days + 1)}
            if len(rows) != len(expected) or {r['cal_date'] for r in rows} != expected or any(r['exchange'] != exchange or r['is_open'] not in (0, 1, '0', '1') for r in rows):
                raise ValueError('Calendar incomplete or invalid: ' + exchange)
            calendars[exchange] = rows
            if rows is old.get('rows'):
                report['files']['calendars/' + exchange + '.json'] = sha(old)
            else:
                persist('calendars/' + exchange + '.json', {'rows': rows, 'start_date': start, 'end_date': end})
        opens = {e: {r['cal_date'] for r in rows if str(r['is_open']) == '1'} for e, rows in calendars.items()}
        report['calendar_agree'] = opens['SSE'] == opens['SZSE']
        if not report['calendar_agree']:
            raise ValueError('SSE and SZSE sessions differ; requires exchange-specific scheduling')
        persist('trade_cal.json', {'calendars': calendars, 'start_date': start, 'end_date': end})
        report['calendar_range'] = start + '—' + end
        sessions = sorted(d for d in opens['SSE'] if d < now.strftime('%Y%m%d'))[-60:]
        existing = set()
        for path in (root / 'days').glob('*.json'):
            old = read(path)
            check_day(path.stem, old['daily'], old['adj_factor'])
            existing.add(path.stem)
        for day in select_days(sessions, existing, 10):
            try:
                prices = single_batch('daily', {'trade_date': day}, 'ts_code,trade_date,open,high,low,close,pre_close,vol,amount', call)
                persist('raw/daily/' + day + '.json', {'trade_date': day, 'rows': prices})
                factors = single_batch('adj_factor', {'trade_date': day}, 'ts_code,trade_date,adj_factor', call)
                persist('raw/adj_factor/' + day + '.json', {'trade_date': day, 'rows': factors})
                check_day(day, prices, factors)
                persist('days/' + day + '.json', {'trade_date': day, 'daily': prices, 'adj_factor': factors})
                existing.add(day)
                report['days'].append({'date': day, 'status': 'success', 'daily': len(prices), 'adj_factor': len(factors)})
                print(day, len(prices), 'daily rows saved', flush=True)
            except ValueError as exc:
                report['days'].append({'date': day, 'status': 'failed'})
                report['errors'].append(day + ': ' + str(exc))
                break  # Preserve checkpoint, do not hammer a quota-limited endpoint.
        report.update(target_days=len(sessions), saved_days=len(set(sessions) & existing), pending_days=len(set(sessions) - existing))
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        report['errors'].append(str(exc).replace(token, '[REDACTED]'))
    report['status'] = 'success' if not report['errors'] else 'partial'
    save(logpath, report)
    logpath.with_suffix('.md').write_text(render(report))
    (root / 'README.md').write_text(render(report) + '\n[全部同步记录](runs/)\n')
    print('Sync archived:', report['status'])
    return 0  # Verification job reports failures after results have been committed.


if __name__ == '__main__':
    raise SystemExit(main())
