"""看板「市场行情」的补充数据：游资龙虎榜、涨跌停池、涨跌停家数。只用标准库。

数据来自 tushare_extra_sync 的检查点（.history/tushare_data/hm_detail/<日期>.json、
.history/tushare_data/limit_list_d/<日期>.json），由服务端在 /api/dashboard 里补进去，
**不**走 GitHub 上公开的 dashboard/latest.json——这几组数据是公开行情，不含任何持仓，但放在服务端
读可以省掉一条导出链路，也不用等 Actions 重跑。

这两个接口目前只有手动触发的同步任务，覆盖的是近期少数交易日（见 hotmoney_features 的说明）。
所以：**某一天缺失 ≠ 那天没有游资/涨跌停**，只是没同步到。每一组都各自取"最近一次有数据的交易日"，
并带上日期，前端必须把日期展示出来。
"""
import re
import time
from pathlib import Path

from tushare_sync import read

HM_TOP = 30                  # 龙虎榜展示前 30 笔（按净买卖额绝对值）
LIMIT_TOP = 40               # 涨跌停池展示前 40 只
SCAN_DAYS = 10               # 最多往回看几个检查点文件
CACHE_SECONDS = 60
_cache = {}

_LIMIT_ORDER = {'U': 0, 'Z': 1, 'D': 2}


def symbol_of(ts_code):
    """000428.SZ → sz000428；北交所 830799.BJ → bj830799。认不出来返回 None。"""
    m = re.fullmatch(r'(\d{6})\.(SH|SZ|BJ)', str(ts_code or ''))
    return m.group(2).lower() + m.group(1) if m else None


def _iso(day):
    return '%s-%s-%s' % (day[:4], day[4:6], day[6:])


def latest_rows(history, folder):
    """(交易日 YYYYMMDD, rows)：最近一个**有数据**的检查点。校验和不对/空文件的跳过，
    宁可退回更早的一天，也不展示可能被写坏的数据。"""
    d = Path(history) / 'tushare_data' / folder
    if not d.is_dir():
        return None, None
    days = sorted((p.stem for p in d.glob('*.json') if re.fullmatch(r'\d{8}', p.stem)), reverse=True)[:SCAN_DAYS]
    for day in days:
        try:
            rows = read(d / (day + '.json')).get('rows')
        except (OSError, ValueError, KeyError):
            continue
        if rows:
            return day, rows
    return None, None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def hm_table(rows):
    out = []
    for r in rows:
        symbol = symbol_of(r.get('ts_code'))
        net = _float(r.get('net_amount'))
        if symbol is None or net is None:
            continue
        out.append({'symbol': symbol, 'name': r.get('ts_name') or '', 'hm_name': r.get('hm_name') or '', 'net_amount': net})
    out.sort(key=lambda x: -abs(x['net_amount']))
    return out


def limit_table(rows):
    out = []
    for r in rows:
        symbol = symbol_of(r.get('ts_code'))
        if symbol is None or r.get('limit') not in _LIMIT_ORDER:
            continue
        times = _float(r.get('limit_times'))
        out.append({'symbol': symbol, 'name': r.get('name') or '', 'pct_chg': _float(r.get('pct_chg')),
                    'limit': r['limit'], 'limit_times': int(times) if times is not None else None,
                    'fd_amount': _float(r.get('fd_amount'))})
    # 涨停在前（连板多的在前，封单大的在前），然后炸板，最后跌停
    out.sort(key=lambda x: (_LIMIT_ORDER[x['limit']], -(x['limit_times'] or 0), -(x['fd_amount'] or 0)))
    return out


def build(history, now=None):
    """{'hotmoney_board': {...}|None, 'limit_counts': {...}|None}。读不到就是 None，绝不抛出。"""
    now = time.time() if now is None else now
    key = str(history)
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        hm_day, hm_rows = latest_rows(history, 'hm_detail')
        lim_day, lim_rows = latest_rows(history, 'limit_list_d')
    except Exception:
        hm_day = hm_rows = lim_day = lim_rows = None
    hm = hm_table(hm_rows) if hm_rows else []
    lim = limit_table(lim_rows) if lim_rows else []
    board = None
    if hm_day or lim_day:
        board = {'hm_date': _iso(hm_day) if hm_day else None, 'limit_date': _iso(lim_day) if lim_day else None,
                 'hm_rows': hm[:HM_TOP], 'hm_total_rows': len(hm),
                 'limit_rows': lim[:LIMIT_TOP], 'limit_total_rows': len(lim)}
    counts = None
    if lim_day:
        counts = {'date': _iso(lim_day), 'up': sum(x['limit'] == 'U' for x in lim),
                  'down': sum(x['limit'] == 'D' for x in lim), 'broken': sum(x['limit'] == 'Z' for x in lim)}
    result = {'hotmoney_board': board, 'limit_counts': counts}
    _cache[key] = (now, result)
    return result


def approx_limits(history):
    """没有 limit_list_d 时的退路：从最近一次全市场归档按涨跌幅近似数涨跌停（标"近似"）。
    只返回涨跌停近似数与来源时间；读不到返回 None。"""
    try:
        import market_context
        b = market_context.breadth_from_universe(Path(history))
    except Exception:
        return None
    if b.get('status') not in ('ready', 'mixed_dates'):
        return None
    return {'up': b['limit_up_approx'], 'down': b['limit_down_approx'],
            'source_generated_at': b.get('source_generated_at'), 'note': b.get('note')}
