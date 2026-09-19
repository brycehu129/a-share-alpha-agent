"""候选池的"三层证据标签"：固定标签 / 合约模拟标签 / 反事实。Python 3.9+，只用标准库。

每条冻结计划（前 ARCHIVE_SIZE 名全部，不只成交的 3 只）回答三个不同的问题，**互相不得冒充**：

1. **固定标签**（`alpha_model.label`，早已有）：次日开盘 → 第 3 个收盘，与买卖规则无关
   → 回答"**选股对不对**"。
2. **合约模拟标签**（本模块）：用这条计划**自己冻结的入场条件和退出规则**跑一遍——触发了没有？
   哪个退出先生效？最终净收益？→ 回答"**条件设得对不对**"。
3. **反事实**（本模块）：无视入场条件，次日开盘直接买，再套**同一套退出规则**
   → 与第 2 层对比，回答"**入场条件到底帮了忙还是添了乱**"；未触发的计划的反事实
   则回答"**条件是不是太严了**"。

**这些是研究标签，不是实际成交结果**：没有资金约束、没有名额限制、没有真实滑点和封板，
也没有"错过窗口"。实际的模拟成交在盘中引擎的账本里（exec-0.2 账户），两者分开统计。

**为什么必须坦白"日线的局限"**：入场/退出规则是盘中触发的，而这里只有日线 OHLC——一根 K 线
里先碰止损还是先碰止盈，日线无法回答（conditional_exec 的头号纪律就是"绝不假装日线能
还原盘中"）。所以：

- 日线能确定先后的情形直接判定（如开盘就跳过止损位——只可能在开盘价成交，且更差）。
- 日线**无法确定**的情形不猜：入场先后不明的（开盘在确认线下、当天既冲过确认线又跌破作废线）
  记为 `ambiguous`，不进统计、单独计数；退出先后不明的，**同时算悲观（先止损）和乐观（先止盈）
  两个结果**，报表里给区间，区间宽就是"日线说不清"的老实表达。
- 入场"09:30–14:00 时段"、涨停附近不买、账户持仓/名额/回撤暂停这些约束日线检验不了，
  一律不模拟，写在每条记录的 limitations 里。
- 成交价近似：开盘成交取开盘价；盘中穿越某价位触发的取该价位（真实成交是观测到的、
  不会更好）；成本统一按 exec_spec.ROUND_TRIP_COST_PCT 扣除。

价格基准：日线序列是前复权的，冻结记录里的参考价是未复权收盘价。这里所有价位都相对**序列里
截止日那根收盘**计算（同一基准，才能和后面的 K 线比较）；两者相差超过 0.5%（除权）的记录
标为 unusable，不硬算。

结果一经写出不可改写（immutable），和 outcomes/ 一样，放在 contract_labels/。
"""
import statistics
from collections import Counter, defaultdict

from exec_spec import ROUND_TRIP_COST_PCT

METHOD = 'daily-bar-v1'
ADJUST_TOLERANCE_PCT = 0.5
GATE = {'min_n': 30, 'min_cohorts': 15}      # 与 review_pipeline / alpha_model.estimate 同一条线

LIMITATIONS = [
    '日线无法确定同一根K线里先碰止损还是止盈：退出同时给悲观（先止损）与乐观（先止盈）两个结果。',
    '入场先后无法确定（当天既上穿确认线又跌破作废线）的记为 ambiguous，不进统计。',
    '不模拟 09:30–14:00 入场时段、涨停附近不买、持仓名额、账户回撤暂停：这些日线检验不了。',
    '盘中触发的成交价取触发价位，真实成交是观测价、不会更好；成本按往返 %.2f%% 统一扣除。' % ROUND_TRIP_COST_PCT,
    '这是研究标签，不是实际成交；实际模拟成交见 exec-0.2 账户。',
]


def _f(bar, key):
    return float(bar[key])


# --- 入场 -------------------------------------------------------------------

def simulate_entry(entry, ref, bar, ma20):
    """入场当天（首个允许交易日）能不能触发。返回 dict：
    state ∈ filled / not_filled / voided / ambiguous；filled 时带 fill 与 fill_kind(open|level)；
    没成交的带 near_miss_pct（离触发条件还差多少，用当天最极端的价格算）。"""
    o, h, l = _f(bar, 'open'), _f(bar, 'high'), _f(bar, 'low')
    lo_pct, hi_pct = entry['min_pct'], entry['max_pct']
    A = None if lo_pct is None else ref * (1 + lo_pct)
    C = ref * (1 + hi_pct)
    V = ref * (1 + entry['void_pct'])
    floor = V
    if entry['void_below_ma20']:
        if ma20 is None:
            return {'state': 'not_filled', 'reason': 'ma20_unavailable'}     # 与执行器一致：MA20 无法核验不成交
        floor = max(V, ma20)
    if A is None and floor > C:
        return {'state': 'not_filled', 'reason': 'zone_empty', 'near_miss_pct': round((floor / C - 1) * 100, 3)}

    if o <= V or (entry['void_below_ma20'] and o < ma20):
        return {'state': 'voided', 'reason': 'void_at_open'}
    if o > C:
        if l <= C and C >= floor:
            return {'state': 'filled', 'fill': round(C, 4), 'fill_kind': 'level', 'reason': 'pulled_back_into_zone'}
        return {'state': 'not_filled', 'reason': 'chase', 'near_miss_pct': round((l / C - 1) * 100, 3)}
    if A is None or o >= A:
        return {'state': 'filled', 'fill': round(o, 4), 'fill_kind': 'open', 'reason': 'in_zone_at_open'}
    # 开盘在确认线下、作废线上：先到哪个线，日线说了不算
    up, down = h >= A, l <= floor
    if up and down:
        return {'state': 'ambiguous', 'reason': 'crossed_both_confirm_and_void'}
    if up:
        return {'state': 'filled', 'fill': round(A, 4), 'fill_kind': 'level', 'reason': 'confirmed_up'}
    if down:
        return {'state': 'voided', 'reason': 'void_before_confirm'}
    return {'state': 'not_filled', 'reason': 'never_confirmed', 'near_miss_pct': round((A / h - 1) * 100, 3)}


# --- 退出 -------------------------------------------------------------------

def simulate_exit(exit_spec, fill, fill_kind, bar0, later, ma20, mode):
    """从 D0 成交之后逐日推演退出。mode: 'pess' 同日先止损，'opt' 同日先止盈并先武装保本。
    later = D1.. 的日线（可能不够长）。返回 None 表示还没走完（数据不够，别写记录）。

    规则镜像 conditional_exec：T+1（入场当天不能卖）；保本武装后止损上移；开盘价直接穿过
    止损/止盈位时以开盘价成交（不是位；跳空只会更差/更好）；第 1 个收盘 ≤ 入场价的 day1 规则
    在次日开盘生效；持满 hold_sessions 个交易日在第 hold_sessions+1 天开盘退出。"""
    stop_base = fill * (1 - exit_spec['stop_pct'])
    if exit_spec.get('stop_floor') == 'ma20_at_fill' and ma20:
        stop_base = max(stop_base, ma20)
    target = fill * (1 + exit_spec['target_pct'])
    arm_level = fill * (1 + exit_spec['breakeven_arm_pct'])
    be_level = fill * (1 + ROUND_TRIP_COST_PCT / 100)          # 近似；执行器用真实费用二分求解，差别在 0.1 个百分点内
    # 入场当天的高点能不能算"入场后到过"：开盘成交的当然算；触发价位成交的先后不明，只在乐观模式里算。
    armed = _f(bar0, 'high') >= arm_level and (fill_kind == 'open' or mode == 'opt')
    for k, bar in enumerate(later, start=1):
        o, h, l = _f(bar, 'open'), _f(bar, 'high'), _f(bar, 'low')
        stop = max(stop_base, be_level) if armed else stop_base
        stop_name = 'breakeven_stop' if armed and stop > stop_base else 'stop'
        if o <= stop:
            return {'reason': stop_name, 'price': o, 'k': k}
        if o >= target:
            return {'reason': 'target', 'price': o, 'k': k}
        if k == 1 and exit_spec['day1_close_rule'] and _f(bar0, 'close') <= fill:
            return {'reason': 'time_stop_day1', 'price': o, 'k': k}
        if k >= exit_spec['hold_sessions']:
            return {'reason': 'hold_expiry', 'price': o, 'k': k}
        hit_target = h >= target
        if mode == 'opt':
            if not armed and h >= arm_level:
                armed = True
                stop = max(stop_base, be_level)
                stop_name = 'breakeven_stop' if stop > stop_base else 'stop'
            if hit_target:
                return {'reason': 'target', 'price': target, 'k': k}
            if l <= stop:
                return {'reason': stop_name, 'price': stop, 'k': k}
        else:
            if l <= stop:
                return {'reason': stop_name, 'price': stop, 'k': k}
            if hit_target:
                return {'reason': 'target', 'price': target, 'k': k}
            if not armed and h >= arm_level:
                armed = True                                    # 悲观：当天武装不改变当天结果，从次日起生效
    return None


def _net(fill, price):
    return round((price / fill - 1) * 100 - ROUND_TRIP_COST_PCT, 4)


def _exit_pair(exit_spec, fill, fill_kind, bar0, later, ma20):
    """(悲观, 乐观) 两个退出；任一还没走完就返回 None。"""
    out = {}
    for mode in ('pess', 'opt'):
        result = simulate_exit(exit_spec, fill, fill_kind, bar0, later, ma20, mode)
        if result is None:
            return None
        out[mode] = {**result, 'price': round(result['price'], 4), 'net_pct': _net(fill, result['price'])}
    out['ambiguous'] = (out['pess']['reason'], out['pess']['price']) != (out['opt']['reason'], out['opt']['price'])
    return out


# --- 一条计划 -----------------------------------------------------------------

def label_plan(f, bars_by_date, dates):
    """返回记录（可写入）；数据还不够时返回 None（下次再算，不写任何东西）。"""
    spec = f.get('exec_spec')
    if not spec or 'entry' not in spec:
        return None
    as_of = f.get('as_of')
    if as_of not in bars_by_date or as_of not in dates:
        return None
    entry_day = next((d for d in dates if d >= f['eligible_from']), None)
    if entry_day is None or entry_day not in bars_by_date:
        return None
    i0 = dates.index(entry_day)
    hold = spec['exit']['hold_sessions']
    base = {'plan_id': f['id'], 'symbol': f['symbol'], 'track': f.get('strategy_type'), 'rank': f.get('rank'),
            'archive_only': bool(f.get('archive_only')), 'selection_version': f.get('selection_version') or f.get('version'),
            'execution_version': f.get('execution_version'), 'exec_spec_revision': spec.get('revision', 0),
            'as_of': as_of, 'entry_day': entry_day, 'method': METHOD, 'limitations': LIMITATIONS}
    ref = _f(bars_by_date[as_of], 'close')
    raw_ref = f.get('reference_price')
    if raw_ref and abs(ref / float(raw_ref) - 1) * 100 > ADJUST_TOLERANCE_PCT:
        return {**base, 'status': 'unusable', 'reason': '前复权序列与冻结参考价相差 %.2f%%（疑似除权），不硬算'
                % (abs(ref / float(raw_ref) - 1) * 100), 'ref_close': ref}
    i_as_of = dates.index(as_of)
    window = dates[max(0, i_as_of - 19):i_as_of + 1]
    ma20 = (sum(_f(bars_by_date[d], 'close') for d in window) / 20
            if len(window) == 20 and all(d in bars_by_date for d in window) else None)

    bar0 = bars_by_date[entry_day]
    later_dates = dates[i0 + 1:i0 + 1 + hold]
    later = []
    for d in later_dates:
        if d not in bars_by_date:
            break                     # 缺一天（停牌/数据缺口）就到此为止，不跳过去算：天数错位比没有标签更糟
        later.append(bars_by_date[d])

    entry = simulate_entry(spec['entry'], ref, bar0, ma20)
    contract = {'entry': entry}
    if entry['state'] == 'filled':
        pair = _exit_pair(spec['exit'], entry['fill'], entry['fill_kind'], bar0, later, ma20)
        if pair is None:
            return None
        contract['exit'] = pair
    # 反事实：无视入场条件，次日开盘直接买，同一套退出规则。
    fill_cf = _f(bar0, 'open')
    pair_cf = _exit_pair(spec['exit'], fill_cf, 'open', bar0, later, ma20)
    if pair_cf is None:
        return None
    return {**base, 'status': 'ok', 'ref_close': round(ref, 4), 'ma20': None if ma20 is None else round(ma20, 4),
            'contract': contract, 'counterfactual': {'fill': fill_cf, 'exit': pair_cf}}


def resolve_all(forecasts, series, benchmark, now, history, immutable, read):
    """给所有冻结计划补上还没算的标签；已写过的原样读回。immutable/read 由调用方传入
    （alpha_engine 的同名函数），避免循环导入。返回记录列表。"""
    dates = [b['date'] for b in benchmark]
    out = []
    folder = history / 'contract_labels'
    cache = {}
    for f in forecasts:
        if f.get('execution_mode') != 'conditional-intraday-v1' or not f.get('exec_spec'):
            continue                                            # 0.3 及更早的计划没有条件规格，不适用
        path = folder / (f['id'] + '.json')
        if path.exists():
            out.append(read(path))
            continue
        if f['symbol'] not in cache:
            cache[f['symbol']] = {b['date']: b for b in series.get(f['symbol'], [])}
        record = label_plan(f, cache[f['symbol']], dates)
        if record is None:
            continue
        record = {**record, 'generated_at': now.isoformat()}
        immutable(path, record)
        out.append(record)
    return out


# --- 汇总 -------------------------------------------------------------------

def _passes_gate(n, cohorts):
    return n >= GATE['min_n'] and cohorts >= GATE['min_cohorts']


def _band(rows, key_pess, key_opt, n, cohorts):
    if not rows or not _passes_gate(n, cohorts):
        return None
    pess, opt = [key_pess(r) for r in rows], [key_opt(r) for r in rows]
    return {'win_pess_pct': round(sum(v > 0 for v in pess) / n * 100, 1),
            'win_opt_pct': round(sum(v > 0 for v in opt) / n * 100, 1),
            'mean_pess_pct': round(statistics.mean(pess), 3), 'mean_opt_pct': round(statistics.mean(opt), 3)}


def _group(records, fixed_outcomes):
    n = len(records)
    cohorts = len({r['as_of'] for r in records})
    filled = [r for r in records if r['contract']['entry']['state'] == 'filled']
    not_triggered = [r for r in records if r['contract']['entry']['state'] in ('not_filled', 'voided')]
    ambiguous_entry = [r for r in records if r['contract']['entry']['state'] == 'ambiguous']
    resolved_entry = len(filled) + len(not_triggered)
    reasons = Counter(r['contract']['entry']['reason'] for r in not_triggered)
    coh = lambda rows: len({r['as_of'] for r in rows})
    fixed = [fixed_outcomes[r['plan_id']] for r in records if r['plan_id'] in fixed_outcomes]
    out = {
        'plans': n, 'cohorts': cohorts,
        'fixed': {'n': len(fixed), 'cohorts': len({o['entry_day'] for o in fixed}),
                  'win_rate_pct': (round(sum(o['win'] for o in fixed) / len(fixed) * 100, 1)
                                   if fixed and _passes_gate(len(fixed), len({o['entry_day'] for o in fixed})) else None),
                  'mean_return_pct': (round(statistics.mean(o['return_pct'] for o in fixed), 3)
                                      if fixed and _passes_gate(len(fixed), len({o['entry_day'] for o in fixed})) else None)},
        'contract': {'filled': len(filled), 'not_triggered': len(not_triggered), 'ambiguous_entry': len(ambiguous_entry),
                     'not_triggered_reasons': dict(reasons),
                     'fill_rate_pct': (round(len(filled) / resolved_entry * 100, 1)
                                       if resolved_entry and _passes_gate(resolved_entry, cohorts) else None),
                     'exit_ambiguous': sum(r['contract']['exit']['ambiguous'] for r in filled),
                     'exit_reasons_pess': dict(Counter(r['contract']['exit']['pess']['reason'] for r in filled)),
                     'result': _band(filled, lambda r: r['contract']['exit']['pess']['net_pct'],
                                     lambda r: r['contract']['exit']['opt']['net_pct'], len(filled), coh(filled))},
        'counterfactual': {'n': n,
                           'result': _band(records, lambda r: r['counterfactual']['exit']['pess']['net_pct'],
                                           lambda r: r['counterfactual']['exit']['opt']['net_pct'], n, cohorts),
                           'untriggered_n': len(not_triggered),
                           'untriggered_result': _band(not_triggered,
                                                       lambda r: r['counterfactual']['exit']['pess']['net_pct'],
                                                       lambda r: r['counterfactual']['exit']['opt']['net_pct'],
                                                       len(not_triggered), coh(not_triggered))},
    }
    return out


def _recent(r):
    c = r['contract']
    row = {'plan_id': r['plan_id'], 'symbol': r['symbol'], 'track': r['track'], 'rank': r['rank'],
           'entry_day': r['entry_day'], 'entry_state': c['entry']['state'], 'entry_reason': c['entry']['reason'],
           'near_miss_pct': c['entry'].get('near_miss_pct'),
           'cf_net_pess': r['counterfactual']['exit']['pess']['net_pct'],
           'cf_net_opt': r['counterfactual']['exit']['opt']['net_pct']}
    if c['entry']['state'] == 'filled':
        row.update(net_pess=c['exit']['pess']['net_pct'], net_opt=c['exit']['opt']['net_pct'],
                   exit_pess=c['exit']['pess']['reason'], exit_opt=c['exit']['opt']['reason'])
    return row


def summarize(records, outcomes, selection_version, execution_version=None):
    """按 track × 排名档（前 3 可成交 / 4–10 仅留档）汇总。

    只汇总**当前选股版本**（可选：当前执行版本）的记录；别的版本只给计数、不并入——
    版本不同规则就不同，混池算出的数字哪个版本都不代表。"""
    usable = [r for r in records if r.get('status') == 'ok']
    current = [r for r in usable if r['selection_version'] == selection_version
               and (execution_version is None or r['execution_version'] == execution_version)]
    fixed = {o['prediction_id']: o for o in outcomes if o.get('horizon') == 3}
    groups = defaultdict(list)
    for r in current:
        groups[(r['track'], 'top3' if not r['archive_only'] else 'rank4_10')].append(r)
    return {
        'method': METHOD, 'selection_version': selection_version, 'execution_version': execution_version,
        'total_records': len(records), 'unusable': len(records) - len(usable),
        'other_versions': len(usable) - len(current), 'gate': dict(GATE),
        'groups': {'%s/%s' % k: _group(v, fixed) for k, v in sorted(groups.items())},
        'recent': [_recent(r) for r in sorted(current, key=lambda r: (r['entry_day'], r['plan_id']), reverse=True)[:12]],
        'all_tracks': {tier: _group([r for r in current if (('top3' if not r['archive_only'] else 'rank4_10') == tier)], fixed)
                       for tier in ('top3', 'rank4_10')},
        'limitations': LIMITATIONS,
    }


TRACK_LABEL = {'breakout': '突破', 'pullback': '回调反弹'}
TIER_LABEL = {'top3': '前3（可成交）', 'rank4_10': '4–10名（仅留档）'}


def _rng(band, prefix=''):
    if band is None:
        return '样本不足'
    return '%+.2f%% ~ %+.2f%%（胜率 %.0f%%~%.0f%%）' % (band['mean_pess_pct'], band['mean_opt_pct'],
                                                     band['win_pess_pct'], band['win_opt_pct'])


def render(summary):
    """报告里的 Markdown 段落。样本未达门槛的比率一律不出数字，只列计数。"""
    lines = ['## 证据三层（候选池，研究标签）', '',
             '每条冻结计划（前10名全部）回答三个不同的问题：**固定标签**=选股对不对（次日开盘→第3个收盘）；'
             '**合约模拟**=用计划自己冻结的入场条件与退出规则跑一遍，条件设得对不对；**反事实**=无视条件、次日开盘直接买、套同一套退出，'
             '与合约对比看入场条件是帮忙还是添乱，未触发者的反事实看条件是不是太严。'
             '**研究标签，不是实际成交。** 日线分不清同一根K线里先止损还是先止盈，所以退出结果给 悲观~乐观 区间。'
             '比率和均值只在样本≥%d 且日期组≥%d 时显示，未达标只列计数（不代表零）。' % (GATE['min_n'], GATE['min_cohorts']), '',
             '选股 %s%s；共 %d 条标签记录%s。' % (
                 summary['selection_version'], ' · 执行 ' + summary['execution_version'] if summary.get('execution_version') else '',
                 summary['total_records'],
                 '，其中 %d 条因除权无法使用、%d 条属于其他版本（不并入）' % (summary['unusable'], summary['other_versions'])
                 if summary['unusable'] or summary['other_versions'] else ''), '']
    if not summary['groups']:
        lines += ['当前版本还没有到期的标签（计划冻结后要等入场日和持有期走完才出）。', '']
        return lines
    lines += ['| track | 档位 | 计划数 | 固定标签 n / 胜率 | 触发 / 未触发 / 先后不明 | 合约净收益 | 反事实净收益 | 未触发者的反事实 |',
              '|---|---|---:|---:|---:|---|---|---|']
    for key, g in summary['groups'].items():
        track, tier = key.split('/')
        c, cf, fx = g['contract'], g['counterfactual'], g['fixed']
        lines.append('| %s | %s | %d | %d / %s | %d / %d / %d | %s | %s | %s |' % (
            TRACK_LABEL.get(track, track), TIER_LABEL[tier], g['plans'], fx['n'],
            '%.0f%%' % fx['win_rate_pct'] if fx['win_rate_pct'] is not None else '样本不足',
            c['filled'], c['not_triggered'], c['ambiguous_entry'], _rng(c['result']),
            _rng(cf['result']), '%d 条：%s' % (cf['untriggered_n'], _rng(cf['untriggered_result'])) if cf['untriggered_n'] else '—'))
    lines.append('')
    reasons = Counter()
    for g in summary['groups'].values():
        reasons.update(g['contract']['not_triggered_reasons'])
    if reasons:
        names = {'never_confirmed': '始终没上穿确认线', 'chase': '开盘高于追高上限且全天没回落', 'void_at_open': '开盘即在作废线下',
                 'void_before_confirm': '先跌破作废线', 'ma20_unavailable': 'MA20无法核验', 'zone_empty': '入场区间为空'}
        lines += ['未触发原因：' + '；'.join('%s %d 条' % (names.get(k, k), v) for k, v in reasons.most_common()) + '。', '']
    lines += ['- ' + t for t in summary['limitations']] + ['']
    return lines
