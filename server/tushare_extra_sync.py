"""Optional Tushare endpoints beyond the core four: hm_detail, limit_list_d, cyq_chips.

Additive to tushare_sync.py: reuses its checkpoint/report primitives
(save/read/sha/bounded_probe/fetch_pages/cooldowns) and its validation
discipline, but lives in a separate module so the already-verified core
sync (stock_basic/trade_cal/daily/adj_factor) is never touched by this
change. Only runs against trading days the core sync has already
validated under tushare_data/days/ -- this module never invents its own
notion of which dates are trading sessions.

hm_detail (10000+ points) and limit_list_d (5000+ points) are fetched
full-market, one trading day at a time, mirroring the daily/adj_factor
pattern. cyq_chips (5000+ points) is fetched only for an explicit list of
stock codes -- a full-market single-day pull would return far more rows
per date than any single request can return, so this module does not
claim full-market chip-distribution coverage.
"""
import argparse
import html
import os
import re
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

from tushare_sync import sha, save, read, bounded_probe, fetch_pages, cooldowns
from tushare_probe import probe, selected_source


def capped_batch(api, params, fields, call, cap):
    """Like tushare_sync.single_batch but with an endpoint-specific row cap."""
    rows = call(api, {**params, 'limit': cap}, fields)
    if len(rows) >= cap:
        raise ValueError(api + ': reached row ceiling; completeness unknown')
    return rows


def validate_hm_detail(day, rows):
    seen = set()
    for r in rows:
        if r['trade_date'] != day:
            raise ValueError('hm_detail: unexpected trade date')
        identity = (r['ts_code'], r['hm_name'])
        if identity in seen:
            raise ValueError('hm_detail: duplicate stock/hot-money pair')
        seen.add(identity)
        try:
            buy, sell, net = (Decimal(str(r[k])) for k in ('buy_amount', 'sell_amount', 'net_amount'))
        except (ArithmeticError, ValueError, KeyError) as exc:
            raise ValueError('hm_detail: invalid numeric field') from exc
        if not all(n.is_finite() for n in (buy, sell, net)) or buy < 0 or sell < 0:
            raise ValueError('hm_detail: invalid amount')
        if abs((buy - sell) - net) > Decimal('0.02'):
            raise ValueError('hm_detail: net_amount does not match buy minus sell')
    return rows


def validate_limit_list(day, rows):
    seen = set()
    for r in rows:
        if r['trade_date'] != day:
            raise ValueError('limit_list_d: unexpected trade date')
        if r['ts_code'] in seen:
            raise ValueError('limit_list_d: duplicate stock')
        seen.add(r['ts_code'])
        if r['limit'] not in ('U', 'D', 'Z'):
            raise ValueError('limit_list_d: unexpected limit type ' + str(r['limit']))
        try:
            close, pct, limit_amount, fd_amount = (
                Decimal(str(r[k])) for k in ('close', 'pct_chg', 'limit_amount', 'fd_amount'))
        except (ArithmeticError, ValueError, KeyError) as exc:
            raise ValueError('limit_list_d: invalid numeric field') from exc
        if not all(n.is_finite() for n in (close, pct, limit_amount, fd_amount)) or close <= 0:
            raise ValueError('limit_list_d: invalid numeric field')
        try:
            open_times, limit_times = int(r['open_times']), int(r['limit_times'])
        except (TypeError, ValueError) as exc:
            raise ValueError('limit_list_d: invalid open/limit times') from exc
        if open_times < 0 or limit_times < 1:
            raise ValueError('limit_list_d: invalid open/limit times')
    return rows


def validate_cyq_chips(day, code, rows):
    total = Decimal('0')
    for r in rows:
        if r['trade_date'] != day or r['ts_code'] != code:
            raise ValueError('cyq_chips: unexpected date or symbol in response')
        try:
            price, percent = Decimal(str(r['price'])), Decimal(str(r['percent']))
        except (ArithmeticError, ValueError, KeyError) as exc:
            raise ValueError('cyq_chips: invalid numeric field') from exc
        if not price.is_finite() or price <= 0 or not percent.is_finite() or percent < 0:
            raise ValueError('cyq_chips: invalid price or percent')
        total += percent
    if rows and abs(total - Decimal('100')) > Decimal('1'):
        raise ValueError('cyq_chips: bucket percentages do not sum to ~100')
    return rows


def render(r):
    safe = lambda x: html.escape(str(x)).replace('|', '&#124;').replace('\n', ' ')
    lines = ['# Tushare 扩展数据同步（游资 / 涨跌停 / 筹码分布）', '',
             f'运行：{r["id"]} · 时间：{r["generated_at"]} · 状态：{r["status"]}', '',
             '本模块新增三个接口：hm_detail（游资每日明细，10000积分门槛）、'
             'limit_list_d（涨跌停列表，5000积分门槛）、cyq_chips（每日筹码分布，5000积分门槛）。'
             '仅在核心同步（stock_basic/trade_cal/daily/adj_factor）已校验通过的交易日上补数，'
             '不引入独立的交易日历口径。', '',
             '| 日期 | 游资明细 | 涨跌停 | 状态 |', '|---|---:|---:|---|']
    lines += [f'| {x["date"]} | {x.get("hm_detail", "—")} | {x.get("limit_list_d", "—")} | {x["status"]} |'
              for x in r['days']]
    lines += ['', '筹码分布仅按运行参数中显式列出的股票代码抓取，不做全市场覆盖——'
              '单个交易日全市场的分箱行数会远超单次请求上限，因此本模块不声称覆盖全市场筹码分布。', '',
              '| 代码 | 日期 | 筹码分布档位数 | 状态 |', '|---|---|---:|---|']
    lines += [f'| {x["code"]} | {x["date"]} | {x.get("cyq_chips", "—")} | {x["status"]} |' for x in r['cyq']]
    lines += ['', '## 错误', '']
    lines += ['- ' + safe(e) for e in r['errors']] or ['无。']
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--history', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--batch-days', type=int, default=10, choices=range(1, 61))
    parser.add_argument('--budget-seconds', type=int, default=480, choices=range(60, 1801))
    parser.add_argument('--cyq-symbols', default='', help='Comma-separated ts_code list for chip-distribution sync')
    parser.add_argument('--cyq-days', type=int, default=5, choices=range(1, 61))
    args = parser.parse_args()
    if not re.fullmatch(r'\d+-\d+', args.run_id):
        raise ValueError('Invalid run ID')
    root = args.history / 'tushare_data'
    logpath = root / 'runs_extra' / (args.run_id + '.json')

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
    endpoint, token = selected_source()
    if not token:
        raise ValueError('Missing data-source token')
    now = datetime.now(timezone(timedelta(hours=8)))
    report = {'id': args.run_id, 'generated_at': now.isoformat(), 'days': [], 'cyq': [], 'errors': [], 'files': {}, 'requests': []}
    started = time.monotonic()
    deadlines = cooldowns(root, now, endpoint)
    blocked = set()

    def call(api, params, fields):
        if api in deadlines:
            raise ValueError(api + ': cooling down until ' + deadlines[api].isoformat())
        if api in blocked:
            raise ValueError(api + ': stopped after unsuccessful response in this run')
        if time.monotonic() - started > args.budget_seconds:
            raise ValueError('Request budget exhausted')
        response = bounded_probe(lambda: probe(api, params, fields, token, endpoint),
                                  report['requests'].append,
                                  lambda: args.budget_seconds - (time.monotonic() - started))
        time.sleep(2)
        if response['status'] not in ('success', 'empty'):
            blocked.add(api)
            raise ValueError(api + ': ' + response['status'] + ' ' + response['message'])
        if set(fields.split(',')) - set(response['fields']):
            raise ValueError(api + ': missing requested fields')
        return [dict(zip(response['fields'], row)) for row in response['items']]

    def persist(relative, payload):
        payload = {'fetched_at': now.isoformat(), 'source': endpoint, **payload}
        save(root / relative, payload)
        report['files'][relative] = sha(payload)

    # --- hm_detail + limit_list_d: only for days the core sync already validated ---
    try:
        available_days = sorted(x.stem for x in (root / 'days').glob('*.json'))
        have_hm = {x.stem for x in (root / 'hm_detail').glob('*.json')} if (root / 'hm_detail').exists() else set()
        have_limit = {x.stem for x in (root / 'limit_list_d').glob('*.json')} if (root / 'limit_list_d').exists() else set()
        pending = [d for d in reversed(available_days) if d not in have_hm or d not in have_limit][:args.batch_days]
        for day in pending:
            entry = {'date': day, 'status': 'success'}
            try:
                if day not in have_hm:
                    rows = fetch_pages('hm_detail', {'trade_date': day},
                                        'trade_date,ts_code,ts_name,buy_amount,sell_amount,net_amount,hm_name,hm_orgs',
                                        ('ts_code', 'hm_name'), call)
                    validate_hm_detail(day, rows)
                    persist('hm_detail/' + day + '.json', {'trade_date': day, 'rows': rows})
                    entry['hm_detail'] = len(rows)
                else:
                    entry['hm_detail'] = 'cached'
                if day not in have_limit:
                    rows = capped_batch('limit_list_d', {'trade_date': day},
                                         'trade_date,ts_code,name,close,pct_chg,limit_amount,fd_amount,open_times,limit_times,limit',
                                         call, 2500)
                    validate_limit_list(day, rows)
                    persist('limit_list_d/' + day + '.json', {'trade_date': day, 'rows': rows})
                    entry['limit_list_d'] = len(rows)
                else:
                    entry['limit_list_d'] = 'cached'
            except ValueError as exc:
                entry['status'] = 'failed'
                report['errors'].append(day + ': ' + str(exc))
                report['days'].append(entry)
                break  # Preserve checkpoint, do not hammer a quota-limited endpoint.
            report['days'].append(entry)
            print(day, entry.get('hm_detail'), entry.get('limit_list_d'), flush=True)
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        report['errors'].append(str(exc).replace(token, '[REDACTED]'))

    # --- cyq_chips: explicit symbol list only, most recent N validated days ---
    codes = sorted({c.strip() for c in args.cyq_symbols.split(',') if c.strip()})
    if codes:
        try:
            target_days = sorted(x.stem for x in (root / 'days').glob('*.json'))[-args.cyq_days:]
            stopped = False
            for code in codes:
                if stopped:
                    break
                cyq_dir = root / 'cyq_chips' / code
                have = {x.stem for x in cyq_dir.glob('*.json')} if cyq_dir.exists() else set()
                for day in target_days:
                    entry = {'code': code, 'date': day, 'status': 'success'}
                    if day in have:
                        entry['cyq_chips'] = 'cached'
                        report['cyq'].append(entry)
                        continue
                    try:
                        rows = capped_batch('cyq_chips', {'trade_date': day, 'ts_code': code},
                                             'ts_code,trade_date,price,percent', call, 6000)
                        validate_cyq_chips(day, code, rows)
                        persist('cyq_chips/' + code + '/' + day + '.json', {'trade_date': day, 'ts_code': code, 'rows': rows})
                        entry['cyq_chips'] = len(rows)
                        report['cyq'].append(entry)
                    except ValueError as exc:
                        entry['status'] = 'failed'
                        report['errors'].append(code + ' ' + day + ': ' + str(exc))
                        report['cyq'].append(entry)
                        stopped = True
                        break
        except (KeyError, TypeError, ArithmeticError) as exc:
            report['errors'].append(str(exc).replace(token, '[REDACTED]'))

    report['status'] = 'success' if not report['errors'] else 'partial'
    save(logpath, report)
    logpath.with_suffix('.md').write_text(render(report))
    (root / 'README_extra.md').write_text(render(report) + '\n[全部扩展同步记录](runs_extra/)\n')
    print('Extra sync archived:', report['status'])
    return 0  # Verification job reports failures after results have been committed.


if __name__ == '__main__':
    raise SystemExit(main())
