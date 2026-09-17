"""Same-day hot-money and limit-board signals, read from tushare_extra_sync's
checkpoints (tushare_data/hm_detail/<date>.json, tushare_data/limit_list_d/<date>.json).

These two interfaces only started being synced 2026-09-17 and are not wired
into daily-agent.yml yet (workflow_dispatch only, batch-days=10 per run), so
coverage is a rolling handful of recent trading days at best. A missing
checkpoint for a given date means 'not synced (yet)', never 'no hot-money
activity that day' -- callers must treat absence as no-signal, not as a
negative one.
"""
import re

from tushare_sync import read


def to_symbol(ts_code):
    # Same convention as alpha_data.symbol(), duplicated rather than imported
    # so this module has no dependency on the quote pipeline. Returns None
    # (never raises) for anything outside SH/SZ -- hm_detail/limit_list_d can
    # include 北交所 codes that this project's SSE/SZSE-only universe never
    # covers, and those rows should be skipped, not crash the load.
    if not re.fullmatch(r'\d{6}\.(SH|SZ)', ts_code):
        return None
    return ts_code[-2:].lower() + ts_code[:6]


def _load_day(history, folder, day):
    path = history / 'tushare_data' / folder / (day + '.json')
    if not path.exists():
        return None
    return read(path)['rows']


def load(history, cutoff):
    """Returns (signals, availability).

    signals: {'sz000001': {'hm_net_amount': float or None, 'hm_desks': int,
                            'limit_status': 'U'/'D'/'Z' or None,
                            'limit_times': int or None}}
    Only ever describes `cutoff`'s own session -- never backfilled or carried
    over from a prior day, and never aggregated across multiple days.

    availability: {'hm_detail': bool, 'limit_list_d': bool} -- whether a
    checkpoint exists for `cutoff` at all, so callers can tell 'no file yet'
    apart from 'file exists, zero rows'.
    """
    signals = {}

    def slot(code):
        return signals.setdefault(code, {'hm_net_amount': None, 'hm_desks': 0,
                                          'limit_status': None, 'limit_times': None})

    hm_rows = _load_day(history, 'hm_detail', cutoff)
    if hm_rows is not None:
        for r in hm_rows:
            code = to_symbol(r['ts_code'])
            if code is None:
                continue
            s = slot(code)
            s['hm_net_amount'] = (s['hm_net_amount'] or 0.0) + float(r['net_amount'])
            s['hm_desks'] += 1

    limit_rows = _load_day(history, 'limit_list_d', cutoff)
    if limit_rows is not None:
        for r in limit_rows:
            code = to_symbol(r['ts_code'])
            if code is None:
                continue
            s = slot(code)
            s['limit_status'] = r['limit']
            s['limit_times'] = int(r['limit_times']) if r.get('limit_times') is not None else None

    return signals, {'hm_detail': hm_rows is not None, 'limit_list_d': limit_rows is not None}
