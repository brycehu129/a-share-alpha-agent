"""Bounded independent BaoStock sessions, disjoint resumable stock partitions."""
import argparse
import socket
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, date
from decimal import Decimal
from pathlib import Path
from collect_quotes import CST
from tushare_sync import read, save, sha
from alpha_data import symbol, completed_day


def normalize(rows):
    result, previous = [], None
    for row in rows:
        d = date.fromisoformat(row['date']).isoformat()
        if previous is not None and d <= previous:
            raise ValueError('Duplicate or unordered date')
        previous = d
        if row.get('tradestatus') == '0':
            continue
        nums = {k:Decimal(row[k]) for k in ('open','high','low','close','volume')}
        if any(not n.is_finite() or n < 0 for n in nums.values()):
            raise ValueError('Invalid OHLCV')
        if not 0 < nums['low'] <= min(nums['open'],nums['close']) <= max(nums['open'],nums['close']) <= nums['high']:
            raise ValueError('Invalid OHLC ordering')
        result.append({'date':d, **{k:str(nums[k]) for k in ('open','high','low','close')},
                       'volume_raw':str(nums['volume']/100), 'is_st':row.get('isST')})
    if not result:
        raise ValueError('Empty daily series')
    return result[-320:]


def partition(args):
    history, codes, run_id, part_id, budget = args
    root = Path(history)/'alpha_data'
    now = datetime.now(CST)
    report = {'generated_at':now.isoformat(),'provider':'BaoStock','requests':[],'errors':[], 'status':'partial'}
    started = time.monotonic()
    socket.setdefaulttimeout(15)
    try:
        import baostock as bs
        logged = bs.login()
        if logged.error_code != '0':
            raise ValueError('Login failed: '+logged.error_msg)
        try:
            failures = 0
            for code in codes:
                if time.monotonic()-started > budget or failures >= 3:
                    report['errors'].append('分批请求预算用尽或连续3次失败；未完成股票下次续跑')
                    break
                try:
                    response = bs.query_history_k_data_plus(code[:2]+'.'+code[2:],
                        'date,code,open,high,low,close,volume,tradestatus,isST',
                        start_date=(now.date()-timedelta(days=550)).isoformat(), end_date=now.date().isoformat(),
                        frequency='d', adjustflag='2')
                    if response.error_code != '0':
                        raise ValueError(response.error_msg)
                    rows = []
                    while response.error_code == '0' and response.next():
                        rows.append(dict(zip(response.fields,response.get_row_data())))
                    if response.error_code != '0':
                        raise ValueError(response.error_msg)
                    bars = normalize(rows)
                    data = {'symbol':code,'adjustment':'qfq','provider':'BaoStock','fetched_at':datetime.now(CST).isoformat(),
                            'url':'https://www.baostock.com/','response_sha256':sha(rows),
                            'original_volume_unit':'shares','normalized_volume_unit':'100 shares','bars':bars}
                    save(root/'series'/(code+'.json'),data)
                    report['requests'].append({'symbol':code,'status':'success','bars':len(bars)})
                    failures = 0
                except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
                    failures += 1
                    report['requests'].append({'symbol':code,'status':'failed','error':str(exc)[:200]})
                if len(report['requests'])%100 == 0:
                    print('BaoStock part',part_id,'processed',len(report['requests']),flush=True)
                    save(root/'fallback_parts'/(run_id+'-'+str(part_id)+'.json'),report)
                time.sleep(0.2)
        finally:
            bs.logout()
    except (ImportError, OSError, ValueError, KeyError) as exc:
        report['errors'].append(str(exc)[:300])
    report['finished_at'] = datetime.now(CST).isoformat()
    report['status'] = 'success' if not report['errors'] and all(r['status']=='success' for r in report['requests']) else 'partial'
    save(root/'fallback_parts'/(run_id+'-'+str(part_id)+'.json'),report)
    return report


def collect(history, run_id, budget=2100, workers=6):
    now = datetime.now(CST)
    root = history/'alpha_data'
    path = root/'fallback_runs'/(run_id+'.json')
    if path.exists():
        raise ValueError('Fallback run exists')
    report = {'generated_at':now.isoformat(),'provider':'BaoStock','status':'partial','requests':[], 'errors':[]}
    master = read(history/'tushare_data/stock_basic.json')
    stocks = [s for s in master['rows'] if s['exchange'] in ('SSE','SZSE') and s['list_status']=='L']
    bench = root/'series/sh000300.json'
    cutoff = completed_day(read(bench)) if bench.exists() else now.date().isoformat()
    pending = []
    for stock in stocks:
        code = symbol(stock['ts_code'])
        cached = root/'series'/(code+'.json')
        if cached.exists():
            data = read(cached)
            if completed_day(data) >= cutoff:
                continue
            pending.append((1,data['fetched_at'],code))
        else:
            pending.append((0,'',code))
    codes = [r[2] for r in sorted(pending)]
    report.update(target_cutoff=cutoff,pending_before=len(codes),workers=workers)
    if codes:
        # Each process owns one public SDK session; never share its socket.
        batches = [(str(history), codes[i::workers], run_id, i, budget) for i in range(workers) if codes[i::workers]]
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for part in pool.map(partition,batches):
                report['requests'].extend(part['requests'])
                report['errors'].extend(part['errors'])
    report['finished_at'] = datetime.now(CST).isoformat()
    report['status'] = 'success' if not report['errors'] and all(r['status']=='success' for r in report['requests']) else 'partial'
    save(path,report)
    print('BaoStock fallback:',report['status'],'processed',len(report['requests']),report['errors'],flush=True)


if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--history',type=Path,required=True)
    p.add_argument('--run-id',required=True)
    a=p.parse_args()
    collect(a.history,a.run_id)
