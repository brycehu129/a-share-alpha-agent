"""资金流留存：交易时段每 5 分钟，对持仓 ∪ 自选各取一次东方财富分钟资金流，追加进
intraday/flow-<day>.jsonl（私有数据，不进 git）。

为什么不放进盘中引擎的每分钟 tick：引擎的 systemd 时限只有 50 秒，而资金流是**额外的**外部请求
（每只股票一次，接口非官方、可能慢或被限流）。放进去，接口一慢就会拖垮告警本身。所以单独一个进程，
自己的 40 秒预算；取不到就记一行 error，绝不静默跳过——"今天采集了几次、哪只失败了"要能查。

留存的是**当时观测到的累计值**。接口一次返回整天序列，所以这里每次只存最新一行的累计值，
够回答"某个时刻资金流是什么样"，也够在事后（接口已经查不到历史）还原当天的走势。
"""
import argparse
import json
import os
import sys
import time as _time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path

import intraday_engine as ie
import money_flow
import portfolio_book
from collect_quotes import CST

BUDGET_S = 40
WORKERS = 4
INDEX_PREFIXES = ('sh000', 'sh880', 'sz399')


def flow_path(directory, day):
    return Path(directory) / ('flow-%s.jsonl' % day)


def stock_symbols(symbols):
    """只留沪深个股：指数没有资金流，其它市场接口不认。"""
    return [s for s in dict.fromkeys(symbols) if len(s) == 8 and s[:2] in ('sh', 'sz') and s[2:].isdigit()
            and not s.startswith(INDEX_PREFIXES)]


def run(history, now=None, symbols=None, directory=None, fetch=None, calendar_fn=None, force=False,
        budget_s=BUDGET_S, workers=WORKERS):
    """跑一次。返回摘要；不抛出——定时任务里一次失败不能影响下一次。"""
    now = now or datetime.now(CST)
    directory = Path(directory or ie.data_dir())
    fetch = fetch or money_flow.fetch_flow
    summary = {'at': now.isoformat(), 'ran': False}
    if calendar_fn is None:
        from session_brief import calendar_state
        calendar_fn = lambda d: calendar_state(history, d.strftime('%Y%m%d'))
    try:
        calendar = calendar_fn(now)
    except Exception as exc:
        calendar = 'unknown(%s)' % type(exc).__name__
    ok, reason = ie.gate(now, calendar)
    if not ok and not force:
        summary['skipped'] = reason
        return summary
    try:
        syms = stock_symbols(symbols() if callable(symbols) else (symbols if symbols is not None else portfolio_book.all_symbols()))
    except Exception as exc:                      # 账本损坏等：记下原因，别崩
        summary['error'] = '读取持仓/自选失败：%s' % type(exc).__name__
        return summary
    if not syms:
        summary['skipped'] = '没有需要采集的个股'
        return summary

    results = {}
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(fetch, s, now): s for s in syms}
        done, not_done = wait(futures, timeout=budget_s)
        for f in done:
            s = futures[f]
            try:
                results[s] = money_flow.compact(f.result())
            except money_flow.FlowError as exc:
                results[s] = {'error': str(exc)}
            except Exception as exc:              # 解析之外的意外：也只记原因
                results[s] = {'error': '资金流采集异常（%s）' % type(exc).__name__}
        for f in not_done:
            f.cancel()
            results[futures[f]] = {'error': '超出本轮 %d 秒时间预算' % budget_s}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    day = now.date().isoformat()
    for s in syms:
        r = results[s]
        row = {'at': now.isoformat(), 'symbol': s}
        row.update({'error': r['error']} if 'error' in r else {k: v for k, v in r.items() if k != 'flip'})
        ie._append_jsonl(directory, 'flow-%s.jsonl' % day, row)
    failed = [s for s in syms if 'error' in results[s]]
    summary.update(ran=True, symbols=len(syms), ok=len(syms) - len(failed), failed=failed)
    return summary


# --- 读留存（给界面用）---------------------------------------------------------

def read_rows(directory, day, symbol=None):
    rows = ie._read_jsonl(flow_path(directory, day))
    return [r for r in rows if symbol is None or r.get('symbol') == symbol]


def series(directory, day, symbol):
    """某只股票当天成功采集的记录（时间升序）。"""
    return [r for r in read_rows(directory, day, symbol) if 'error' not in r]


def collection_summary(directory, day):
    """{'runs': 采集次数, 'symbols': {symbol: {'ok', 'error', 'last_error'}}}。"""
    rows = read_rows(directory, day)
    per = {}
    for r in rows:
        s = per.setdefault(r.get('symbol'), {'ok': 0, 'error': 0, 'last_error': None})
        if 'error' in r:
            s['error'] += 1
            s['last_error'] = r['error']
        else:
            s['ok'] += 1
    return {'runs': len({r.get('at') for r in rows}), 'symbols': per}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--history', type=Path, default=Path(os.environ.get('HISTORY_DIR', '.history')))
    p.add_argument('--force-session', action='store_true', help='忽略时段闸门（本地调试用）')
    p.add_argument('--json', action='store_true')
    a = p.parse_args()
    started = _time.monotonic()
    result = run(a.history, force=a.force_session)
    result['duration_ms'] = round((_time.monotonic() - started) * 1000)
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif not result.get('ran'):
        print('未运行：%s' % (result.get('skipped') or result.get('error')))
    else:
        print('%s · 采集 %d/%d · %dms%s' % (result['at'], result['ok'], result['symbols'], result['duration_ms'],
                                        ' · 失败 ' + ','.join(result['failed']) if result['failed'] else ''))
    return 0 if result.get('ran') or result.get('skipped') else 1


if __name__ == '__main__':
    sys.exit(main())
