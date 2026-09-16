"""Offline, auditable conversion of paired daily prices and adjustment factors."""
import argparse
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from collect_quotes import CST
from tushare_sync import read, save, sha, check_day
from alpha_data import symbol


def convert(rows):
    rows = sorted(rows, key=lambda r: r['trade_date'])
    if len({r['trade_date'] for r in rows}) != len(rows):
        raise ValueError('Duplicate daily date')
    if not rows:
        return [], []
    anchor = Decimal(str(rows[-1]['adj_factor']))
    adjusted, raw = [], []
    for r in rows:
        factor = Decimal(str(r['adj_factor']))
        if not factor.is_finite() or factor <= 0 or not anchor.is_finite() or anchor <= 0:
            raise ValueError('Invalid adjustment factor')
        d = datetime.strptime(r['trade_date'], '%Y%m%d').date().isoformat()
        bar = {'date': d, 'volume_raw': str(r['vol'])}
        values = {k: Decimal(str(r[k])) for k in ('open','high','low','close')}
        raw.append({**bar, **{k:str(v) for k,v in values.items()}})
        adjusted.append({**bar, **{k:str(v * factor / anchor) for k,v in values.items()}})
    return adjusted, raw


def run(history, run_id):
    now = datetime.now(CST)
    root = history / 'tushare_data'
    calendar = read(root / 'trade_cal.json')['calendars']
    opens = [{r['cal_date'] for r in calendar[e] if str(r['is_open']) == '1' and r['cal_date'] < now.strftime('%Y%m%d')} for e in ('SSE','SZSE')]
    if opens[0] != opens[1]:
        raise ValueError('Exchange calendars disagree')
    dates = sorted(opens[0])[-60:]
    # Only use a consecutive suffix ending on the latest completed session.
    available = []
    for day in reversed(dates):
        if not (root / 'days' / (day+'.json')).exists():
            break
        available.append(day)
    if len(available) < 21:
        raise ValueError('Need at least 21 consecutive saved sessions through the latest prior session')
    master = read(root / 'stock_basic.json')
    stocks = [r for r in master['rows'] if r['exchange'] in ('SSE','SZSE') and r['list_status']=='L']
    rows, hashes, fetched, sources = defaultdict(list), {}, [], set()
    for day in sorted(available):
        data = read(root / 'days' / (day+'.json'))
        check_day(day, data['daily'], data['adj_factor'])
        factors = {r['ts_code']:r['adj_factor'] for r in data['adj_factor']}
        hashes[day] = sha(data)
        fetched.append(data['fetched_at'])
        sources.add(data['source'])
        for row in data['daily']:
            rows[row['ts_code']].append({**row, 'adj_factor':factors[row['ts_code']]})
    cutoff = datetime.strptime(max(available), '%Y%m%d').date().isoformat()
    destination = history / 'alpha_data' / 'tushare_series'
    counts = []
    for stock in stocks:
        code = symbol(stock['ts_code'])
        bars, raw = convert(rows[stock['ts_code']])
        save(destination / (code+'.json'), {'symbol':code, 'adjustment':'qfq',
             'provider':'Tushare-compatible paired daily', 'sources':sorted(sources),
             'fetched_at':max(fetched), 'converted_at':now.isoformat(), 'cutoff':cutoff,
             'source_manifest':run_id, 'bars':bars, 'raw_bars':raw})
        counts.append(len(bars))
    manifest = {'id':run_id,'generated_at':now.isoformat(),'cutoff':cutoff,
                'sessions':len(available),'stocks':len(stocks), 'with_21_bars':sum(n>=21 for n in counts),
                'source_hashes':hashes,'sources':sorted(sources),
                'notes':'成交量保持手；复权价=原价×当日因子/窗口末因子；缺失日不填充。'}
    save(history / 'alpha_data' / 'bridge_runs' / (run_id+'.json'), manifest)
    save(history / 'alpha_data' / 'bridge_latest.json', manifest)
    print({k:v for k,v in manifest.items() if k!='source_hashes'}, flush=True)


if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--history',type=Path,required=True)
    p.add_argument('--run-id',required=True)
    a=p.parse_args()
    run(a.history,a.run_id)
