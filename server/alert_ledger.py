"""规则层告警的每日机制核对 + 结果标签。和 scenario_ledger.py 是同一套纪律，但对象不同：
scenario_ledger 对账 AI 给出的情景（触发/目标/失效价）；这里对账**规则层**自己发出的告警
（stop_hit、target_hit、buy_signal、高抛低吸……）——这些告警从发布以来从没有人验证过发的对不对。

两件事，都在收盘后 15:20 随现有的 alpha-shadow-sentinel-reconcile.timer 一起跑（不新增定时任务）：

**① 机制核对（audit_day）**：只核对系统自己的记录内部是否一致，不重新判定业务规则该不该触发
（那部分已经在 test_sentinel_rules.py 里用单测覆盖）——所以几乎不该有假阳性：

  - 引擎层（intraday/events-<day>.jsonl）触发的每个哨兵信号，必须在告警层（sentinel/alerts-
    <day>.jsonl）的留档里出现同样的次数——不多（重复推送）也不少（丢事件）。
  - 告警文字里带的现价，必须和触发前后最近一次 intraday/bars-<day>.jsonl 里记录的观测价一致
    （两处时间戳来自不同的 datetime.now() 调用，允许 120 秒内的最近一条，不要求逐秒对齐）。
  - watch 档不能出现在真正推送出去的文本里；标了推送成功的档必须真的在推送文本里。
  - 同一个信号 key 当天相邻两次触发的间隔不能违反它自己的重新武装间隔（`REARM_BY_KIND`，直接
    引用 sentinel_rules.py 里的命名常量；below_cost/volume_surge(_down) 用的是该文件里的内联
    字面量，这里同步硬编码——sentinel_rules.py 改了这几个数字，这里不会自动跟着变）。

任何不一致都进 `mismatches`，不抛异常：这是诊断报告，不是硬性关卡。

**② 结果标签（label_day）**：只用告警**之后**、同一交易日收盘前的观测价，判定"如果照这条
提示做，当天有没有被验证"。和 scenario_ledger 的纪律完全一致：只用发布之后的数据（不事后
诸葛亮）；保留"无结论"，不硬塞进对错；样本 <20 只给计数，不给百分比；只统计**确认推送成功**
的告警（未送达的和 scenario_ledger 排除 delivered=False 是同一个理由——你根本没看到，不该
计入统计）。

**局限（比 scenario_ledger 更明显的几条，写进每份汇总）**：
  - 只看当天收盘前的数据——不是最初设想的"次日开盘/收盘"，因为 15:20 跑的时候明天还没发生。
  - 标签只回答"这条提示本身有没有被验证"，不等于照做了会赚钱：滑点、流动性、你是否真的执行
    都不考虑在内。
  - ATR 用来把"避损/错杀"的判定门槛按每只股票自己的波动缩放，和 book_levels.py 的止损/止盈
    算法同源，但这里只是**评分口径**，不是新的交易规则；阈值都是起始默认值，没有前瞻验证。
"""
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import exec_spec
import intraday_engine as ie
import live_check
import scenario_ledger
import sentinel_rules as sr
import shortterm_model
from collect_quotes import CST

# --- 会被打分/核对的信号种类 ---------------------------------------------------

# 卖出/减仓类：无论原始触发是下跌事件（止损）还是上涨事件（止盈），问题都一样——
# 照这条提示卖了/减了，后面是继续走弱（该卖，避损）还是大幅反弹（卖早了，错杀）？
# 数字是 ATR 倍数：urgent 档用更大的门槛（1.0），action 档（止盈/均线破位）用更敏感的门槛（0.5）。
SELL_SIDE_ATR_MULT = {
    'sentinel.stop_hit': 1.0,
    'sentinel.trail_stop_hit': 1.0,
    'sentinel.low20_break_volume': 1.0,
    'sentinel.near_limit_down': 1.0,
    'sentinel.volume_surge_down': 1.0,
    'sentinel.target_hit': 0.5,
    'sentinel.ma20_break': 0.5,
    'sentinel.ma60_break': 0.5,
}
T_SELL_KIND = 'sentinel.t_sell_high'
T_BUY_KIND = 'sentinel.t_buy_low'
BUY_SIGNAL_KIND = 'sentinel.buy_signal'
LABELED_KINDS = set(SELL_SIDE_ATR_MULT) | {T_SELL_KIND, T_BUY_KIND, BUY_SIGNAL_KIND}

T_BUYBACK_BUFFER_PCT = 0.2      # 往返成本之外再留一点缓冲，避免"刚好打平"也算验证成立
BUY_SIGNAL_ATR_MULT_UP = 1.0
BUY_SIGNAL_ATR_MULT_DOWN = 0.5

OUTCOME_LABEL = {
    **{k: {'favorable': '避损', 'unfavorable': '错杀'} for k in SELL_SIDE_ATR_MULT},
    T_SELL_KIND: {'favorable': '可做成', 'unfavorable': '卖飞'},
    T_BUY_KIND: {'favorable': '可做成', 'unfavorable': '落空'},
    BUY_SIGNAL_KIND: {'favorable': '延续', 'unfavorable': '假突破'},
}

MIN_N_FOR_RATE = 20
LIMITATIONS = [
    '只用告警当天收盘前的观测价：不是最初设想的"次日开盘/收盘"，因为 15:20 跑的时候明天还没发生。',
    '标签只回答"这条提示本身有没有被验证"，不等于照做了会赚钱：滑点、流动性、你是否真的执行都不考虑在内。',
    'ATR 用于按每只股票自己的波动缩放判定门槛，和 book_levels.py 止损/止盈同源，但这里只是评分口径，'
    '不是新的交易规则；阈值都是起始默认值，没有前瞻验证。',
]

# 相邻两次触发的最短间隔（秒）。能引用 sentinel_rules.py 命名常量的都引用；below_cost/volume_surge(_down)
# 是该文件里 sig(...) 调用时直接传的内联字面量，这里同步硬编码——sentinel_rules.py 改了这几个数字，
# 这里不会自动跟着变，需要人工同步（见该文件 rearm_s= 相关行）。
REARM_BY_KIND = {
    'sentinel.below_cost': sr.LEVEL_REARM_S,
    'sentinel.stop_hit': sr.LEVEL_REARM_S,
    'sentinel.trail_stop_hit': sr.LEVEL_REARM_S,
    'sentinel.target_hit': sr.LEVEL_REARM_S,
    'sentinel.ma20_break': sr.LEVEL_REARM_S,
    'sentinel.ma60_break': sr.LEVEL_REARM_S,
    'sentinel.volume_surge_down': 1800,
    'sentinel.volume_surge': 1800,
    'sentinel.buy_signal': sr.LEVEL_REARM_S,
    'sentinel.intraday_support_reclaim': sr.INTRADAY_SIGNAL_REARM_S,
    'sentinel.intraday_resistance_reject': sr.INTRADAY_SIGNAL_REARM_S,
    'sentinel.t_sell_high': sr.T_REARM_S,
    'sentinel.t_buy_low': sr.T_REARM_S,
}
REARM_TOLERANCE_S = 5           # 时钟粒度容差
PRICE_MATCH_TOLERANCE_S = 120   # 告警文字的现价 vs bars 留存：两处时间戳来自不同的 now()，允许这么近


def sdir(directory=None):
    return scenario_ledger.sentinel_dir(directory)


def _intraday_dir(intraday_dir=None):
    return intraday_dir if intraday_dir is not None else ie.data_dir()


def _jsonl(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue           # 半行（写入中被读到）跳过，不让核对崩
    return out


# --- ① 机制核对 ---------------------------------------------------------------

def audit_day(directory, intraday_dir, day):
    """返回 {'checked': 引擎当天触发的哨兵事件数, 'mismatches': [{'key','why'}, ...]}。"""
    idir, d = _intraday_dir(intraday_dir), sdir(directory)
    engine_events = [e for e in _jsonl(idir / ('events-%s.jsonl' % day))
                     if e['kind'].startswith('sentinel.') and not e['kind'].startswith('sentinel.node_')]
    alerts = _jsonl(d / ('alerts-%s.jsonl' % day))
    bars_by_symbol = defaultdict(list)
    for b in _jsonl(idir / ('bars-%s.jsonl' % day)):
        if b.get('last') is not None:
            bars_by_symbol[b['symbol']].append(b)
    for rows in bars_by_symbol.values():
        rows.sort(key=lambda r: r['at'])

    mismatches = []

    # 1) 事件计数：engine 记了几次，alerts 的留档就该有几次——不多（重复）不少（丢事件）。
    engine_counts = Counter(e['key'] for e in engine_events)
    alert_counts = Counter()
    for rec in alerts:
        for e in rec.get('events') or []:
            alert_counts[e['key']] += 1
    for key, n in engine_counts.items():
        m = alert_counts.get(key, 0)
        if m != n:
            mismatches.append({'key': key, 'why': 'engine 触发 %d 次，alerts 留档 %d 次' % (n, m)})
    for key in alert_counts:
        if key not in engine_counts:
            mismatches.append({'key': key, 'why': 'alerts 留档里有、engine 事件里没有'})

    # 2) 价格一致性：告警文字里的现价必须和触发前后最近一次观测到的报价一致。
    for rec in alerts:
        at = _parse(rec.get('at'))
        for b in rec.get('blocks') or []:
            if b.get('price') is None or at is None:
                continue
            row = _closest_bar(bars_by_symbol.get(b['symbol']) or [], at)
            if row is None:
                continue
            try:
                if abs(float(row['last']) - float(b['price'])) > 1e-6:
                    mismatches.append({'key': b['symbol'], 'why': '告警价 %s 和最近一次观测价 %s 对不上（%s）' % (
                        b['price'], row['last'], rec['at'])})
            except (TypeError, ValueError):
                continue

    # 3) 推送/不推送一致性：watch 档不该出现在真正发出去的文本里；标了推送成功的必须真的在里面。
    for rec in alerts:
        text = rec.get('text') or ''
        sent = bool((rec.get('push') or {}).get('sent'))
        for b in rec.get('blocks') or []:
            pushed = b.get('pushed')
            if pushed is None:
                continue                      # 改造之前的旧留档没有这个字段，跳过
            in_text = bool(b.get('text')) and b['text'] in text
            if pushed and sent and not in_text:
                mismatches.append({'key': b['symbol'], 'why': '标了 pushed=true 但文本没有出现在这次推送里（%s）' % rec['at']})
            if not pushed and in_text:
                mismatches.append({'key': b['symbol'], 'why': '标了 pushed=false（watch 档）却出现在推送文本里（%s）' % rec['at']})

    # 4) 重新武装间隔：同一个 key 当天相邻两次触发的间隔不能小于它自己的重新武装间隔。
    kind_by_key = {}
    times_by_key = defaultdict(list)
    for e in engine_events:
        kind_by_key.setdefault(e['key'], e['kind'])
        times_by_key[e['key']].append(e['observed_at'])
    for key, times in times_by_key.items():
        rearm = REARM_BY_KIND.get(kind_by_key.get(key))
        if rearm is None or len(times) < 2:
            continue
        times = sorted(times)
        for prev, cur in zip(times, times[1:]):
            gap = (datetime.fromisoformat(cur) - datetime.fromisoformat(prev)).total_seconds()
            if gap < rearm - REARM_TOLERANCE_S:
                mismatches.append({'key': key, 'why': '相邻两次触发间隔 %.0f 秒，小于重新武装间隔 %d 秒（%s → %s）' % (
                    gap, rearm, prev, cur)})

    return {'checked': len(engine_events), 'mismatches': mismatches}


def _parse(iso):
    try:
        return datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def _closest_bar(rows, at):
    """rows 已按 at 升序排列。挑最近一条不晚于（或稍晚于，clock skew 双向都可能）at 的观测，
    超过 PRICE_MATCH_TOLERANCE_S 就当找不到——不能拿几分钟前的价格去质疑刚发生的告警。"""
    best, best_gap = None, None
    for r in rows:
        r_at = _parse(r['at'])
        if r_at is None:
            continue
        gap = abs((at - r_at).total_seconds())
        if best_gap is None or gap < best_gap:
            best, best_gap = r, gap
    if best is not None and best_gap <= PRICE_MATCH_TOLERANCE_S:
        return best
    return None


# --- ② 结果标签 ---------------------------------------------------------------

def _atr_for(history, symbol):
    """(atr_pct, nominal)。算不出来（日线不足15根）就退回典型 ATR，和 book_levels.exit_levels
    同一个口径，标 nominal=True。"""
    bars, _, _ = live_check.load_series(Path(history), symbol)
    dates = [b['date'] for b in bars]
    atr = shortterm_model.atr_pct({b['date']: b for b in bars}, dates) if len(dates) >= 15 else None
    if atr is not None and atr > 0:
        return atr, False
    return exec_spec.NOMINAL_ATR_PCT, True


def judge_sell_side(alert_price, atr_pct, mult, prices):
    """卖出/减仓类信号共享同一个问题：照这条提示卖了，后面是继续走弱（避损，验证该卖）
    还是大幅反弹（错杀，卖早了）？按时间顺序，谁先发生算谁。"""
    down, up = alert_price * (1 - mult * atr_pct), alert_price * (1 + mult * atr_pct)
    for p in prices:
        if p <= down:
            return 'favorable'
        if p >= up:
            return 'unfavorable'
    return None


def judge_t_sell(alert_price, atr_pct, prices):
    buyback = alert_price * (1 - (exec_spec.ROUND_TRIP_COST_PCT + T_BUYBACK_BUFFER_PCT) / 100)
    fail = alert_price * (1 + atr_pct)
    for p in prices:
        if p <= buyback:
            return 'favorable'
        if p >= fail:
            return 'unfavorable'
    return None


def judge_t_buy(alert_price, atr_pct, prices):
    buyback = alert_price * (1 + (exec_spec.ROUND_TRIP_COST_PCT + T_BUYBACK_BUFFER_PCT) / 100)
    fail = alert_price * (1 - atr_pct)
    for p in prices:
        if p >= buyback:
            return 'favorable'
        if p <= fail:
            return 'unfavorable'
    return None


def judge_buy_signal(alert_price, atr_pct, prices):
    up = alert_price * (1 + BUY_SIGNAL_ATR_MULT_UP * atr_pct)
    down = alert_price * (1 - BUY_SIGNAL_ATR_MULT_DOWN * atr_pct)
    for p in prices:
        if p >= up:
            return 'favorable'
        if p <= down:
            return 'unfavorable'
    return None


def judge_event(kind, alert_price, atr_pct, prices):
    if not prices:
        return None
    if kind in SELL_SIDE_ATR_MULT:
        return judge_sell_side(alert_price, atr_pct, SELL_SIDE_ATR_MULT[kind], prices)
    if kind == T_SELL_KIND:
        return judge_t_sell(alert_price, atr_pct, prices)
    if kind == T_BUY_KIND:
        return judge_t_buy(alert_price, atr_pct, prices)
    if kind == BUY_SIGNAL_KIND:
        return judge_buy_signal(alert_price, atr_pct, prices)
    return None


def label_day(directory, intraday_dir, history, day):
    """对当天所有**确认推送成功**、kind 在 LABELED_KINDS 里的事件打结果标签。
    未送达的（webhook 失败、找不到对应留档）排除——和 scenario_ledger 排除 delivered=False
    是同一个理由：你根本没看到，不该计入统计。"""
    idir, d = _intraday_dir(intraday_dir), sdir(directory)
    events = _jsonl(idir / ('events-%s.jsonl' % day))
    alerts = _jsonl(d / ('alerts-%s.jsonl' % day))
    bars_by_symbol = defaultdict(list)
    for b in _jsonl(idir / ('bars-%s.jsonl' % day)):
        if b.get('last') is not None:
            bars_by_symbol[b['symbol']].append(b)

    # (key, batch_sha256) 精确匹配到那一次告警是否真的送达——同一个 key 当天可能多次触发，
    # 每次触发对应一次不同的报价批次（batch_sha256），不会串档。
    delivered = {}
    for rec in alerts:
        sent = bool((rec.get('push') or {}).get('sent'))
        for e in rec.get('events') or []:
            sha = (e.get('evidence') or {}).get('batch_sha256')
            delivered[(e['key'], sha)] = sent

    atr_cache, rows = {}, []
    for ev in events:
        kind = ev['kind']
        if kind not in LABELED_KINDS or ev.get('severity') not in ('urgent', 'action'):
            continue
        sha = (ev.get('evidence') or {}).get('batch_sha256')
        if delivered.get((ev['key'], sha)) is not True:
            continue
        symbol = ev['symbol']
        try:
            alert_price = float(ev['price'])
        except (TypeError, ValueError):
            continue
        if symbol not in atr_cache:
            atr_cache[symbol] = _atr_for(history, symbol)
        atr_pct, nominal = atr_cache[symbol]
        prices = [float(r['last']) for r in bars_by_symbol.get(symbol, []) if r['at'] > ev['observed_at']]
        outcome = judge_event(kind, alert_price, atr_pct, prices)
        rows.append({'key': ev['key'], 'symbol': symbol, 'kind': kind, 'severity': ev['severity'],
                    'observed_at': ev['observed_at'], 'alert_price': alert_price, 'atr_pct': atr_pct,
                    'atr_nominal': nominal, 'bars_after': len(prices), 'outcome': outcome})
    return rows


def reconcile(directory, intraday_dir, history, day, now=None):
    now = now or datetime.now(CST)
    payload = {'day': day, 'reconciled_at': now.isoformat(),
               'audit': audit_day(directory, intraday_dir, day),
               'outcomes': label_day(directory, intraday_dir, history, day),
               'limitations': LIMITATIONS}
    d = sdir(directory)
    d.mkdir(parents=True, exist_ok=True)
    (d / ('alert_audit-%s.json' % day)).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    return payload


# --- 汇总与推送 ---------------------------------------------------------------

def _stats(rows):
    judged = [r for r in rows if r['outcome']]
    fav = sum(r['outcome'] == 'favorable' for r in judged)
    unfav = sum(r['outcome'] == 'unfavorable' for r in judged)
    return {'n': len(rows), 'concluded': len(judged), 'favorable': fav, 'unfavorable': unfav,
            'inconclusive': len(rows) - len(judged),
            'favorable_rate_pct': round(fav / len(judged) * 100, 1) if len(judged) >= MIN_N_FOR_RATE else None}


def summarize(rows):
    by_kind = defaultdict(list)
    for r in rows:
        by_kind[r['kind']].append(r)
    return {'overall': _stats(rows), 'by_kind': {k: _stats(v) for k, v in sorted(by_kind.items())},
            'limitations': LIMITATIONS}


def render_daily(day, audit, outcomes):
    import sentinel                       # 延迟 import：sentinel.py 顶部无条件 import fcntl
    s = summarize(outcomes)
    o = s['overall']
    fmt = lambda v: '样本不足，不给百分比' if v is None else '%s%%' % v
    lines = ['【哨兵日报 %s】' % day,
             '机制核对：%d 条已检查，%s。' % (audit['checked'],
                 '全部通过' if not audit['mismatches'] else '发现 %d 处不一致，见后台' % len(audit['mismatches'])),
             '结果标签：%d 条有结论（避损/可做成 %d、错杀/卖飞 %d），%d 条无结论。验证率：%s。' % (
                 o['concluded'], o['favorable'], o['unfavorable'], o['inconclusive'], fmt(o['favorable_rate_pct']))]
    parts = ['%s：n=%d %s' % (sentinel.kind_label(k), st['n'], fmt(st['favorable_rate_pct']))
            for k, st in s['by_kind'].items() if st['n']]
    if parts:
        lines.append('按类型：' + '；'.join(parts))
    lines.append(LIMITATIONS[0])
    return '\n'.join(lines)


def weekly(directory, intraday_dir, history, end_day, days=7):
    end = datetime.strptime(end_day, '%Y-%m-%d')
    span = [(end - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(days - 1, -1, -1)]
    rows, checked, mismatches = [], 0, 0
    for day in span:
        path = sdir(directory) / ('alert_audit-%s.json' % day)
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding='utf-8'))
        rows += data.get('outcomes', [])
        checked += data.get('audit', {}).get('checked', 0)
        mismatches += len(data.get('audit', {}).get('mismatches', []))
    return {'from': span[0], 'to': span[-1], 'checked': checked, 'mismatches': mismatches, **summarize(rows)}


def render_weekly(w):
    o = w['overall']
    fmt = lambda v: '样本不足，不给百分比' if v is None else '%s%%' % v
    lines = ['【哨兵告警周报 %s ~ %s】' % (w['from'], w['to']),
             '机制核对：%d 条，%s。' % (w['checked'], '全部通过' if not w['mismatches'] else '累计 %d 处不一致' % w['mismatches']),
             '结果标签：%d 条有结论（避损/可做成 %d、错杀/卖飞 %d），%d 条无结论；验证率：%s。' % (
                 o['concluded'], o['favorable'], o['unfavorable'], o['inconclusive'], fmt(o['favorable_rate_pct']))]
    lines += ['', LIMITATIONS[0], LIMITATIONS[1]]
    return '\n'.join(lines)


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['reconcile', 'weekly'])
    p.add_argument('--day', default=datetime.now(CST).strftime('%Y-%m-%d'))
    p.add_argument('--history', type=Path, default=Path(os.environ.get('HISTORY_DIR', '.history')))
    p.add_argument('--dir', type=Path)
    p.add_argument('--intraday-dir', type=Path)
    a = p.parse_args()
    if a.command == 'reconcile':
        r = reconcile(a.dir, a.intraday_dir, a.history, a.day)
        print('%s 机制核对 %d 条（不一致 %d）；结果标签 %d 条（有结论 %d）' % (
            a.day, r['audit']['checked'], len(r['audit']['mismatches']), len(r['outcomes']),
            sum(1 for x in r['outcomes'] if x['outcome'])))
    else:
        print(render_weekly(weekly(a.dir, a.intraday_dir, a.history, a.day)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
