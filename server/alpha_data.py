"""Full-list Tencent daily collection. Cached inputs are never labelled fresh."""
import argparse
import hashlib
import json
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as clock_time
from pathlib import Path
from urllib.parse import urlencode
from urllib.error import HTTPError
from collect_quotes import CST
from daily_data import request, parse_daily
from tushare_sync import read, save


def symbol(code):
    if not re.fullmatch(r'\d{6}\.(SH|SZ)', code):
        raise ValueError('Unsupported symbol')
    return code[-2:].lower() + code[:6]


def completed_day(data):
    fetched = datetime.fromisoformat(data['fetched_at']).astimezone(CST)
    today = fetched.date().isoformat()
    return max((b['date'] for b in data['bars'] if b['date'] < today or
                (b['date'] == today and fetched.time() >= clock_time(15,10))), default='')


def collect(history, run_id, budget=1200):
    now = datetime.now(CST)
    root = history / 'alpha_data'
    root.mkdir(exist_ok=True)
    master = read(history / 'tushare_data/stock_basic.json')
    stocks = [r for r in master['rows'] if r['exchange'] in ('SSE', 'SZSE') and r['list_status'] == 'L']
    if not 1000 < len(stocks) < 10000 or len({s['ts_code'] for s in stocks}) != len(stocks):
        raise ValueError('Stock list incomplete or duplicated')
    started = time.monotonic()
    stopped = threading.Event()
    jobs = [(symbol(s['ts_code']), 'qfq') for s in stocks]
    cutoff = None

    def fetch(job):
        code, adj = job
        cache = root / 'series' / (code + '.json')
        # One full historical refresh per local date/session; current-day bars are
        # re-fetched after close. This cache is only an input, not an audit record.
        session = now.strftime('%Y-%m-%d') + ('-close' if now.hour >= 15 else '-pre')
        if cache.exists():
            old = read(cache)
            if code != 'sh000300' and cutoff and completed_day(old) >= cutoff:
                return {'symbol': code, 'status': 'cached', 'fetched_at': old['fetched_at']}
        if time.monotonic() - started > budget:
            return {'symbol': code, 'status': 'failed', 'error': '本轮请求预算已用尽'}
        if stopped.is_set():
            return {'symbol': code, 'status': 'failed', 'error': '源连续不可用，本轮停止新增请求'}
        url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?' + urlencode({
            'param': f'{code},day,,{now.date().isoformat()},320,' + ('qfq' if adj == 'qfq' else '')})
        try:
            raw = request(url)
            bars = parse_daily(raw, code, adj, now.date())
            data = {'symbol': code, 'adjustment': adj, 'fetched_at': datetime.now(CST).isoformat(),
                    'session': session, 'url': url, 'response_sha256': hashlib.sha256(raw).hexdigest(), 'bars': bars}
            save(cache, data)
            return {'symbol': code, 'status': 'success', 'bars': len(bars), 'fetched_at': data['fetched_at']}
        except HTTPError as exc:
            if exc.code in (401, 403, 429, 501, 503):
                stopped.set()
            return {'symbol': code, 'status': 'failed', 'error': str(exc)[:200]}
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
            return {'symbol': code, 'status': 'failed', 'error': str(exc)[:200]}
        finally:
            time.sleep(0.15)

    results = [fetch(('sh000300', 'none'))]
    bench_path = root/'series/sh000300.json'
    cutoff = completed_day(read(bench_path)) if bench_path.exists() else None
    # Bootstrap missing symbols first, then refresh oldest checkpoints. Never
    # restart the same prefix of a 5,000-stock list after the daily date changes.
    jobs.sort(key=lambda job: ((root/'series'/(job[0]+'.json')).exists(), job[0]))
    with ThreadPoolExecutor(max_workers=3) as pool:
        for r in pool.map(fetch, jobs):
            results.append(r)
            if len(results) % 500 == 0:
                print('Daily scan', len(results), '/', len(jobs), flush=True)
    report = {'id': run_id, 'generated_at': datetime.now(CST).isoformat(), 'started_at': now.isoformat(),
              'master_fetched_at': master['fetched_at'], 'stocks': stocks, 'requests': results,
              'status': 'success' if all(r['status'] != 'failed' for r in results) else 'partial'}
    path = root / 'runs' / (run_id + '.json')
    if path.exists():
        raise ValueError('Run already exists')
    save(path, report)
    print('Full-list scan archived:', report['status'], flush=True)


def raw_bars(history, codes, now):
    """Unadjusted execution prices for candidates/positions; never infer from QFQ."""
    result = {}
    for code in sorted(set(codes)):
        url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?' + urlencode({
            'param': f'{code},day,,{now.date().isoformat()},320,'})
        try:
            raw = request(url)
            result[code] = {'bars': parse_daily(raw, code, 'none', now.date()),
                            'fetched_at': datetime.now(CST).isoformat(), 'url': url,
                            'response_sha256': hashlib.sha256(raw).hexdigest()}
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
            result[code] = {'bars': [], 'error': str(exc)[:200]}
        time.sleep(0.15)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--history', type=Path, required=True)
    p.add_argument('--run-id', required=True)
    a = p.parse_args()
    if not re.fullmatch(r'\d+-\d+', a.run_id):
        raise ValueError('Invalid ID')
    collect(a.history, a.run_id)
