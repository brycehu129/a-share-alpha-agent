"""Independent BaoStock daily fallback. One sequential session; bounded runtime."""
import argparse
import socket
import time
from datetime import datetime, timedelta, date
from decimal import Decimal
from pathlib import Path
from collect_quotes import CST
from tushare_sync import read, save, sha
from alpha_data import symbol


def normalize(rows):
    result, previous = [], None
    for row in rows:
        d = date.fromisoformat(row['date']).isoformat()
        if previous is not None and d <= previous:
            raise ValueError('Duplicate or unordered date')
        previous = d
        if row.get('tradestatus') == '0':
            continue  # Missing session will block candidate, not become fake liquidity.
        nums = {k:Decimal(row[k]) for k in ('open','high','low','close','volume')}
        if any(not n.is_finite() or n < 0 for n in nums.values()):
            raise ValueError('Invalid OHLCV')
        if not 0 < nums['low'] <= min(nums['open'],nums['close']) <= max(nums['open'],nums['close']) <= nums['high']:
            raise ValueError('Invalid OHLC ordering')
        # BaoStock volume is shares; normalize to the Tencent lot scale used
        # by the relative liquidity proxy. Keep original unit and provider explicit.
        result.append({'date':d, **{k:str(nums[k]) for k in ('open','high','low','close')},
                       'volume_raw':str(nums['volume']/100), 'is_st':row.get('isST')})
    if not result:
        raise ValueError('Empty daily series')
    return result[-320:]


def collect(history, run_id, budget=900):
    now = datetime.now(CST)
    report = {'generated_at':now.isoformat(),'provider':'BaoStock','status':'partial','requests':[], 'errors':[]}
    root = history/'alpha_data'
    path = root/'fallback_runs'/(run_id+'.json')
    if path.exists():
        raise ValueError('Fallback run exists')
    started = time.monotonic()
    socket.setdefaulttimeout(15)
    try:
        import baostock as bs
        logged = bs.login()
        if logged.error_code != '0':
            raise ValueError('Login failed: '+logged.error_msg)
        try:
            master = read(history/'tushare_data/stock_basic.json')
            stocks = [s for s in master['rows'] if s['exchange'] in ('SSE','SZSE') and s['list_status']=='L']
            session = now.strftime('%Y-%m-%d')+('-close' if now.hour>=15 else '-pre')
            failures = 0
            for stock in stocks:
                code = symbol(stock['ts_code'])
                target = root/'series'/(code+'.json')
                if target.exists() and read(target).get('session') == session:
                    continue
                if time.monotonic()-started > budget or failures >= 3:
                    report['errors'].append('请求预算用尽或连续3次失败；剩余清单保留缺失状态')
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
                            'session':session,'url':'https://www.baostock.com/','response_sha256':sha(rows),
                            'original_volume_unit':'shares','normalized_volume_unit':'100 shares','bars':bars}
                    save(target,data)
                    report['requests'].append({'symbol':code,'status':'success','bars':len(bars)})
                    failures = 0
                except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
                    failures += 1
                    report['requests'].append({'symbol':code,'status':'failed','error':str(exc)[:200]})
                if len(report['requests'])%500 == 0:
                    print('BaoStock processed',len(report['requests']),flush=True)
                time.sleep(0.1)
            report['status'] = 'success' if not report['errors'] and all(x['status']=='success' for x in report['requests']) else 'partial'
        finally:
            bs.logout()
    except (ImportError, OSError, ValueError, KeyError) as exc:
        report['errors'].append(str(exc)[:300])
    report['finished_at'] = datetime.now(CST).isoformat()
    save(path,report)
    print('BaoStock fallback:',report['status'],report['errors'],flush=True)


if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--history',type=Path,required=True)
    p.add_argument('--run-id',required=True)
    a=p.parse_args()
    collect(a.history,a.run_id)
