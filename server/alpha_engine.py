"""Orchestrate screening, immutable forecasts, forward acceptance and shadow NAV."""
import argparse
import html
import json
import os
import re
import time as _time
from collections import Counter
from datetime import datetime, timedelta, time
from pathlib import Path
from alpha_data import raw_bars, symbol
from alpha_model import VERSION as MID_VERSION, POLICY as MID_POLICY, TARGET as MID_TARGET, screen, walk_forward, estimate, label
from alpha_portfolio import initial, advance
from collect_quotes import CST
from dashboard_export import latest
from hotmoney_features import load as load_hotmoney
from review_pipeline import current_tuning
import baseline
import contract_labels
from exec_spec import EXEC_MODE, breakeven_win_rate, build_spec, entry_zone, execution_version, sizing
from shortterm_model import (SELECTION_VERSION, ARCHIVE_SIZE, TARGET,
                              SHORT_POLICY as POLICY, screen_short,
                              select_candidates, walk_forward_short)
from tushare_sync import read, save, sha

# alpha-shadow-0.3-shortterm replaces the T+10 strategy going forward: VERSION/
# TARGET/POLICY above are the short-term (breakout/pullback) ones and drive
# every NEW forecast and the live account's stop/target/hold rules. The old
# mid-term names stay imported under MID_* purely so walk_forward()/estimate()
# keep producing the T+10 calibration line for continuity/audit, and so
# resolve() can still mature outcomes for predictions already frozen under
# alpha-shadow-0.2-observed. Nothing about those old records changes.
HORIZONS_BY_HOLD = {10: (5, 10, 20), 3: (1, 2, 3)}


def immutable(path, payload):
    if path.exists():
        if read(path) != payload:
            raise ValueError('Immutable record conflict: '+path.name)
        return
    save(path, payload)


def eligible_from(now):
    return (now.date() if now.time() < time(9, 20) else now.date()+timedelta(days=1)).isoformat()


def selection_version_of(f):
    """预测记录所属的选股版本。0.2/0.3 的旧记录没有 selection_version 字段，
    只有 version——当时选股和执行不分家，那个 version 就是它的选股版本标签。"""
    return f.get('selection_version') or f.get('version')


def tagged(outcome, f):
    """给验收记录补上版本与排名，只改内存里的副本，绝不回写磁盘（验收记录不可改写）。

    早先验收记录里根本没有版本字段，样本池只能靠 horizon 分组——0.2/0.3 碰巧一个
    10天一个3天才没混。0.4 仍是3天窗口，再靠 horizon 分就会把 0.3 和 0.4 混进
    同一个池子，而两版的筛选规则已经不同。"""
    outcome.setdefault('selection_version', selection_version_of(f))
    outcome.setdefault('execution_version', f.get('execution_version') or 'legacy')
    outcome.setdefault('rank', f.get('rank'))
    outcome.setdefault('archive_only', f.get('archive_only'))
    return outcome


def split_short_pools(outcomes, version=SELECTION_VERSION):
    """3天窗口的验收分成 (当前选股版本, 其他版本) 两池，只有前者参与当前版本的概率估计。

    不能只靠 horizon 分：0.3 和 0.4 窗口都是3天但筛选规则不同，混池算出的胜率
    哪个版本都不代表。其他版本的样本仍然保留计数（legacy），只是不参与估计。

    `version` 默认当前主策略的 SELECTION_VERSION；多策略并行时（见 server/strategies/），
    每个策略调一次、各自传自己的 STRATEGY_ID，样本池永不跨策略混合。"""
    live = [o for o in outcomes if o['horizon'] == 3 and o.get('selection_version') == version]
    other = [o for o in outcomes if o['horizon'] == 3 and o.get('selection_version') != version]
    return live, other


def cutoff_slots(forecasts, cutoff, version=SELECTION_VERSION):
    """该选股版本、该截止日已经留档了多少条、其中多少条允许模拟成交。
    同一截止日会被重跑好几次，靠这个给留档条数和成交名额设上限。

    `version` 同 split_short_pools：多策略并行时每个策略各自传自己的 id。"""
    same = [f for f in forecasts if selection_version_of(f) == version and f['as_of'] == cutoff]
    return len(same), sum(bool(f['paper_eligible']) for f in same)


def resolve(forecasts, series, benchmark, now, history):
    outcomes = []
    dates = [b['date'] for b in benchmark]
    for f in forecasts:
        if not dates or f['eligible_from'] < dates[0]:
            # A rolling provider window cannot redefine an old forecast's entry.
            outcomes.extend(tagged(read(p), f) for p in (history / 'outcomes').glob(f['id']+'-*.json'))
            continue
        entry = next((d for d in dates if d >= f['eligible_from']), None)
        if entry is None:
            continue
        horizons = HORIZONS_BY_HOLD.get(f.get('policy', {}).get('hold_sessions'), (5, 10, 20))
        for horizon in horizons:
            path = history / 'outcomes' / (f['id']+'-'+str(horizon)+'.json')
            if path.exists():
                outcomes.append(tagged(read(path), f))
                continue
            outcome = label(series.get(f['symbol'], []), benchmark, entry, horizon)
            if outcome is not None:
                # Attribution is a measurable diagnostic, never a fabricated
                # percentage allocation or a causal explanation from prices alone.
                diagnostic = ('达成目标' if outcome['win'] else '基准同期下跌' if outcome['benchmark_pct'] < 0
                              else '标的未取得净正收益或未跑赢基准')
                record = {'prediction_id': f['id'], 'generated_at': now.isoformat(), **outcome,
                          'selection_version': selection_version_of(f),
                          'execution_version': f.get('execution_version') or 'legacy',
                          'rank': f.get('rank'),
                          'bucket': f['bucket'], 'regime': f['regime'], 'probability_at_issue': f['probability']['probability'],
                          'archive_only': bool(f.get('archive_only')),
                          'diagnostic': diagnostic, 'causal_attribution': '待复核，不能仅凭收益判定原因'}
                immutable(path, record)
                outcomes.append(record)
    return outcomes


def exec_fields(track, exec_revision=None, atr_pct=None):
    """冻结进每条预测的执行侧字段。exec_revision = (修订号, {track: {参数: 值}})，来自已被人批准的
    提议（proposals.active()）；不传 = exec-0.3 原始规格。atr_pct（小数）用来把止损/止盈/追高上限
    解析成这只股票自己的数字。规格在这里深拷贝进记录，此后改常量或批准新修订都不会影响这条记录。"""
    revision, overrides = exec_revision or (0, {})
    return {'execution_version': execution_version(revision), 'execution_mode': EXEC_MODE,
            'exec_spec': build_spec(track, overrides.get(track), revision, atr_pct)}


NEWS_CHECK_N = 25          # 排名前多少只做夜间公告核查（要留出被剔除后递补的余量）


def closed_limit_up(raw_bars_of_symbol, cutoff, symbol):
    """截止日收盘是不是涨停（未复权价、按板块涨跌幅限制估算，留 0.15 个百分点容差）。
    涨停收盘的票次日大概率一开盘就封死、买不到——留档做研究，但不占成交名额。缺前一日数据就判为"不是"（不确定不剔除）。"""
    bars = raw_bars_of_symbol or []
    idx = next((i for i, b in enumerate(bars) if b['date'] == cutoff), None)
    if idx is None or idx == 0:
        return False
    prev_close, close = float(bars[idx - 1]['close']), float(bars[idx]['close'])
    if prev_close <= 0:
        return False
    limit = 0.20 if symbol.startswith(('sz30', 'sh688')) else 0.10
    return close / prev_close - 1 >= limit - 0.0015


def plan_levels(spec, reference_price, policy):
    """买入信号卡片用的具体数字：入场区间、作废价、止损/目标（以参考价为基准估算，实际以成交价计）、最长持有、仓位。"""
    lo, hi, void = entry_zone(spec['entry'], reference_price)
    exit_spec = spec['exit']
    return {'entry_low': None if lo is None else round(lo, 3), 'entry_high': round(hi, 3), 'void_price': round(void, 3),
            'stop_pct': exit_spec['stop_pct'], 'target_pct': exit_spec['target_pct'],
            'stop_price_at_reference': round(reference_price * (1 - exit_spec['stop_pct']), 3),
            'target_price_at_reference': round(reference_price * (1 + exit_spec['target_pct']), 3),
            'hold_sessions': exit_spec['hold_sessions'], 'atr_pct': spec.get('atr_pct'),
            'window': spec['entry']['window'], 'sizing': sizing(exit_spec, policy),
            'breakeven_win_rate_pct': round(breakeven_win_rate(exit_spec), 1)}


def run(history, run_id, exec_revision=None, exec_account=None, announce_fn=None, freeze=True):
    """exec_revision = (修订号, {track: {参数: 值}})，来自已被人批准的提议（proposals.active()）。
    不传 = exec-0.3 原始规格。exec_account = conditional_exec.read_ledger() 读到的 exec 账本（或 None）。
    announce_fn(symbols, since, now) -> {symbol: {'status', 'items', ...}}：夜间公告核查（announcements.check_many）；
    不传 = 不核查（测试/离线），此时计划里不带公告核查结果。
    freeze=False：只更新验收/估值/证据，**不新冻结计划**（收盘那两轮用：选股改在盘前，见下）。
    本函数不自己读提议文件/账本/网络：由 __main__ 传入，测试因此不受服务器状态影响。

    **选股时点**：计划在盘前（08:40）用昨收数据 + 昨晚到清晨发布的公告冻结，而不是收盘后（15:35）——
    收盘后冻结的计划要带着一整夜的未知消息去开盘，而且当天晚些时候才同步的游资/龙虎榜数据也用不上。"""
    exec_version = execution_version((exec_revision or (0, {}))[0])
    now = datetime.now(CST)
    root = history / 'alpha_data'
    # 08:40 流程必须在 09:20 前跑完（否则当天没有计划，健康检查会报警），耗时从未实测过。
    # 这套埋点是加多策略之前的第一步：先量清楚现在单策略花多少时间，再决定能塞几个策略。
    _timing_t0 = _time.perf_counter()
    _timing_last = [_timing_t0]
    timing = {}

    def _lap(name):
        t = _time.perf_counter()
        timing[name] = round((t - _timing_last[0]) * 1000)
        _timing_last[0] = t

    report = {'id': run_id, 'version': SELECTION_VERSION, 'selection_version': SELECTION_VERSION,
              'execution_version': exec_version, 'archive_size': ARCHIVE_SIZE,
              'generated_at': now.isoformat(), 'status': 'waiting_data',
              'target': TARGET, 'policy': POLICY, 'mid_policy': MID_POLICY, 'issues': [], 'screen': None,
              'candidates': [], 'calibration': None, 'calibration_short': None, 'portfolio': None,
              'forecasts': [], 'outcomes': [], 'source_hashes': {}, 'evidence': None, 'exec02': None,
              'baseline': None, 'news_check': None, 'new_forecast_ids': [], 'timing': timing}
    master_path = history / 'tushare_data/stock_basic.json'
    benchmark_path = root / 'series/sh000300.json'
    previous_path, previous = latest(history, 'agent')
    tuning, _ = current_tuning(history)
    if not master_path.exists() or not benchmark_path.exists():
        report['issues'].append('等待股票清单和沪深300历史日线；不补造候选或成交。')
        report['portfolio'] = previous.get('portfolio') if previous else None
        timing['total'] = round((_time.perf_counter() - _timing_t0) * 1000)
        return report
    master, bench_source = read(master_path), read(benchmark_path)
    report['source_hashes']['stock_basic'] = sha(master)
    report['source_hashes']['sh000300'] = sha(bench_source)
    today = now.date().isoformat()
    bridge_path = root / 'bridge_latest.json'
    bridge = read(bridge_path) if bridge_path.exists() else None
    if bridge:
        report['source_hashes']['bridge'] = sha(bridge)
    benchmark = [b for b in bench_source['bars'] if b['date'] < today or (b['date'] == today and now.time() >= time(15, 10))]
    if len(benchmark) < 21:
        report['issues'].append('基准不足21个已结束交易日。')
        report['portfolio'] = previous.get('portfolio') if previous else None
        timing['total'] = round((_time.perf_counter() - _timing_t0) * 1000)
        return report
    if bridge:
        benchmark = [b for b in benchmark if b['date'] <= bridge['cutoff']]
        if len(benchmark) < 21:
            report['issues'].append('新源与基准共同窗口不足21日。')
            report['portfolio'] = previous.get('portfolio') if previous else None
            timing['total'] = round((_time.perf_counter() - _timing_t0) * 1000)
            return report
    cutoff = benchmark[-1]['date']
    stocks = [s for s in master['rows'] if s['exchange'] in ('SSE', 'SZSE') and s['list_status'] == 'L']
    series = {}
    for stock in stocks:
        code = symbol(stock['ts_code'])
        path = root / 'series' / (code+'.json')
        imported = root / 'tushare_series' / (code+'.json')
        if bridge and imported.exists():
            path = imported
        if path.exists():
            data = read(path)
            report['source_hashes'][code] = sha(data)
            series[code] = [b for b in data['bars'] if b['date'] <= cutoff]
    hotmoney, hotmoney_availability = load_hotmoney(history, cutoff)
    if not hotmoney_availability['hm_detail']:
        report['issues'].append('本轮暂无游资明细数据（尚未同步到当日），打分未包含游资净买入维度。')
    if not hotmoney_availability['limit_list_d']:
        report['issues'].append('本轮暂无涨跌停数据（尚未同步到当日），突破track打分未包含涨停确认维度。')
    _lap('data_load')
    screened = screen_short(stocks, series, benchmark, cutoff, tuning, hotmoney)
    report['screen'] = screened
    _lap('screen')
    stale = (now - datetime.fromisoformat(master['fetched_at'])).days > 7 or (now - datetime.fromisoformat(bench_source['fetched_at'])).total_seconds() > 86400 or (now.date()-datetime.fromisoformat(cutoff).date()).days > 5
    if stale:
        report['issues'].append('股票清单超过7日、基准采集超过24小时或行情日期距今超过5日，只展示研究，不产生新虚拟计划。')
    if not screened['complete']:
        report['issues'].append('有效日线覆盖不足95%，候选仅供观察，本轮不建立虚拟入场计划。')
        _, scan = latest(history, 'alpha_data/runs')
        if scan:
            failures = Counter(x.get('error','未知错误') for x in scan['requests'] if x['status']=='failed')
            report['issues'].extend(f'最近腾讯扫描：{reason}（{count}只）' for reason,count in failures.most_common(3))
        _, backup = latest(history, 'alpha_data/fallback_runs')
        if backup:
            report['issues'].extend('备用源：'+e for e in backup.get('errors',[])[:3])
    if screened['market_score'] < screened['market_score_pause']:
        report['issues'].append(f'市场趋势评分低于{screened["market_score_pause"]}，暂停新虚拟计划。')
    # T+10版本(alpha-shadow-0.2-observed)校准继续跑，留作历史对照，不再驱动新预测。
    calibration = walk_forward(stocks, series, benchmark, cutoff, tuning)
    calibration_short = walk_forward_short(stocks, series, benchmark, cutoff, tuning,
                                            hotmoney_loader=lambda d: load_hotmoney(history, d)[0])
    _lap('calibration')
    forecasts = [read(p) for p in sorted((history / 'predictions').glob('*.json'))]
    outcomes = resolve(forecasts, series, benchmark, now, history)
    _lap('resolve')
    live_mid = [o for o in outcomes if o['horizon'] == 10]
    # 三层证据里的后两层（合约模拟/反事实）。这是**可选的研究层**：出任何问题都只记一条 issue，
    # 绝不能让必需的候选生成跟着失败。
    try:
        contract_records = contract_labels.resolve_all(forecasts, series, benchmark, now, history, immutable, read)
        report['evidence'] = contract_labels.summarize(contract_records, outcomes, SELECTION_VERSION, exec_version)
    except Exception as exc:
        report['evidence'] = None
        report['issues'].append('证据三层（合约模拟/反事实）本轮计算失败，已跳过：%s: %s' % (type(exc).__name__, str(exc)[:120]))
    _lap('evidence')
    # 随机基线：同样是可选研究层，出错只记 issue。抽样在这里冻结（盘前，用昨收数据），标签等日线走完才算。
    try:
        report['baseline'] = baseline.run_daily(history, stocks, series, benchmark, cutoff, screened['complete'] and freeze,
                                                forecasts, outcomes, now, SELECTION_VERSION, immutable, read)
    except Exception as exc:
        report['baseline'] = None
        report['issues'].append('随机基线本轮计算失败，已跳过：%s: %s' % (type(exc).__name__, str(exc)[:120]))
    _lap('baseline')
    # 3天窗口的验收按**选股版本**再切一刀，不能只看 horizon：0.3 和 0.4 窗口相同，
    # 但筛选规则不同，混进一个池子算出的胜率哪个版本都不代表。
    live_short, legacy_short = split_short_pools(outcomes)
    report['calibration'] = {k: v for k, v in calibration.items() if k != 'samples'}
    report['calibration'].update(historical_n=len(calibration['samples']), live_n=len(live_mid),
                                live_win_rate=round(sum(o['win'] for o in live_mid)/len(live_mid)*100, 2) if live_mid else None)
    report['calibration_short'] = {k: v for k, v in calibration_short.items() if k != 'samples'}
    report['calibration_short'].update(historical_n=len(calibration_short['samples']), live_n=len(live_short),
                                       legacy_live_n=len(legacy_short), selection_version=SELECTION_VERSION)
    for track in ('breakout', 'pullback'):
        live_t = [o for o in live_short if o['bucket'] == track]
        report['calibration_short'][f'live_win_rate_{track}'] = round(sum(o['win'] for o in live_t)/len(live_t)*100, 2) if live_t else None
    all_short_candidates = screened['breakout']['candidates'] + screened['pullback']['candidates']
    for c in all_short_candidates:
        live_p = estimate(live_short, today, c['strategy_type'], screened['regime'])
        historical_p = estimate(calibration_short['samples'], cutoff, c['strategy_type'], screened['regime'])
        c['probability'] = live_p if live_p['probability'] is not None else historical_p
        c['probability']['source'] = '前瞻留档样本' if live_p['probability'] is not None else '有回看偏差的历史研究'
        if live_p['probability'] is not None:
            c['probability']['status'] = 'forward_empirical'
    # 展示列表和留档/成交用同一套排名（两条 track 各自按原始分转百分位再合并，
    # 见 shortterm_model.select_candidates 的 docstring）。展示排前面的，就是实际
    # 会被留档、被允许成交的那批——两处排序不一致只会让人对不上账。
    ranked_all = select_candidates(screened, max(len(all_short_candidates), 1))
    # 夜间公告核查：只在真的要冻结计划时做（要联网），只查排名靠前的一批；命中高风险关键词的直接剔除、
    # 由后面的候选递补——它不占名额，也不进留档（选股规则的一部分，见 select-0.5）。取不到的标"未核验"，不剔除也不当作没事。
    news = {}
    if freeze and announce_fn is not None and ranked_all:
        since = datetime.fromisoformat(cutoff + 'T15:00:00+08:00')
        try:
            news = announce_fn([c['symbol'] for c in ranked_all[:NEWS_CHECK_N]], since, now) or {}
        except Exception as exc:
            report['issues'].append('夜间公告核查整体失败，本轮所有候选标记为"公告未核验"：%s: %s' % (type(exc).__name__, str(exc)[:100]))
    for c in ranked_all:
        c['news'] = news.get(c['symbol'])
    blocked = [c for c in ranked_all if (c.get('news') or {}).get('status') == 'block']
    ranked_all = [c for c in ranked_all if (c.get('news') or {}).get('status') != 'block']
    report['news_check'] = None if not (freeze and announce_fn is not None) else {
        'since': cutoff + 'T15:00:00+08:00', 'checked': len(news),
        'clear': sum(v['status'] == 'clear' for v in news.values()), 'flagged': sum(v['status'] == 'flag' for v in news.values()),
        'unverified': sum(v['status'] == 'unverified' for v in news.values()),
        'blocked': [{'symbol': c['symbol'], 'name': c['name'], 'strategy_type': c['strategy_type'],
                     'items': [i for i in c['news']['items'] if i['level'] == 'block'][:3]} for c in blocked]}
    report['candidates'] = ranked_all[:30]
    # 前 ARCHIVE_SIZE 名全部留档做研究，其中最多 max_positions 名允许模拟成交（收盘涨停的除外）。
    ranked = ranked_all[:ARCHIVE_SIZE]
    old_state = previous.get('portfolio') if previous else None
    codes = {c['symbol'] for c in ranked} if freeze else set()
    codes.update(p['symbol'] for p in (old_state or {}).get('positions', []))
    codes.update(f['symbol'] for f in forecasts if f['paper_eligible'] and f['id'] not in (old_state or {}).get('attempted', []))
    # Keep delisted/missing master positions in the valuation path. A missing
    # series must block valuation rather than disappear from the account.
    for code in codes-set(series):
        path = root / 'series' / (code+'.json')
        if path.exists():
            series[code] = [b for b in read(path)['bars'] if b['date'] <= cutoff]
    raw = raw_bars(history, codes, now, cutoff=cutoff)
    immutable(history / 'execution_inputs' / (run_id+'.json'), {'generated_at': now.isoformat(), 'series': raw})
    raw_series = {s: [b for b in d['bars'] if b['date'] <= cutoff] for s, d in raw.items()}
    old_state = old_state or initial(cutoff, float(benchmark[-1]['close']), policy=POLICY)
    report['portfolio'] = advance(old_state, forecasts, raw_series, series, benchmark, cutoff, execute=False, policy=POLICY)
    report['issues'].extend(report['portfolio']['issues'])
    _lap('raw_bars_and_portfolio')
    report['exec02'] = None
    if exec_account is not None:
        try:
            import conditional_exec
            prior = ((previous or {}).get('exec02') or {}).get('curve', [])
            report['exec02'] = conditional_exec.account_snapshot(exec_account, now, prior)
        except Exception as exc:
            report['issues'].append('盘中条件执行账户快照失败，已跳过：%s: %s' % (type(exc).__name__, str(exc)[:120]))
    created = datetime.now(CST)  # Actual freeze time after all requests, not job start.
    # 同一个截止日一天里会被重跑好几次（整点任务 + 每小时的补偿检查）。每次重跑排名都
    # 可能因为数据补齐而略有不同，不设上限的话，同一天留档条数会悄悄超过 ARCHIVE_SIZE，
    # 允许成交的计划也会超过 max_positions。这里按"该选股版本、该截止日"数已有的。
    archived_n, paper_n = cutoff_slots(forecasts, cutoff)
    trade_slots = POLICY['max_positions']
    for rank, c in enumerate(ranked if freeze else [], start=1):
        ref = next((b for b in raw_series.get(c['symbol'], []) if b['date'] == cutoff), None)
        identity = SELECTION_VERSION + '-' + cutoff + '-' + c['symbol']
        path = history / 'predictions' / (identity+'.json')
        if path.exists():
            continue
        if archived_n >= ARCHIVE_SIZE:
            report['issues'].append('%s 已留档 %d 条，达到上限，本轮不再新增。' % (cutoff, archived_n))
            break
        if ref is None:
            report['issues'].append(c['symbol']+' 未获得未复权参考价，不生成预测记录。')
            continue
        pause_threshold = screened['market_score_pause']
        can_trade = screened['complete'] and not stale and screened['market_score'] >= pause_threshold and report['portfolio']['valuation_status'] == 'current' and not report['portfolio']['paused']
        plan_reasons = []
        if not screened['complete']: plan_reasons.append('有效日线覆盖不足95%')
        if stale: plan_reasons.append('数据超过允许时效')
        if screened['market_score'] < pause_threshold: plan_reasons.append(f'市场评分低于{pause_threshold}')
        if report['portfolio']['valuation_status'] != 'current': plan_reasons.append('虚拟账户估值暂停')
        if report['portfolio']['paused']: plan_reasons.append('账户回撤风控暂停')
        atr = c.get('atr14')
        if atr is None:
            plan_reasons.append('ATR 无法计算，不能定止损：仅研究留档')
        limit_up = closed_limit_up(raw_series.get(c['symbol']), cutoff, c['symbol'])
        if limit_up:
            plan_reasons.append('截止日收盘涨停：次日大概率一开盘就封死、买不到，仅研究留档、不占成交名额')
        slots_full = paper_n >= trade_slots
        if slots_full and not limit_up:
            plan_reasons.append('仅研究留档：前%d个可成交名额已被排名更靠前的计划占用' % trade_slots)
        archive_only = limit_up or slots_full or atr is None
        can_trade = can_trade and not archive_only
        news_c = c.get('news')
        if news_c and news_c['status'] == 'unverified':
            plan_reasons.append('夜间公告未核验（%s）：请自行看一眼公告' % (news_c.get('error') or '接口不可用'))
        elif news_c and news_c['status'] == 'flag':
            plan_reasons.append('夜间有需要留意的公告（未达到剔除标准），见 news')
        spec_fields = exec_fields(c['strategy_type'], exec_revision, atr)
        levels = plan_levels(spec_fields['exec_spec'], float(ref['close']), POLICY)
        forecast = {'id': identity, 'version': SELECTION_VERSION, 'selection_version': SELECTION_VERSION,
            'rank': rank, 'rank_pct': c.get('rank_pct'),
            'archive_only': archive_only, 'created_at': created.isoformat(), 'as_of': cutoff,
            'plan_reasons': plan_reasons,
            # 条件触发入场；规格在这里原样冻结进记录，执行器只读记录里的这份，以后改常量
            # 不会悄悄改变已冻结计划的行为。仅研究留档的计划也带规格——合约模拟标签要用。
            **spec_fields, 'plan_levels': levels, 'news': news_c,
            'eligible_from': eligible_from(created), 'symbol': c['symbol'], 'name': c['name'], 'industry': c['industry'],
            'score': c['score'], 'strategy_type': c['strategy_type'], 'bucket': c['strategy_type'],
            'regime': screened['regime'], 'probability': c['probability'], 'hotmoney': c.get('hotmoney'),
            'reference_price': float(ref['close']), 'target': TARGET, 'paper_eligible': can_trade,
            'features': c, 'policy': POLICY, 'source_run': run_id,
            'limitations': calibration_short['limitations'], 'forecast_type': '研究假设，非已验证买入建议'}
        immutable(path, forecast)
        forecasts.append(forecast)
        report['new_forecast_ids'].append(identity)
        archived_n += 1
        paper_n += int(can_trade)
    _lap('freeze')
    report['forecasts'] = sorted(forecasts, key=lambda f: f['created_at'], reverse=True)
    report['outcomes'] = sorted(outcomes, key=lambda o: o['generated_at'], reverse=True)
    report['generated_at'] = datetime.now(CST).isoformat()
    report['status'] = 'ready' if screened['complete'] and not stale else 'partial'
    timing['total'] = round((_time.perf_counter() - _timing_t0) * 1000)
    return report


TRACK_LABEL = {'breakout': '突破', 'pullback': '回调反弹', 'reversal': '超跌反弹'}


def render(r):
    safe = lambda x: html.escape(str(x)).replace('|', '&#124;').replace('\n', ' ')
    hold = r['policy']['hold_sessions']
    lines = ['# Alpha 候选、预测与虚拟组合', '', f'生成：{r["generated_at"]} · 选股 {r.get("selection_version", r["version"])} · 执行 {r.get("execution_version", "—")} · {r["status"]}', '', r['target'], '',
             '策略为固定规则的研究实验；历史估计存在当前名单与行业分类回看偏差。所有胜率分母和数据覆盖均公开。'
             f'{hold}日持有短线策略（突破/回调反弹两条track，独立打标签、独立校准，不互相混用胜率）已替代原T+10版本，'
             '原T+10预测/账户规则冻结留档，不再产生新计划。', '']
    s = r['screen']
    if s:
        breakout_n, pullback_n = len(s['breakout']['candidates']), len(s['pullback']['candidates'])
        lines += [f'截至 {s["cutoff"]}；沪深在市 {s["listed"]}，覆盖 {s["coverage_pct"]}%。',
                  f'市场评分 {s["market_score"]} /100（规则分数，不是概率；暂停阈值 {s["market_score_pause"]}）；'
                  f'突破track通过 {breakout_n} 只，回调反弹track通过 {pullback_n} 只。', '',
                  f'| track | 股票 | 行业 | 综合分 | 原始分 | {hold}日研究胜率 | 有效样本/日期组 | 区间 |', '|---|---|---|---:|---:|---:|---:|---|']
        for c in r['candidates']:
            p = c['probability']
            interval = f'{p["low"]}–{p["high"]}%' if p['low'] is not None else '—'
            lines += [f'| {TRACK_LABEL.get(c["strategy_type"], c["strategy_type"])} | {safe(c["name"])} {c["symbol"]} | '
                      f'{safe(c["industry"])} | {c["score"]} | {c.get("raw_score", "—")} | {p["probability"] if p["probability"] is not None else "尚不可估计"} | '
                      f'{p["n"]}/{p["cohorts"]} | {interval} |']
        lines += ['', '突破track排除统计：'+safe(s['breakout']['exclusion_counts']),
                  '回调反弹track排除统计：'+safe(s['pullback']['exclusion_counts']), '',
                  '每只股票的排除理由及完整候选见同编号JSON。列表顺序就是实际排名：两条track各自按**截断前的原始分**'
                  '转成track内百分位，再合并排序（综合分被截断在100，只用于展示，不能用来排序）；'
                  f'前{r.get("archive_size", 10)}名全部留档做研究，其中只有前{r["policy"]["max_positions"]}名允许模拟成交。'
                  '概率仅作旁证，不能声称它们是全市场真实胜率最高。', '']
    p = r['portfolio']
    if p:
        lines += ['> **注意：下面这个账户是 exec-0.1**，只承载 0.3 版留下的 16 条旧计划（09:30–09:35 窗口按报价成交），已冻结。'
                  '`select-0.4` 起的新计划由盘中引擎按 **exec-0.2** 条件触发执行——独立的 10 万虚拟本金、独立账本，'
                  '见下面的「盘中条件执行虚拟账户」一节。两个账户的成交与胜率不得混算。', '']
        trade_rate = str(p['trade_win_rate'])+'%' if p.get('trade_win_rate') is not None else '暂无已平仓样本'
        pol = r['policy']
        lines += ['## 虚拟账户', '', f'估值日期 {p["last_date"]} · 状态 {p["valuation_status"]} · 总资产 {p["equity"]} · 现金 {p["cash"]} · 持仓 {len(p["positions"])}只',
                  f'净值 {p["nav"]}；同期基准收益 {p["benchmark_return_pct"]}%；超额 {p["excess_pp"]}个百分点；最大回撤 {p["max_drawdown_pct"]}%。',
                  f'已平仓 {p.get("closed_trades",0)}笔；实测虚拟交易胜率：{trade_rate}。', '',
                  f'成本假设：每边滑点{pol["slippage"]*100}%，佣金{pol["commission"]*100}%且至少{pol["minimum_commission"]}元，'
                  f'过户费{pol["transfer_fee"]*100}%，卖出税费{pol["sell_tax"]*100}%；仅为模拟参数，非券商报价。',
                  '只在预测留档后的下一合格日期模拟开盘成交。9:20之后生成的计划最早下一自然日开始等待实际基准交易日。',
                  f'新计划在09:30–09:35按采集时有效报价判断；偏离参考价超过±{pol["entry_gap_max"]*100}%、接近涨跌停或报价无法核验不成交。'
                  f'T+1，收盘退出信号等待下次观察窗口；不补记日线成交。',
                  f'最多{pol["max_positions"]}只、单只{pol["max_weight"]*100:.0f}%、计划单笔风险{pol["risk_per_trade"]*100:.0f}%、'
                  f'止损{pol["stop_pct"]*100:.0f}%/止盈{pol["target_pct"]*100:.0f}%/最长持有{pol["hold_sessions"]}个交易日；'
                  f'回撤{pol["drawdown_pause"]*100:.0f}%暂停加仓并排队退出。除权变化或持仓缺价暂停整个账本推进，等待可核验数据。', '']
    nc = r.get('news_check')
    if nc:
        lines += ['## 夜间公告核查（选股前）', '',
                  '核查范围：截止日 15:00 之后发布的公告（东方财富，非官方接口；只有公告、没有新闻，关键词分类很粗）。'
                  '查了 %d 只：无高风险 %d、需留意 %d、**未核验 %d**（取不到，不等于没事）、剔除 %d。' % (
                      nc['checked'], nc['clear'], nc['flagged'], nc['unverified'], len(nc['blocked'])), '']
        for b in nc['blocked']:
            lines.append('- 已剔除 %s %s（%s）：%s' % (safe(b['name']), b['symbol'], TRACK_LABEL.get(b['strategy_type'], b['strategy_type']),
                                                     '；'.join(safe(i['reason'] + '：' + i['title']) for i in b['items'][:2])))
        lines.append('')
    if r.get('exec02'):
        import conditional_exec
        lines += conditional_exec.render_account(r['exec02'])
    if r.get('evidence'):
        lines += contract_labels.render(r['evidence'])
    if r.get('baseline'):
        lines += baseline.render(r['baseline'])
    lines += ['## 预测验收', '', f'冻结预测 {len(r["forecasts"])} 条；已验收记录 {len(r["outcomes"])} 条。预测标签与实际虚拟成交盈亏分开统计。', '',
              '验收等待真实交易日自然到期；跳过成交不删除预测。错误归因先展示可计算结果，因果判断留待复核，不编造责任百分比。', '']
    cs = r.get('calibration_short') or {}
    if cs:
        rate = lambda v: '样本不足' if v is None else f'{v}%'
        lines += [f'短线两条track合计历史回看样本 {cs.get("historical_n", 0)} 条，前瞻留档已到期 {cs.get("live_n", 0)} 条。',
                  f'突破track前瞻胜率：{rate(cs.get("live_win_rate_breakout"))}；回调反弹track前瞻胜率：{rate(cs.get("live_win_rate_pullback"))}。', '']
    lines += ['- '+safe(i) for i in r['issues']]
    lines += ['- '+safe(i) for i in cs.get('limitations', [])]
    lines += ['', '规则说明：[策略口径](https://github.com/brycehu129/a-share-alpha-agent/blob/master/server/STRATEGY.md)。', '']
    return '\n'.join(lines)


def main(argv=None, now=None):
    """命令行入口（cron_daily_agent.sh 调用）。拆成函数是为了能测：这段代码曾经因为漏了 import os 而
    只有真跑才会崩——而它在必需的流程里。"""
    p = argparse.ArgumentParser()
    p.add_argument('--history', type=Path, required=True)
    p.add_argument('--run-id', required=True)
    p.add_argument('--freeze', dest='freeze', action='store_true', default=None, help='强制冻结新计划（手动补跑用）')
    p.add_argument('--no-freeze', dest='freeze', action='store_false', help='不冻结新计划，只更新验收/估值/证据')
    a = p.parse_args(argv)
    if not re.fullmatch(r'\d+-\d+', a.run_id):
        raise ValueError('Invalid ID')
    path = a.history / 'agent' / (a.run_id+'.json')
    if path.exists():
        raise ValueError('Report already archived')
    import proposals
    exec_account = None
    try:
        import conditional_exec
        exec_account = conditional_exec.read_ledger()
    except Exception as exc:                                  # 账本读不了不该拖垮候选生成；run() 里没有它就不出这一节
        print('exec 账本读取失败，本次报告不含该账户：%s: %s' % (type(exc).__name__, exc))
    # 收盘那两轮（15:35 / 18:20）只更新验收、估值和证据，不冻结新计划：计划改在盘前（08:40）用昨收数据 + 夜间公告冻结。
    freeze = a.freeze
    if freeze is None:
        # 默认规则：收盘那两轮不冻结；盘前 09:20 之后也不冻结——那之后冻结的计划当天已不能成交（执行器 09:20 截止），
        # 而它们的固定标签会从"后天"开始算，等于混进了一批错位的样本。
        freeze = os.environ.get('REPORT_SLOT') != 'close' and (now or datetime.now(CST)).time() < time(9, 20)
    import announcements
    # 人批准过的参数修订、exec 账本、公告核查都在这里读取/联网后传入（库代码不读，测试因此不受服务器状态影响）
    report = run(a.history, a.run_id, proposals.active(), exec_account, announcements.check_many, freeze)
    from expire_plans import reconcile
    reconcile(a.history, report, datetime.now(CST))
    immutable(path, report)
    path.with_suffix('.md').write_text(render(report))
    (path.parent/'README.md').write_text(render(report))
    print('Alpha engine:', report['status'], 'forecasts:', len(report['forecasts']), 'new:', len(report['new_forecast_ids']),
          '（冻结计划）' if freeze else '（本轮不冻结）')
    if freeze and report['new_forecast_ids']:
        try:
            import signal_cards
            print('买入计划推送：', signal_cards.push(report))
        except Exception as exc:                              # 推送失败绝不能让必需的选股流程失败
            print('买入计划推送失败：%s: %s' % (type(exc).__name__, exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
