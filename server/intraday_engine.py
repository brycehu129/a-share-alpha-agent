"""盘中轮询引擎：候选池的条件入场/盘中止损和自选股哨兵共用的底座。

这个模块自己**不做任何交易或提醒判断**，只提供四样东西，把纪律焊死在底座里，
免得每个上层各写一遍、各漏一条：

1. **只认实际观测到的报价。** 每轮取一次快照，只把"新鲜且属于今天"的报价交给评估器。
   两次轮询之间价格曾经碰过某个位置又回来了——没观测到就是没发生，绝不事后补记。
   （和 opening_observer 的 observed-quote-v1 同一条纪律。）漏轮询会被记成 gap，
   评估器能看到 gap_seconds，知道这段时间的极值是"未观测"而不是"没有"。
2. **信号有状态，事件才推送。** 评估器只汇报"这个条件此刻成不成立"（signal），
   引擎负责判断"要不要为它推一次"：边沿触发（只在 假→真 时推一次）或冷却触发，
   带重新武装间隔防止价格在阈值附近来回抖动时刷屏。推太多用户就不看了，
   那比不推还糟——所以这件事不能交给每个评估器自己实现。
3. **每个事件带证据。** 报价响应哈希 + 采集时刻 + 报价自带时间，事后能追溯到具体那次响应。
4. **"差一点"记录。** 评估器可以为信号声明一个价位；引擎记录观测到的价格离它最近
   到过多近。"距激活只差 0.3%" 直接说明阈值是不是卡得太死，而这类信息事后无法补。

状态存服务器本地 server/data/private/intraday/（持仓相关，不进公开仓库）。
一个交易日一份状态文件；新的一天从前一天继承**显式声明 carry 的**信号（比如"已跌破
成本价"这种持续状态，否则每天早上都会重复推一遍），其余信号每天重置。

上层怎么接：写一个 `evaluate(tick) -> [signal dict]` 函数，调 register() 注册。
这一期没有注册任何评估器，引擎单独跑起来只做轮询、留档、gap 记录。
"""
import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
import time as _time
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path

import live_quote
import minute_data
import portfolio_book
from collect_quotes import CST

STALE_SECONDS = 180          # 报价时间距采集超过这个数就不算"新鲜"，不交给评估器
GAP_SECONDS = 180            # 相邻两轮的有效交易间隔超过它，记一次 gap
MAX_EVENTS_PER_TICK = 5      # 每轮最多推几条；超出的顺延到下一轮，不丢
DEFAULT_REARM_SECONDS = 300  # 边沿信号变回"假"后，至少安静这么久才允许再次触发
DEFAULT_COOLDOWN_SECONDS = 3600
FINAL_TICK_BEFORE = clock_time(15, 3)   # 15:00 收盘后的最后一轮，抓收盘价与量

SEVERITY_ORDER = {'urgent': 0, 'normal': 1}
EVALUATORS = []


def register(fn):
    """注册评估器：fn(tick) -> 可迭代的 signal dict。返回 fn 方便当装饰器用。"""
    EVALUATORS.append(fn)
    return fn


# --- 时段闸门 ---------------------------------------------------------------

def gate(now, calendar_state):
    """返回 (是否运行, 原因)。只有"日历确认开市 + 连续竞价时段"才运行。

    日历未知（文件缺失/校验失败）时**不运行**而不是假定开市——非交易日拿旧数据
    当实时数据，比漏跑一轮糟得多。"""
    session = live_quote.clock_session(now)
    in_session = session in ('morning', 'afternoon') or (
        session == 'closing' and now.time() < FINAL_TICK_BEFORE)
    if not in_session:
        return False, '非连续竞价时段(%s)' % session
    if calendar_state != 'open':
        return False, '交易日历状态为 %s，不是确认开市' % calendar_state
    return True, session


def trading_seconds_between(a, b):
    """两个时刻之间扣除午休(11:30–13:00)后的秒数。午休不算 gap。"""
    if b <= a:
        return 0.0
    total = (b - a).total_seconds()
    lunch_start = a.replace(hour=11, minute=30, second=0, microsecond=0)
    lunch_end = a.replace(hour=13, minute=0, second=0, microsecond=0)
    overlap = (min(b, lunch_end) - max(a, lunch_start)).total_seconds()
    return total - max(0.0, overlap)


# --- 状态 -------------------------------------------------------------------

def data_dir():
    return Path(os.environ.get('INTRADAY_DIR') or (Path(portfolio_book.default_dir()) / 'intraday'))


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=str(path.parent), suffix='.tmp')
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _state_path(directory, day):
    return directory / ('state-%s.json' % day)


def new_state(day, previous=None):
    """新的一天的状态。只继承前一天显式声明 carry 的信号，且只继承它的 active 标志——
    这样"昨天就已经跌破成本价"的持仓，今天早上不会当成新事件再推一遍。"""
    state = {'date': day, 'last_tick_at': None, 'tick_count': 0, 'signals': {},
             'extremes': {}, 'levels': {}, 'gaps': []}
    for key, rec in ((previous or {}).get('signals') or {}).items():
        if rec.get('carry'):
            state['signals'][key] = {'active': rec['active'], 'carry': True, 'fired': 0,
                                     'last_fired_at': None, 'inactive_since': None,
                                     'carried_from': previous.get('date')}
    return state


def load_state(directory, day):
    path = _state_path(directory, day)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except ValueError:
            # 状态文件损坏：宁可丢状态从头来（最坏是重复推一次），也不能让引擎每分钟都崩。
            broken = path.with_suffix('.broken')
            os.replace(path, broken)
    earlier = sorted(p for p in directory.glob('state-*.json') if p.stem[6:] < day) if directory.exists() else []
    previous = None
    if earlier:
        try:
            previous = json.loads(earlier[-1].read_text(encoding='utf-8'))
        except ValueError:
            previous = None
    return new_state(day, previous)


def save_state(directory, state):
    _atomic_write(_state_path(directory, state['date']), json.dumps(state, ensure_ascii=False, indent=1))


def _append_jsonl(directory, name, record):
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# --- 信号 → 事件 ------------------------------------------------------------

def normalize_signal(sig):
    for field in ('key', 'symbol', 'kind'):
        if not sig.get(field):
            raise ValueError('信号缺少必填字段 %s: %r' % (field, sig))
    out = {'active': bool(sig.get('active')), 'mode': sig.get('mode', 'edge'),
           'severity': sig.get('severity', 'normal'), 'carry': bool(sig.get('carry', False)),
           'rearm_s': sig.get('rearm_s', DEFAULT_REARM_SECONDS),
           'cooldown_s': sig.get('cooldown_s', DEFAULT_COOLDOWN_SECONDS),
           'detail': sig.get('detail'), 'level': sig.get('level'),
           **{k: sig[k] for k in ('key', 'symbol', 'kind')}}
    if out['mode'] not in ('edge', 'cooldown'):
        raise ValueError('未知触发模式: %r' % out['mode'])
    if out['severity'] not in SEVERITY_ORDER:
        raise ValueError('未知严重级别: %r' % out['severity'])
    return out


def _seconds_since(iso, now):
    return (now - datetime.fromisoformat(iso)).total_seconds()


def decide(rec, sig, now):
    """这个信号这一轮要不要推。返回 (是否推, 是否抖动被压制)。不修改 rec。"""
    if not sig['active']:
        return False, False
    if sig['mode'] == 'cooldown':
        last = rec.get('last_fired_at')
        return (last is None or _seconds_since(last, now) >= sig['cooldown_s']), False
    if rec.get('active'):
        return False, False                       # 持续为真：边沿信号不重复推
    if rec.get('last_fired_at') is None:
        return True, False                        # 从未推过：第一次转真
    quiet = rec.get('inactive_since')
    if quiet is not None and _seconds_since(quiet, now) < sig['rearm_s']:
        return False, True                        # 阈值附近抖动，压制
    return True, False


def update_levels(state, sig, quote):
    """记录观测到的价格离信号价位最近到过多近（"差一点"）。只用实际观测到的报价。"""
    level = sig.get('level')
    if not level or not quote:
        return
    price, target, direction = float(quote['last']), float(level['price']), level['direction']
    gap_pct = (target / price - 1) * 100 if direction == 'up' else (price / target - 1) * 100
    rec = state['levels'].setdefault(sig['key'], {
        'price': target, 'direction': direction, 'closest_pct': None,
        'closest_at': None, 'reached': False})
    rec['price'], rec['direction'] = target, direction
    # closest_pct 只记"还差多少"：越过价位就是 0，不记负数——否则"越过0.5%"会被 abs()
    # 比较成"还差0.5%"，和真正差一点的情形混在一起。
    remaining = max(gap_pct, 0.0)
    if rec['closest_pct'] is None or remaining < rec['closest_pct']:
        rec['closest_pct'] = round(remaining, 4)
        rec['closest_at'] = quote.get('quote_at') or quote.get('quote_at_raw')
    if gap_pct <= 0:
        rec['reached'] = True


def apply_signals(state, signals, quotes, now):
    """两阶段：先算出所有想推的，按严重级别排序、按每轮上限截断，**只有真正推出去的
    才提交状态**。被上限挤掉的不标记已推——下一轮它们仍是"从未推过/刚转真"，会顺延推出，
    而不是悄悄丢失。返回 (events, deferred_keys, flap_suppressed_keys)。"""
    now_iso = now.isoformat()
    plan, flapped, seen = [], [], set()
    for raw in signals:
        sig = normalize_signal(raw)
        if sig['key'] in seen:
            # 同一个 key 在同一轮出现两次（比如两个评估器都汇报了它）：只认第一条。
            # 否则两条会各自判定为"从未推过"，同一件事推两次；而且状态提交阶段还会互相覆盖。
            continue
        seen.add(sig['key'])
        rec = state['signals'].setdefault(sig['key'], {
            'active': False, 'carry': sig['carry'], 'fired': 0,
            'last_fired_at': None, 'inactive_since': None})
        rec['carry'] = sig['carry']
        update_levels(state, sig, quotes.get(sig['symbol']))
        fire, flap = decide(rec, sig, now)
        if flap:
            flapped.append(sig['key'])
        plan.append((sig, rec, fire))
    candidates = sorted((p for p in plan if p[2]), key=lambda p: (SEVERITY_ORDER[p[0]['severity']], p[0]['key']))
    emit = candidates[:MAX_EVENTS_PER_TICK]
    deferred = [p[0]['key'] for p in candidates[MAX_EVENTS_PER_TICK:]]
    emitted_keys = {p[0]['key'] for p in emit}
    events = []
    for sig, rec, fire in plan:
        if sig['key'] in emitted_keys:
            rec['last_fired_at'] = now_iso
            rec['fired'] = rec.get('fired', 0) + 1
        if sig['key'] in deferred:
            continue                                   # 保持"未武装的旧状态"，下一轮再判
        if sig['active']:
            rec['active'] = True
            rec['inactive_since'] = None
        else:
            # 刚从"真"变"假"，或这个信号第一次被观测且为假：从现在起计"安静了多久"。
            if rec.get('active') or rec.get('inactive_since') is None:
                rec['inactive_since'] = now_iso
            rec['active'] = False
    for sig, rec, fire in emit:
        q = quotes.get(sig['symbol']) or {}
        events.append({
            'key': sig['key'], 'symbol': sig['symbol'], 'kind': sig['kind'],
            'severity': sig['severity'], 'detail': sig['detail'], 'observed_at': now_iso,
            'price': q.get('last'), 'quote_at': q.get('quote_at'),
            'evidence': {'batch_sha256': q.get('batch_sha256'), 'batch_fetched_at': q.get('batch_fetched_at')},
        })
    return events, deferred, flapped


# --- 一轮 -------------------------------------------------------------------

def make_tick(now, session, snapshot, state, minute_fetch):
    fresh, stale, wrong_day = {}, [], []
    for q in snapshot['quotes']:
        if q.get('quote_date') != now.date().isoformat():
            wrong_day.append(q['symbol'])
        elif q.get('age_seconds') is None or q['age_seconds'] > STALE_SECONDS:
            stale.append(q['symbol'])
        else:
            fresh[q['symbol']] = q
    prev = datetime.fromisoformat(state['last_tick_at']) if state.get('last_tick_at') else None
    gap = trading_seconds_between(prev, now) if prev else None
    minute_cache = {}

    def minutes(symbol):
        """按需取当日分时，一轮内每只股票最多请求一次。分时日期不是今天就报错——
        接口在盘前/非交易日返回的是上一交易日的数据，不能当今天的走势用。"""
        if symbol not in minute_cache:
            result = minute_fetch(symbol, now)
            if not minute_data.is_today(result, now):
                raise minute_data.MinuteError('分时数据日期是 %s，不是今天' % result['trade_date'])
            minute_cache[symbol] = result
        return minute_cache[symbol]

    def node_due(hhmm):
        """固定时间节点(如 '0945')在上一轮和这一轮之间被跨过了吗？返回迟到秒数，否则 None。
        没有上一轮（当天第一轮）就从当日 00:00 起算。迟到多少由评估器自己决定要不要放弃。"""
        node = now.replace(hour=int(hhmm[:2]), minute=int(hhmm[2:]), second=0, microsecond=0)
        since = prev if prev else now.replace(hour=0, minute=0, second=0, microsecond=0)
        return (now - node).total_seconds() if since < node <= now else None

    return {'now': now, 'session': session, 'quotes': fresh, 'stale': stale, 'wrong_day': wrong_day,
            'failures': snapshot['failures'], 'gap_seconds': gap, 'prev_tick_at': prev,
            'state': state, 'minutes': minutes, 'node_due': node_due}


def record_extremes(state, quotes):
    for symbol, q in quotes.items():
        price = float(q['last'])
        e = state['extremes'].setdefault(symbol, {'first': price, 'high': price, 'low': price, 'n': 0})
        e['high'], e['low'], e['n'] = max(e['high'], price), min(e['low'], price), e['n'] + 1


def run_tick(history, symbols, now=None, snapshot_fn=None, minute_fn=None, calendar_fn=None,
             directory=None, force=False, dry_run=False, evaluators=None):
    """跑一轮。返回摘要 dict；不抛出（定时任务里一次失败不能影响下一分钟）。"""
    now = now or datetime.now(CST)
    directory = directory or data_dir()
    snapshot_fn = snapshot_fn or live_quote.snapshot
    minute_fn = minute_fn or minute_data.fetch_minute
    evaluators = EVALUATORS if evaluators is None else evaluators
    started = _time.monotonic()
    summary = {'at': now.isoformat(), 'ran': False, 'dry_run': dry_run}

    if calendar_fn is None:
        from session_brief import calendar_state
        calendar_fn = lambda d: calendar_state(history, d.strftime('%Y%m%d'))
    try:
        calendar = calendar_fn(now)
    except Exception as exc:  # 归档正被 git reset 改写等情况：视为未知，不运行
        calendar = 'unknown(%s)' % type(exc).__name__
    ok, reason = gate(now, calendar)
    if not ok and not force:
        summary['skipped'] = reason
        return summary
    session = live_quote.clock_session(now)
    if not ok:
        summary['forced'] = reason

    # symbols 可以是返回列表的函数：通过时段闸门之后才求值。非交易时段每分钟都会触发，
    # 那时候去读计划/账本文件纯属浪费。
    if callable(symbols):
        symbols = symbols()
    if not symbols:
        summary['skipped'] = '没有需要监控的股票'
        return summary

    lock = None
    if not dry_run:
        directory.mkdir(parents=True, exist_ok=True)
        lock = open(directory / '.lock', 'a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            summary['skipped'] = '上一轮仍在运行，本轮跳过（不排队，避免状态互相覆盖）'
            return summary
    try:
        state = load_state(directory, now.date().isoformat()) if directory.exists() else new_state(now.date().isoformat())
        snapshot = snapshot_fn(symbols)
        tick = make_tick(now, session, snapshot, state, minute_fn)
        tick['dry_run'] = dry_run      # 有副作用的评估器（如条件执行器要写账本）必须尊重它

        if tick['gap_seconds'] is not None and tick['gap_seconds'] > GAP_SECONDS:
            state['gaps'].append({'from': state['last_tick_at'], 'to': now.isoformat(),
                                  'seconds': round(tick['gap_seconds'])})

        signals, errors = [], []
        for fn in evaluators:
            try:
                signals.extend(list(fn(tick) or []))
            except Exception as exc:  # 一个评估器坏了不能让其余的一起哑掉
                errors.append('%s: %s: %s' % (getattr(fn, '__name__', 'evaluator'), type(exc).__name__, str(exc)[:120]))
        events, deferred, flapped = apply_signals(state, signals, tick['quotes'], now)
        record_extremes(state, tick['quotes'])
        state['last_tick_at'] = now.isoformat()
        state['tick_count'] += 1

        summary.update(ran=True, session=session, symbols=len(symbols), fresh=len(tick['quotes']),
                       stale=tick['stale'], wrong_day=tick['wrong_day'],
                       failures=[f['symbol'] for f in snapshot['failures']],
                       gap_seconds=None if tick['gap_seconds'] is None else round(tick['gap_seconds']),
                       signals=len(signals), events=events, deferred=deferred,
                       flap_suppressed=flapped, evaluator_errors=errors,
                       duration_ms=round((_time.monotonic() - started) * 1000))
        if not dry_run:
            save_state(directory, state)
            log = {k: v for k, v in summary.items() if k != 'events'}
            log['event_count'] = len(events)
            _append_jsonl(directory, 'ticks-%s.jsonl' % state['date'], log)
            for e in events:
                _append_jsonl(directory, 'events-%s.jsonl' % state['date'], e)
        return summary
    except Exception as exc:
        summary.update(error='%s: %s' % (type(exc).__name__, str(exc)[:200]))
        return summary
    finally:
        if lock:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()


def default_symbols():
    return portfolio_book.all_symbols()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--history', type=Path, default=Path(os.environ.get('HISTORY_DIR', '.history')))
    p.add_argument('--symbols', help='逗号分隔；默认取持仓+自选股')
    p.add_argument('--force-session', action='store_true', help='忽略时段闸门（本地调试用）')
    p.add_argument('--dry-run', action='store_true', help='不写状态与日志')
    p.add_argument('--json', action='store_true')
    a = p.parse_args()
    explicit = [s.strip() for s in a.symbols.split(',') if s.strip()] if a.symbols else None
    if explicit and any(not re.fullmatch(r'(sh|sz)\d{6}', s) for s in explicit):
        p.error('只支持沪深个股代码')

    # 挂上条件执行器（exec-0.2）。它需要盯的股票——今天还在观察的计划和所有持仓——在通过
    # 时段闸门之后才去读，所以 symbols 传的是函数。
    import conditional_exec
    import sentinel as sentinel_mod
    executor = conditional_exec.attach(a.history)
    watcher = sentinel_mod.Sentinel(a.history)          # 自选股哨兵：持仓/自选 → 规则信号 → 告警
    register(watcher.evaluate)

    def symbols():
        base = explicit if explicit is not None else watcher.symbols()
        return sorted(set(base) | set(executor.watch_symbols(datetime.now(CST), dry_run=a.dry_run)))

    result = run_tick(a.history, symbols, force=a.force_session, dry_run=a.dry_run)
    # 告警在这里推，而不是在评估器里：评估器只汇报"条件成不成立"，推不推由引擎去重之后才知道。
    # 只推快的（规则层事实）；慢的 AI 情景研判由 sentinel.py analyze 独立进程处理。
    if result.get('ran'):
        result['sentinel'] = watcher.after_tick(result)
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        if not result.get('ran'):
            print('未运行：%s' % (result.get('skipped') or result.get('error')))
        else:
            print('%s · %s · 新鲜 %d/%d · 信号 %d · 事件 %d · %dms' % (
                result['at'], result['session'], result['fresh'], result['symbols'],
                result['signals'], len(result['events']), result['duration_ms']))
            for label, key in (('陈旧', 'stale'), ('非今日', 'wrong_day'), ('取价失败', 'failures')):
                if result[key]:
                    print('  %s: %s' % (label, ','.join(result[key])))
            for e in result['events']:
                print('  事件 [%s] %s %s：%s' % (e['severity'], e['symbol'], e['kind'], e['detail']))
            for err in result['evaluator_errors']:
                print('  评估器异常: ' + err)
            if result.get('sentinel', {}).get('alerts'):
                print('  哨兵告警 %d 条，推送：%s' % (result['sentinel']['alerts'], result['sentinel'].get('push')))
    return 0 if result.get('ran') or result.get('skipped') else 1


if __name__ == '__main__':
    sys.exit(main())
