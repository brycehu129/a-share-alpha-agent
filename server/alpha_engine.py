"""Orchestrate screening, immutable forecasts, forward acceptance and shadow NAV."""
import argparse
import html
import json
import re
from collections import Counter
from datetime import datetime, timedelta, time
from pathlib import Path
from alpha_data import raw_bars, symbol
from alpha_model import VERSION as MID_VERSION, POLICY as MID_POLICY, TARGET as MID_TARGET, screen, walk_forward, estimate, label
from alpha_portfolio import initial, advance
from collect_quotes import CST
from dashboard_export import latest
from review_pipeline import current_tuning
from shortterm_model import (VERSION, TARGET, SHORT_POLICY as POLICY, screen_short,
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


def resolve(forecasts, series, benchmark, now, history):
    outcomes = []
    dates = [b['date'] for b in benchmark]
    for f in forecasts:
        if not dates or f['eligible_from'] < dates[0]:
            # A rolling provider window cannot redefine an old forecast's entry.
            outcomes.extend(read(p) for p in (history / 'outcomes').glob(f['id']+'-*.json'))
            continue
        entry = next((d for d in dates if d >= f['eligible_from']), None)
        if entry is None:
            continue
        horizons = HORIZONS_BY_HOLD.get(f.get('policy', {}).get('hold_sessions'), (5, 10, 20))
        for horizon in horizons:
            path = history / 'outcomes' / (f['id']+'-'+str(horizon)+'.json')
            if path.exists():
                outcomes.append(read(path))
                continue
            outcome = label(series.get(f['symbol'], []), benchmark, entry, horizon)
            if outcome is not None:
                # Attribution is a measurable diagnostic, never a fabricated
                # percentage allocation or a causal explanation from prices alone.
                diagnostic = ('达成目标' if outcome['win'] else '基准同期下跌' if outcome['benchmark_pct'] < 0
                              else '标的未取得净正收益或未跑赢基准')
                record = {'prediction_id': f['id'], 'generated_at': now.isoformat(), **outcome,
                          'bucket': f['bucket'], 'regime': f['regime'], 'probability_at_issue': f['probability']['probability'],
                          'diagnostic': diagnostic, 'causal_attribution': '待复核，不能仅凭收益判定原因'}
                immutable(path, record)
                outcomes.append(record)
    return outcomes


def run(history, run_id):
    now = datetime.now(CST)
    root = history / 'alpha_data'
    report = {'id': run_id, 'version': VERSION, 'generated_at': now.isoformat(), 'status': 'waiting_data',
              'target': TARGET, 'policy': POLICY, 'mid_policy': MID_POLICY, 'issues': [], 'screen': None,
              'candidates': [], 'calibration': None, 'calibration_short': None, 'portfolio': None,
              'forecasts': [], 'outcomes': [], 'source_hashes': {}}
    master_path = history / 'tushare_data/stock_basic.json'
    benchmark_path = root / 'series/sh000300.json'
    previous_path, previous = latest(history, 'agent')
    tuning, _ = current_tuning(history)
    if not master_path.exists() or not benchmark_path.exists():
        report['issues'].append('等待股票清单和沪深300历史日线；不补造候选或成交。')
        report['portfolio'] = previous.get('portfolio') if previous else None
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
        return report
    if bridge:
        benchmark = [b for b in benchmark if b['date'] <= bridge['cutoff']]
        if len(benchmark) < 21:
            report['issues'].append('新源与基准共同窗口不足21日。')
            report['portfolio'] = previous.get('portfolio') if previous else None
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
    screened = screen_short(stocks, series, benchmark, cutoff, tuning)
    report['screen'] = screened
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
    calibration_short = walk_forward_short(stocks, series, benchmark, cutoff, tuning)
    forecasts = [read(p) for p in sorted((history / 'predictions').glob('*.json'))]
    outcomes = resolve(forecasts, series, benchmark, now, history)
    live_mid = [o for o in outcomes if o['horizon'] == 10]
    live_short = [o for o in outcomes if o['horizon'] == 3]
    report['calibration'] = {k: v for k, v in calibration.items() if k != 'samples'}
    report['calibration'].update(historical_n=len(calibration['samples']), live_n=len(live_mid),
                                live_win_rate=round(sum(o['win'] for o in live_mid)/len(live_mid)*100, 2) if live_mid else None)
    report['calibration_short'] = {k: v for k, v in calibration_short.items() if k != 'samples'}
    report['calibration_short'].update(historical_n=len(calibration_short['samples']), live_n=len(live_short))
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
    # Display list: both tracks pooled and ranked for browsing (30, not a
    # trading decision). The actual position-selection ranking is separate
    # (top3 below) -- per explicit instruction, pooled by raw score, not
    # split evenly across tracks. See shortterm_model.select_candidates()'s
    # own docstring for the score-comparability caveat that implies.
    report['candidates'] = sorted(all_short_candidates, key=lambda r: (-r['score'], r['symbol']))[:30]
    top3 = select_candidates(screened, POLICY['max_positions'])
    old_state = previous.get('portfolio') if previous else None
    codes = {c['symbol'] for c in top3}
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
    created = datetime.now(CST)  # Actual freeze time after all requests, not job start.
    for c in top3:
        ref = next((b for b in raw_series.get(c['symbol'], []) if b['date'] == cutoff), None)
        identity = VERSION + '-' + cutoff + '-' + c['symbol']
        path = history / 'predictions' / (identity+'.json')
        if path.exists():
            continue
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
        forecast = {'id': identity, 'version': VERSION, 'created_at': created.isoformat(), 'as_of': cutoff,
            'plan_reasons': plan_reasons,
            'execution_mode': 'observed-quote-v1',
            'eligible_from': eligible_from(created), 'symbol': c['symbol'], 'name': c['name'], 'industry': c['industry'],
            'score': c['score'], 'strategy_type': c['strategy_type'], 'bucket': c['strategy_type'],
            'regime': screened['regime'], 'probability': c['probability'],
            'reference_price': float(ref['close']), 'target': TARGET, 'paper_eligible': can_trade,
            'features': c, 'policy': POLICY, 'source_run': run_id,
            'limitations': calibration_short['limitations'], 'forecast_type': '研究假设，非已验证买入建议'}
        immutable(path, forecast)
        forecasts.append(forecast)
    report['forecasts'] = sorted(forecasts, key=lambda f: f['created_at'], reverse=True)
    report['outcomes'] = sorted(outcomes, key=lambda o: o['generated_at'], reverse=True)
    report['generated_at'] = datetime.now(CST).isoformat()
    report['status'] = 'ready' if screened['complete'] and not stale else 'partial'
    return report


TRACK_LABEL = {'breakout': '突破', 'pullback': '回调反弹'}


def render(r):
    safe = lambda x: html.escape(str(x)).replace('|', '&#124;').replace('\n', ' ')
    hold = r['policy']['hold_sessions']
    lines = ['# Alpha 候选、预测与虚拟组合', '', f'生成：{r["generated_at"]} · {r["version"]} · {r["status"]}', '', r['target'], '',
             '策略为固定规则的研究实验；历史估计存在当前名单与行业分类回看偏差。所有胜率分母和数据覆盖均公开。'
             f'{hold}日持有短线策略（突破/回调反弹两条track，独立打标签、独立校准，不互相混用胜率）已替代原T+10版本，'
             '原T+10预测/账户规则冻结留档，不再产生新计划。', '']
    s = r['screen']
    if s:
        breakout_n, pullback_n = len(s['breakout']['candidates']), len(s['pullback']['candidates'])
        lines += [f'截至 {s["cutoff"]}；沪深在市 {s["listed"]}，覆盖 {s["coverage_pct"]}%。',
                  f'市场评分 {s["market_score"]} /100（规则分数，不是概率；暂停阈值 {s["market_score_pause"]}）；'
                  f'突破track通过 {breakout_n} 只，回调反弹track通过 {pullback_n} 只。', '',
                  f'| track | 股票 | 行业 | 综合分 | {hold}日研究胜率 | 有效样本/日期组 | 区间 |', '|---|---|---|---:|---:|---:|---|']
        for c in r['candidates']:
            p = c['probability']
            interval = f'{p["low"]}–{p["high"]}%' if p['low'] is not None else '—'
            lines += [f'| {TRACK_LABEL.get(c["strategy_type"], c["strategy_type"])} | {safe(c["name"])} {c["symbol"]} | '
                      f'{safe(c["industry"])} | {c["score"]} | {p["probability"] if p["probability"] is not None else "尚不可估计"} | '
                      f'{p["n"]}/{p["cohorts"]} | {interval} |']
        lines += ['', '突破track排除统计：'+safe(s['breakout']['exclusion_counts']),
                  '回调反弹track排除统计：'+safe(s['pullback']['exclusion_counts']), '',
                  '每只股票的排除理由及完整候选见同编号JSON。两条track各自按固定综合分排序；'
                  '实际入账户的前3只按两条track合并后的原始分数统一排序（不按track分名额）——'
                  '两条打分公式量纲不同，混排可能让某条track系统性占多数席位，这点尚未用真实数据验证过，'
                  '概率仅作旁证，不能声称它们是全市场真实胜率最高。', '']
    p = r['portfolio']
    if p:
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


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--history', type=Path, required=True)
    p.add_argument('--run-id', required=True)
    a = p.parse_args()
    if not re.fullmatch(r'\d+-\d+', a.run_id):
        raise ValueError('Invalid ID')
    path = a.history / 'agent' / (a.run_id+'.json')
    if path.exists():
        raise ValueError('Report already archived')
    report = run(a.history, a.run_id)
    from expire_plans import reconcile
    reconcile(a.history, report, datetime.now(CST))
    immutable(path, report)
    path.with_suffix('.md').write_text(render(report))
    (path.parent/'README.md').write_text(render(report))
    print('Alpha engine:', report['status'], 'forecasts:', len(report['forecasts']))
