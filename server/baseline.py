"""随机基线：策略选的票，比"随手抽的票"好多少？Python 3.9+，只用标准库。

**为什么必须有它。** 固定标签的"赢"是：次日开盘买入、第 3 个收盘卖出、扣 0.5% 成本后大于 0 **且跑赢沪深 300**。
随手抽一批合格股票，这个"赢"的**底数**本来就不是 50%——可能只有三四成。不知道底数，"胜率 45%"
是好是坏根本没法判断；证据三层只在策略内部互相比较（条件触发 vs 直接买），回答不了"选股本身
到底有没有比随机强"。这个模块补的就是这个对照。

**四条基线，每个截止日各抽 30 只：**

| 基线 | 抽样范围 | 回答什么 |
|---|---|---|
| `universe` | 全体合格股票（通过基础排除 + 窗口完整 + 流动性不在后 20%） | 策略比"随便买点什么"强多少 |
| `strong_industry` | 上面的范围里，只取行业强度前 10% 的行业成员 | 策略在**行业过滤之外**，靠突破/回调这套个股规则又多赚了多少 |
| `mid_industry` | 既不是强势行业、也不在最弱 10% 的行业 | 信息对照：行业强弱本身有没有预测力 |
| `weak_industry` | 行业强度最弱 10% 的行业成员 | 信息对照：短线是"强者恒强"还是"弱者反弹" |

用本地 35 个交易日回放：强势行业内随机抽样的 3 日超额是 −1.24pp，最弱 10% 行业是 +1.47pp——这段时间是**反转**行情，
"先选强势行业"这道闸门是负贡献。这只是一种行情下的 35 天，不能据此翻转策略，但值得**每天前瞻地记下去**：
`mid`/`weak` 两条基线就是为此加的（不参与主指标，只做信息对照）。

**两种"成功"的量法，都算：**
1. **屏障标签（主）**：次日开盘买入，按这只股票自己的 ATR 定止损/止盈（`exec_spec.neutral_exit`：止损 = ATR×倍数、止盈 = 止损×R、
   最长持有 5 个交易日），先碰哪个先出、到期收盘价出——和你说的"设好止损止盈，预测几天内达到什么价格"是同一把尺子。
   日线分不清同一根 K 线里先碰哪个，所以给悲观/乐观两个结果。策略前 10 名和随机抽样**套同一套出场规则**，才是同一把尺子。
2. **固定标签（次要）**：次日开盘 → 第 3 个收盘，扣 0.5% 成本、要跑赢沪深 300（`alpha_model.label`）。

**主指标在看数据之前就定死，防止事后挑对自己有利的那个数：**
`top10`（留档前 10 名）相对 `strong_industry` 基线，**屏障标签平均净收益（悲观口径，%）的逐日配对差值**。
（2026-09-20 用户明确了"成功 = 设好止损止盈、几天内达到目标价"，据此把主指标从固定标签超额改为屏障标签——
这是产品定义的变更，发生在任何前瞻数据出现之前，不是看了结果之后换的。）
胜率差、`top3`、其余基线、固定标签都是次要指标，报表里一并列出，但结论只看主指标。

**配对，不是汇总对比。** 同一个截止日，策略的 10 只和随机的 30 只面对的是同一天的市场；
逐日算差值、再看差值的均值和它的标准误，才能把"这几天行情好"从结论里扣掉。
同一天的票高度相关，所以标准误按**日期**算（不是按股票数），需要至少 15 个日期组才出区间；
相邻截止日的 3 日标签窗口互相重叠、逐日差值自相关，所以标准误做了 Newey–West 重叠校正（滞后 3）——
不校正的话有效样本数被高估约 4 倍，区间会显得比实际窄。
区间跨过 0 就是"目前无法区分策略与随机"——这是完全正常、也最常见的早期结果，不是故障。

**抽样必须可复现、且在结果已知之前冻结：** 随机种子由 (基线名, 截止日) 的哈希决定，先对代码排序再抽，
所以同一天重跑抽出同一批；抽出的名单在盘前选股那一轮就写盘（不可改写），标签等日线走完才算。

**两种记录，永远分开统计：**
- `forward`：每天日线流程里冻结、之后逐日验收的前瞻记录。这才是证据。
- `backfill`：用当前缓存的历史日线**回放**过去若干个截止日（策略选股 + 基线抽样在同一批历史数据上同时算），
  立刻就有数字，但和 `walk_forward_short` 一样带**幸存者/分类回看偏差**（当前名单、当前行业）——
  只能拿来对比，不能当作前瞻证据。策略和基线吃的是同一份有偏差的数据，所以对比本身大体公平，但绝对水平不可信。
"""
import hashlib
import json
import math
import random
import statistics
from collections import defaultdict

import contract_labels
import exec_spec
from alpha_model import POLICY, label, screen as screen_mid
from shortterm_model import ARCHIVE_SIZE, LIQUIDITY_MIN_PCT, atr_pct, stock_features, strong_industries

DRAW_N = 30
HORIZON = 3
HOLD = exec_spec.SPECS['breakout']['exit']['hold_sessions']     # 屏障标签的最长持有；验收要等它走完
MATURITY = max(HORIZON, HOLD)
GATE_COHORTS = 15
GATE_STRATEGY_N = 30
NAMES = {'universe': '全体合格股票', 'strong_industry': '强势行业内', 'mid_industry': '中间行业（对照）', 'weak_industry': '最弱10%行业（对照）'}
TIERS_FOR = {'universe': ('top10', 'top3'), 'strong_industry': ('top10', 'top3'), 'mid_industry': ('top10',), 'weak_industry': ('top10',)}
PRIMARY = ('top10', 'strong_industry', 'barrier_net_pess')       # 看数据之前就定死的主指标
METRICS = ('barrier_net_pess', 'barrier_net_opt', 'mean_excess_pp', 'win_rate_pct', 'mean_return_pct')
SEED_SALT = 'alpha-shadow-baseline-v1'


# --- 抽样 -------------------------------------------------------------------

def candidate_sets(stocks, series, benchmark, cutoff):
    """当天各条基线的抽样范围（代码列表，已排序）+ 当天的市场环境。"""
    feats = stock_features(stocks, series, benchmark, cutoff)
    ok = {c: r for c, r in feats.items() if r['liquidity_pct'] >= LIQUIDITY_MIN_PCT}
    mid = screen_mid(stocks, series, benchmark, cutoff)
    strong = strong_industries(mid['industries'])
    ranked = sorted(mid['industries'], key=lambda x: (-x['score'], x['name']))
    weak_names = {r['name'] for r in ranked[len(ranked) - max(1, math.ceil(len(ranked) * 0.1)):]} if ranked else set()
    sets = {'universe': sorted(ok),
            'strong_industry': sorted(c for c, r in ok.items() if r['industry'] in strong),
            'weak_industry': sorted(c for c, r in ok.items() if r['industry'] in weak_names and r['industry'] not in strong),
            'mid_industry': sorted(c for c, r in ok.items() if r['industry'] not in strong and r['industry'] not in weak_names)}
    return sets, {'market_score': mid['market_score'], 'regime': mid['regime'], 'complete': mid['complete']}


def draw(symbols, cutoff, name, n=DRAW_N):
    """可复现的随机抽样：种子只取决于 (基线名, 截止日)，与传入顺序无关。不足 n 只就全取。"""
    pool = sorted(symbols)
    if len(pool) <= n:
        return pool
    seed = int(hashlib.sha256(('%s|%s|%s' % (SEED_SALT, name, cutoff)).encode()).hexdigest(), 16)
    return sorted(random.Random(seed).sample(pool, n))


def entry_day_after(cutoff, dates):
    return next((d for d in dates if d > cutoff), None)


def barrier_outcome(bars_by_date, cutoff, dates):
    """屏障标签：次日开盘买入，套按这只股票 ATR 定的中性出场规则（没有 day1 规则）。走完返回 (悲观, 乐观, 是否有歧义)，
    没走完/数据缺一天/没有 ATR 返回 None。"""
    entry = entry_day_after(cutoff, dates)
    if entry is None:
        return None
    i0 = dates.index(entry)
    window = [d for d in dates if d <= cutoff]
    atr = atr_pct(bars_by_date, window)
    if atr is None:
        return None
    spec = exec_spec.neutral_exit(atr)
    hold = spec['hold_sessions']
    needed = dates[i0:i0 + hold + 1]
    if len(needed) < hold + 1 or any(d not in bars_by_date for d in needed):
        return None
    bar0 = bars_by_date[needed[0]]
    pair = contract_labels._exit_pair(spec, float(bar0['open']), 'open', bar0, [bars_by_date[d] for d in needed[1:]], None)
    return pair


def _barrier_aggregate(pairs):
    if not pairs:
        return {'barrier_n': 0, 'barrier_net_pess': None, 'barrier_net_opt': None, 'barrier_stop_pct': None,
                'barrier_target_pct': None, 'barrier_expiry_pct': None}
    n = len(pairs)
    share = lambda names: round(sum(p['pess']['reason'] in names for p in pairs) / n * 100, 1)
    return {'barrier_n': n, 'barrier_net_pess': round(statistics.mean(p['pess']['net_pct'] for p in pairs), 4),
            'barrier_net_opt': round(statistics.mean(p['opt']['net_pct'] for p in pairs), 4),
            'barrier_stop_pct': share(('stop', 'breakeven_stop')), 'barrier_target_pct': share(('target',)),
            'barrier_expiry_pct': share(('hold_expiry', 'time_stop_day1'))}


def label_draw(symbols, cutoff, series, benchmark):
    """给一批抽样标的打两种标签。窗口（取两者较长的）还没走完返回 None（别写记录）。"""
    dates = [b['date'] for b in benchmark]
    entry = entry_day_after(cutoff, dates)
    if entry is None or dates.index(entry) + MATURITY >= len(dates):
        return None
    labels, pairs = [], []
    for symbol in symbols:
        bars = series.get(symbol, [])
        outcome = label(bars, benchmark, entry, HORIZON)
        if outcome is not None:
            labels.append(outcome)
        pair = barrier_outcome({b['date']: b for b in bars}, cutoff, dates)
        if pair is not None:
            pairs.append(pair)
    result = {'entry_day': entry, 'end_day': dates[dates.index(entry) + MATURITY], 'n_drawn': len(symbols),
              'n_labeled': len(labels)}
    result.update(_aggregate(labels))
    result.update(_barrier_aggregate(pairs))
    return result


def _aggregate(labels):
    """labels: 含 win/return_pct/excess_pp 的字典列表（固定标签）。"""
    if not labels:
        return {'wins': 0, 'win_rate_pct': None, 'mean_return_pct': None, 'mean_excess_pp': None}
    return {'wins': sum(bool(x['win']) for x in labels),
            'win_rate_pct': round(sum(bool(x['win']) for x in labels) / len(labels) * 100, 3),
            'mean_return_pct': round(statistics.mean(x['return_pct'] for x in labels), 4),
            'mean_excess_pp': round(statistics.mean(x['excess_pp'] for x in labels), 4)}


# --- 汇总 -------------------------------------------------------------------

def strategy_by_cutoff(outcomes, forecasts, selection_version, series=None, benchmark=None):
    """策略当日留档的前 10 / 可成交的那几条：固定标签（3 日窗口，当前选股版本）+ 屏障标签（按截止日聚合）。

    屏障标签需要 series/benchmark（不传就只有固定标签）；同一批票、同一套中性出场规则，和随机抽样是同一把尺子。"""
    by_id = {f['id']: f for f in forecasts}
    days = defaultdict(lambda: {'top10': {'labels': [], 'pairs': []}, 'top3': {'labels': [], 'pairs': []}})

    def tradable(rank, archive_only):
        return (not archive_only) if archive_only is not None else (rank is not None and rank <= POLICY['max_positions'])

    for o in outcomes:
        f = by_id.get(o['prediction_id'])
        if o.get('horizon') != HORIZON or o.get('selection_version') != selection_version or not f or not f.get('as_of'):
            continue
        if o.get('rank') is None:
            continue
        days[f['as_of']]['top10']['labels'].append(o)
        if tradable(o['rank'], o.get('archive_only')):
            days[f['as_of']]['top3']['labels'].append(o)
    if series is not None and benchmark is not None:
        dates = [b['date'] for b in benchmark]
        cache = {}
        for f in forecasts:
            if selection_version != (f.get('selection_version') or f.get('version')) or f.get('rank') is None or not f.get('as_of'):
                continue
            if f['symbol'] not in cache:
                cache[f['symbol']] = {b['date']: b for b in series.get(f['symbol'], [])}
            pair = barrier_outcome(cache[f['symbol']], f['as_of'], dates)
            if pair is not None:
                days[f['as_of']]['top10']['pairs'].append(pair)
                if tradable(f['rank'], f.get('archive_only')):
                    days[f['as_of']]['top3']['pairs'].append(pair)
    return {c: {tier: {**_aggregate(g['labels']), 'n_labeled': len(g['labels']), **_barrier_aggregate(g['pairs'])}
                for tier, g in tiers.items()} for c, tiers in days.items()}


def _nw_variance_of_mean(values, lags):
    """Newey–West（Bartlett 权重）：相邻截止日的持有窗口互相重叠，逐日差值是自相关的——
    直接用 stdev/sqrt(n) 会把有效样本数高估好几倍、区间显得比实际窄。
    values 必须按截止日先后排序。"""
    n = len(values)
    mean = statistics.mean(values)
    dev = [v - mean for v in values]
    total = sum(d * d for d in dev) / n
    for k in range(1, min(lags, n - 1) + 1):
        cov = sum(dev[i] * dev[i - k] for i in range(k, n)) / n
        total += 2 * (1 - k / (lags + 1)) * cov
    return max(total, 0.0) / n


def _paired(diffs):
    """diffs：按截止日排序的逐日差值。返回均值与 95% 区间（标准误按日期、且做了重叠校正）。
    日期组不足门槛不出区间。"""
    k = len(diffs)
    out = {'days': k, 'mean': None, 'low': None, 'high': None, 'verdict': None}
    if k < GATE_COHORTS:
        return out
    mean = statistics.mean(diffs)
    se = math.sqrt(_nw_variance_of_mean(diffs, MATURITY))
    low, high = mean - 1.96 * se, mean + 1.96 * se
    out.update(mean=round(mean, 4), low=round(low, 4), high=round(high, 4),
               verdict='better' if low > 0 else 'worse' if high < 0 else 'indistinguishable')
    return out


def _mean_of(rows, metric):
    values = [r[metric] for r in rows if r.get(metric) is not None]
    return round(statistics.mean(values), 3) if values else None


def summarize(base_by_cutoff, strategy_days, kind):
    """base_by_cutoff: {(cutoff, 基线名): 抽样标签结果}；strategy_days: strategy_by_cutoff 的结果。

    返回：每条基线自己的底数（逐日平均），以及策略各档相对它的配对差值。日期组以"屏障标签有结果"为准。"""
    out = {'kind': kind, 'primary': {'tier': PRIMARY[0], 'baseline': PRIMARY[1], 'metric': PRIMARY[2]},
           'gate': {'min_days': GATE_COHORTS, 'min_strategy_n': GATE_STRATEGY_N}, 'baselines': {}}
    for name in NAMES:
        rows = {c: r for (c, n), r in base_by_cutoff.items() if n == name and r.get('barrier_n')}
        days = len(rows)
        entry = {'label': NAMES[name], 'days': days, 'labeled': sum(r['barrier_n'] for r in rows.values()),
                 'base_rate': None, 'vs': {}}
        if days >= GATE_COHORTS:
            entry['base_rate'] = {m: _mean_of(rows.values(), m) for m in METRICS}
            entry['base_rate'].update(stop_pct=_mean_of(rows.values(), 'barrier_stop_pct'),
                                      target_pct=_mean_of(rows.values(), 'barrier_target_pct'),
                                      expiry_pct=_mean_of(rows.values(), 'barrier_expiry_pct'))
        for tier in TIERS_FOR[name]:
            paired = {c: strategy_days[c][tier] for c in rows if c in strategy_days and strategy_days[c][tier].get('barrier_n')}
            n_strategy = sum(v['barrier_n'] for v in paired.values())
            block = {'days': len(paired), 'strategy_n': n_strategy, 'strategy': None}
            if len(paired) >= GATE_COHORTS and n_strategy >= GATE_STRATEGY_N:
                block['strategy'] = {m: _mean_of(paired.values(), m) for m in METRICS}
                block['strategy'].update(stop_pct=_mean_of(paired.values(), 'barrier_stop_pct'),
                                         target_pct=_mean_of(paired.values(), 'barrier_target_pct'),
                                         expiry_pct=_mean_of(paired.values(), 'barrier_expiry_pct'))
                for metric in METRICS:
                    both = [c for c in sorted(paired) if paired[c].get(metric) is not None and rows[c].get(metric) is not None]
                    block[metric] = _paired([paired[c][metric] - rows[c][metric] for c in both])
            entry['vs'][tier] = block
        out['baselines'][name] = entry
    primary = out['baselines'][PRIMARY[1]]['vs'].get(PRIMARY[0], {})
    out['primary_result'] = primary.get(PRIMARY[2])
    return out


# --- 前瞻：每天冻结抽样、逐日验收 -----------------------------------------------

def run_daily(history, stocks, series, benchmark, cutoff, complete, forecasts, outcomes, now, selection_version,
              immutable, read):
    """返回 report['baseline'] 的内容。冻结今天的抽样（complete 为真时；调用方在"本轮不冻结计划"时传 False），
    验收已走完窗口的历史抽样，汇总。"""
    folder = history / 'baselines'
    if complete:
        sets, env = candidate_sets(stocks, series, benchmark, cutoff)
        for name, symbols in sets.items():
            path = folder / ('%s-%s-%s.json' % (selection_version, cutoff, name))
            if path.exists() or not symbols:
                continue                                    # 同一天重跑：名单已冻结，不重抽；范围为空就没什么可抽
            immutable(path, {'kind': 'forward', 'cutoff': cutoff, 'name': name, 'selection_version': selection_version,
                             'symbols': draw(symbols, cutoff, name), 'universe_size': len(symbols),
                             'market_score': env['market_score'], 'regime': env['regime'], 'created_at': now.isoformat(),
                             'rule': '排序后按 sha256(盐|基线名|截止日) 做种子的 random.sample，每天 %d 只' % DRAW_N})
    results, frozen = {}, 0
    if folder.is_dir():
        for path in sorted(folder.glob('%s-*.json' % selection_version)):
            record = read(path)
            frozen += 1
            out_path = history / 'baseline_outcomes' / path.name
            if out_path.exists():
                result = read(out_path)
            else:
                labeled = label_draw(record['symbols'], record['cutoff'], series, benchmark)
                if labeled is None:
                    continue
                result = {'cutoff': record['cutoff'], 'name': record['name'], 'selection_version': selection_version,
                          **labeled, 'generated_at': now.isoformat()}
                immutable(out_path, result)
            results[(result['cutoff'], result['name'])] = result
    summary = summarize(results, strategy_by_cutoff(outcomes, forecasts, selection_version, series, benchmark), 'forward')
    summary.update(frozen=frozen, resolved=len(results), selection_version=selection_version)
    summary['backfill'] = summarize_backfill(history, read)
    return summary


# --- 回放：用历史缓存立刻得到一个有偏差的对照 ---------------------------------------

def load_world(history, read):
    """和 alpha_engine.run 同一套输入装载（含新源桥接），供回放用。返回 (stocks, series, benchmark)。"""
    root = history / 'alpha_data'
    master = read(history / 'tushare_data/stock_basic.json')
    bench_source = read(root / 'series/sh000300.json')
    bridge_path = root / 'bridge_latest.json'
    bridge = read(bridge_path) if bridge_path.exists() else None
    benchmark = list(bench_source['bars'])
    if bridge:
        benchmark = [b for b in benchmark if b['date'] <= bridge['cutoff']]
    stocks = [s for s in master['rows'] if s['exchange'] in ('SSE', 'SZSE') and s['list_status'] == 'L']
    series = {}
    for stock in stocks:
        code = stock['ts_code'][-2:].lower() + stock['ts_code'][:6]
        path = root / 'series' / (code + '.json')
        imported = root / 'tushare_series' / (code + '.json')
        if bridge and imported.exists():
            path = imported
        if path.exists():
            series[code] = read(path)['bars']
    return stocks, series, benchmark


def backfill(history, days, now, immutable, read, progress=None, world=None):
    """回放最近 days 个已能验收的截止日：策略选股与基线抽样在同一份历史数据上同时算，逐日冻结。
    可重复运行：已存在的日期跳过。**带幸存者/分类回看偏差，只做对照，不是前瞻证据。**
    注意：回放里的策略选股不含夜间公告核查（没有历史公告数据），也不含涨停收盘的成交名额规则。"""
    from alpha_engine import load_hotmoney
    from shortterm_model import screen_short, select_candidates
    stocks, series, benchmark = world or load_world(history, read)
    dates = [b['date'] for b in benchmark]
    last = len(dates) - MATURITY - 2                    # 需要 entry(+1) 和之后 MATURITY 个间隔都存在
    written = 0
    index = {}
    for i in range(max(22, last - days + 1), last + 1):
        cutoff = dates[i]
        path = history / 'baselines' / 'backfill' / ('%s.json' % cutoff)
        if path.exists():
            continue
        sets, env = candidate_sets(stocks, series, benchmark, cutoff)
        record = {'kind': 'backfill', 'cutoff': cutoff, 'market_score': env['market_score'], 'regime': env['regime'],
                  'universe_sizes': {n: len(s) for n, s in sets.items()}, 'baselines': {}, 'strategy': None,
                  'created_at': now.isoformat(),
                  'limitations': '用当前名单/行业回放，带幸存者与分类回看偏差；仅供对照，不是前瞻证据'}
        for name, symbols in sets.items():
            picked = draw(symbols, cutoff, name)
            record['baselines'][name] = {'symbols': picked, **(label_draw(picked, cutoff, series, benchmark) or {})}
        screened = screen_short(stocks, series, benchmark, cutoff, None, load_hotmoney(history, cutoff)[0])
        active = screened['complete'] and screened['market_score'] >= screened['market_score_pause']
        picks = []
        if active:
            for rank, c in enumerate(select_candidates(screened, ARCHIVE_SIZE), start=1):
                outcome = label(series[c['symbol']], benchmark, entry_day_after(cutoff, dates), HORIZON)
                if c['symbol'] not in index:
                    index[c['symbol']] = {b['date']: b for b in series[c['symbol']]}
                pair = barrier_outcome(index[c['symbol']], cutoff, dates)
                if outcome is not None and pair is not None:
                    picks.append({'symbol': c['symbol'], 'track': c['strategy_type'], 'rank': rank,
                                  'win': bool(outcome['win']), 'return_pct': outcome['return_pct'],
                                  'excess_pp': outcome['excess_pp'], 'barrier_pess': pair['pess']['net_pct'],
                                  'barrier_opt': pair['opt']['net_pct'], 'barrier_reason': pair['pess']['reason']})
        record['strategy'] = {'active': active, 'picks': picks}
        immutable(path, record)
        written += 1
        if progress:
            progress(cutoff, written)
    return written


def _picks_aggregate(picks):
    pairs = [{'pess': {'net_pct': p['barrier_pess'], 'reason': p['barrier_reason']}, 'opt': {'net_pct': p['barrier_opt']}}
             for p in picks]
    return {**_aggregate(picks), 'n_labeled': len(picks), **_barrier_aggregate(pairs)}


def summarize_backfill(history, read):
    folder = history / 'baselines' / 'backfill'
    if not folder.is_dir():
        return None
    base, strategy = {}, {}
    for path in sorted(folder.glob('*.json')):
        r = read(path)
        for name, b in r['baselines'].items():
            if b.get('barrier_n'):
                base[(r['cutoff'], name)] = b
        picks = [p for p in (r.get('strategy') or {}).get('picks') or [] if 'barrier_pess' in p]
        if picks:
            strategy[r['cutoff']] = {'top10': _picks_aggregate(picks),
                                     'top3': _picks_aggregate([p for p in picks if p['rank'] <= POLICY['max_positions']])}
    if not base:
        return None
    out = summarize(base, strategy, 'backfill')
    out['limitations'] = '回放带幸存者/分类回看偏差，只做对照，不是前瞻证据'
    return out


# --- 报告文字 -----------------------------------------------------------------

VERDICT_TEXT = {'better': '策略优于随机', 'worse': '策略不如随机', 'indistinguishable': '目前无法区分策略与随机', None: '样本不足'}


def _fmt(v, suffix=''):
    return '—' if v is None else '%+.2f%s' % (v, suffix)


def _net(rate):
    return '—' if not rate or rate.get('barrier_net_pess') is None else '%s ~ %s%%' % (_fmt(rate['barrier_net_pess']), _fmt(rate['barrier_net_opt']))


def render(summary):
    """Markdown。主指标单独一行放最前面，避免读者在一堆次要数字里挑自己喜欢的。"""
    lines = ['## 随机基线（策略选股 vs 随手抽样）', '',
             '"赢"的**底数不是 50%%**——不知道随手抽样能赚多少，就没法判断策略赚的是不是真本事。每个截止日各抽 %d 只，分四组：'
             '`全体合格股票`、`强势行业内`（策略在行业过滤之外又多赚多少）、以及信息对照的`中间行业`和`最弱10%%行业`（短线是强者恒强还是弱者反弹）。'
             '**主口径是"屏障标签"**：次日开盘买入，按每只股票自己的 ATR 定止损/止盈、最长持有 %d 个交易日，先碰哪个先出——'
             '策略前 10 名和随机抽样套**同一套出场规则**，逐日配对（同一天面对同一个市场）。日线分不清同一根K线里先碰止损还是止盈，'
             '所以给「悲观 ~ 乐观」两个结果。区间的标准误按日期算并做了重叠校正（相邻截止日的持有窗口互相重叠），需要至少 %d 个日期组；'
             '区间跨过 0 就是"目前无法区分"，这是早期最常见的结果，不是故障。'
             '**主指标（看数据之前就定死的）**：前10名相对「强势行业内」基线的平均净收益（悲观口径）逐日差值；其余都是次要指标。'
             % (DRAW_N, HOLD, GATE_COHORTS), '']
    for block, title in ((summary, '前瞻（每日冻结、逐日验收——这才是证据）'), (summary.get('backfill'), '历史回放（带幸存者/分类回看偏差，只做对照）')):
        if not block:
            lines += ['**%s**：暂无数据。' % title, '']
            continue
        primary = block['primary_result']
        head = '主指标：%s' % VERDICT_TEXT[(primary or {}).get('verdict')]
        if primary and primary['mean'] is not None:
            head += '（差值 %s pp，95%% 区间 [%s, %s]，%d 个日期组）' % (
                _fmt(primary['mean']), _fmt(primary['low']), _fmt(primary['high']), primary['days'])
        lines += ['**%s**' % title, '', head, '']
        lines += ['| 基线 | 日期组 | 底数：屏障净收益（悲观~乐观） | 策略档 | 策略：屏障净收益 | 净收益差值(悲观) [95%区间] | 结论 | 次要：固定标签超额差值(pp) |',
                  '|---|---:|---|---|---|---|---|---|']
        for name, b in block['baselines'].items():
            base = b['base_rate']
            base_text = ('%s；止损%.0f%%/止盈%.0f%%/到期%.0f%%' % (_net(base), base['stop_pct'], base['target_pct'], base['expiry_pct'])
                         if base else '样本不足（%d 个日期组，需要 %d）' % (b['days'], GATE_COHORTS))
            for tier, v in b['vs'].items():
                s = v['strategy']
                ex = v.get('barrier_net_pess') or {}
                fx = v.get('mean_excess_pp') or {}
                lines.append('| %s | %d | %s | %s | %s | %s | %s | %s |' % (
                    b['label'], b['days'], base_text, '前10名' if tier == 'top10' else '可成交的前3',
                    ('%s；止损%.0f%%/止盈%.0f%%/到期%.0f%%' % (_net(s), s['stop_pct'], s['target_pct'], s['expiry_pct'])) if s
                    else '样本不足（%d 个日期组、%d 条）' % (v['days'], v['strategy_n']),
                    ('%s [%s, %s]' % (_fmt(ex['mean']), _fmt(ex['low']), _fmt(ex['high']))) if ex.get('mean') is not None else '—',
                    VERDICT_TEXT[ex.get('verdict')], _fmt(fx.get('mean')) if fx.get('mean') is not None else '—'))
        lines.append('')
    lines += ['- 抽样在每天盘前的选股流程里冻结（种子=基线名+截止日，可复现、不可改写），标签等日线走完才算（要走完最长持有期，约 %d 个交易日后）。' % MATURITY,
              '- 同一天的股票高度相关，所以看日期组而不是股票数；多个基线×多个档×多个指标同时看，偶然"显著"的机会很多，'
              '所以结论只认上面那个预先定死的主指标。', '']
    return lines


def main(argv=None):
    import argparse
    import sys
    from datetime import datetime
    from pathlib import Path

    from alpha_engine import immutable
    from collect_quotes import CST
    from tushare_sync import read

    ap = argparse.ArgumentParser(description='随机基线：回放历史截止日，立刻得到一个（有偏差的）对照')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('backfill', help='回放最近 N 个已能验收的截止日（可重复运行，已存在的跳过）')
    p.add_argument('--history', type=Path, required=True)
    p.add_argument('--days', type=int, default=60)
    p = sub.add_parser('report', help='打印当前汇总（前瞻 + 回放）')
    p.add_argument('--history', type=Path, required=True)
    a = ap.parse_args(argv)
    now = datetime.now(CST)
    if a.cmd == 'backfill':
        written = backfill(a.history, a.days, now, immutable, read,
                           progress=lambda day, n: print('  %s 完成（第 %d 个）' % (day, n), file=sys.stderr, flush=True))
        print('回放完成：新写入 %d 个截止日' % written)
    summary = summarize_backfill(a.history, read)
    if summary is None:
        print('尚无回放数据')
        return 0
    empty = {'kind': 'forward', 'primary': summary['primary'], 'gate': summary['gate'], 'primary_result': None,
             'baselines': {n: {'label': NAMES[n], 'days': 0, 'labeled': 0, 'base_rate': None, 'vs': {}} for n in NAMES}}
    print('\n'.join(render({**empty, 'backfill': summary})))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
