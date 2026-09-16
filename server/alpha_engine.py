"""Orchestrate screening, immutable forecasts, forward acceptance and shadow NAV."""
import argparse
import html
import json
import re
from datetime import datetime, timedelta, time
from pathlib import Path
from alpha_data import raw_bars, symbol
from alpha_model import VERSION, POLICY, TARGET, screen, walk_forward, estimate, label
from alpha_portfolio import initial, advance
from collect_quotes import CST
from dashboard_export import latest
from tushare_sync import read, save, sha


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
        entry = next((d for d in dates if d >= f['eligible_from']), None)
        if entry is None:
            continue
        for horizon in (5, 10, 20):
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
              'target': TARGET, 'policy': POLICY, 'issues': [], 'screen': None, 'candidates': [],
              'calibration': None, 'portfolio': None, 'forecasts': [], 'outcomes': [], 'source_hashes': {}}
    master_path = history / 'tushare_data/stock_basic.json'
    benchmark_path = root / 'series/sh000300.json'
    previous_path, previous = latest(history, 'agent')
    if not master_path.exists() or not benchmark_path.exists():
        report['issues'].append('等待股票清单和沪深300历史日线；不补造候选或成交。')
        report['portfolio'] = previous.get('portfolio') if previous else None
        return report
    master, bench_source = read(master_path), read(benchmark_path)
    report['source_hashes']['stock_basic'] = sha(master)
    report['source_hashes']['sh000300'] = sha(bench_source)
    today = now.date().isoformat()
    benchmark = [b for b in bench_source['bars'] if b['date'] < today or (b['date'] == today and now.time() >= time(15, 10))]
    if len(benchmark) < 21:
        report['issues'].append('基准不足21个已结束交易日。')
        report['portfolio'] = previous.get('portfolio') if previous else None
        return report
    cutoff = benchmark[-1]['date']
    stocks = [s for s in master['rows'] if s['exchange'] in ('SSE', 'SZSE') and s['list_status'] == 'L']
    series = {}
    for stock in stocks:
        code = symbol(stock['ts_code'])
        path = root / 'series' / (code+'.json')
        if path.exists():
            data = read(path)
            report['source_hashes'][code] = sha(data)
            series[code] = [b for b in data['bars'] if b['date'] <= cutoff]
    screened = screen(stocks, series, benchmark, cutoff)
    report['screen'] = screened
    stale = (now - datetime.fromisoformat(master['fetched_at'])).days > 7 or (now - datetime.fromisoformat(bench_source['fetched_at'])).total_seconds() > 86400
    if stale:
        report['issues'].append('股票清单超过7日或基准采集超过24小时，只展示研究，不产生新虚拟计划。')
    if not screened['complete']:
        report['issues'].append('有效日线覆盖不足95%，候选仅供观察，本轮不建立虚拟入场计划。')
    if screened['market_score'] < 40:
        report['issues'].append('市场趋势评分低于40，暂停新虚拟计划。')
    calibration = walk_forward(stocks, series, benchmark, cutoff)
    forecasts = [read(p) for p in sorted((history / 'predictions').glob('*.json'))]
    outcomes = resolve(forecasts, series, benchmark, now, history)
    live = [o for o in outcomes if o['horizon'] == 10]
    report['calibration'] = {k: v for k, v in calibration.items() if k != 'samples'}
    report['calibration'].update(historical_n=len(calibration['samples']), live_n=len(live),
                                live_win_rate=round(sum(o['win'] for o in live)/len(live)*100, 2) if live else None)
    for c in screened['candidates']:
        live_p = estimate(live, today, c['bucket'], c['regime'])
        historical_p = estimate(calibration['samples'], cutoff, c['bucket'], c['regime'])
        c['probability'] = live_p if live_p['probability'] is not None else historical_p
        c['probability']['source'] = '前瞻留档样本' if live_p['probability'] is not None else '有回看偏差的历史研究'
        if live_p['probability'] is not None:
            c['probability']['status'] = 'forward_empirical'
    # All candidates use the same selection rule used in historical folds.
    # Probability is shown alongside score; never change ranking retrospectively.
    report['candidates'] = screened['candidates'][:30]
    old_state = previous.get('portfolio') if previous else None
    codes = {c['symbol'] for c in report['candidates'][:3]}
    codes.update(p['symbol'] for p in (old_state or {}).get('positions', []))
    codes.update(f['symbol'] for f in forecasts if f['paper_eligible'] and f['id'] not in (old_state or {}).get('attempted', []))
    # Keep delisted/missing master positions in the valuation path. A missing
    # series must block valuation rather than disappear from the account.
    for code in codes-set(series):
        path = root / 'series' / (code+'.json')
        if path.exists():
            series[code] = [b for b in read(path)['bars'] if b['date'] <= cutoff]
    raw = raw_bars(history, codes, now)
    immutable(history / 'execution_inputs' / (run_id+'.json'), {'generated_at': now.isoformat(), 'series': raw})
    raw_series = {s: [b for b in d['bars'] if b['date'] <= cutoff] for s, d in raw.items()}
    old_state = old_state or initial(cutoff, float(benchmark[-1]['close']))
    report['portfolio'] = advance(old_state, forecasts, raw_series, series, benchmark, cutoff)
    report['issues'].extend(report['portfolio']['issues'])
    created = datetime.now(CST)  # Actual freeze time after all requests, not job start.
    for c in report['candidates'][:3]:
        ref = next((b for b in raw_series.get(c['symbol'], []) if b['date'] == cutoff), None)
        identity = VERSION + '-' + cutoff + '-' + c['symbol']
        path = history / 'predictions' / (identity+'.json')
        if path.exists():
            continue
        if ref is None:
            report['issues'].append(c['symbol']+' 未获得未复权参考价，不生成预测记录。')
            continue
        can_trade = screened['complete'] and not stale and screened['market_score'] >= 40 and report['portfolio']['valuation_status'] == 'current' and not report['portfolio']['paused']
        forecast = {'id': identity, 'version': VERSION, 'created_at': created.isoformat(), 'as_of': cutoff,
            'eligible_from': eligible_from(created), 'symbol': c['symbol'], 'name': c['name'], 'industry': c['industry'],
            'score': c['score'], 'bucket': c['bucket'], 'regime': c['regime'], 'probability': c['probability'],
            'reference_price': float(ref['close']), 'target': TARGET, 'paper_eligible': can_trade,
            'features': c, 'policy': POLICY, 'source_run': run_id,
            'limitations': calibration['limitations'], 'forecast_type': '研究假设，非已验证买入建议'}
        immutable(path, forecast)
        forecasts.append(forecast)
    report['forecasts'] = sorted(forecasts, key=lambda f: f['created_at'], reverse=True)
    report['outcomes'] = sorted(outcomes, key=lambda o: o['generated_at'], reverse=True)
    report['generated_at'] = datetime.now(CST).isoformat()
    report['status'] = 'ready' if screened['complete'] and not stale else 'partial'
    return report


def render(r):
    safe = lambda x: html.escape(str(x)).replace('|', '&#124;').replace('\n', ' ')
    lines = ['# Alpha 候选、预测与虚拟组合', '', f'生成：{r["generated_at"]} · {r["version"]} · {r["status"]}', '', r['target'], '',
             '策略为固定规则的研究实验；历史估计存在当前名单与行业分类回看偏差。所有胜率分母和数据覆盖均公开。', '']
    s = r['screen']
    if s:
        lines += [f'截至 {s["cutoff"]}；沪深在市 {s["listed"]}，基础排除后 {s["eligible"]}，有效窗口 {s["valid"]}，覆盖 {s["coverage_pct"]}%。',
                  f'市场评分 {s["market_score"]} /100（规则分数，不是概率）；通过全部过滤 {len(s["candidates"])} 只。', '',
                  '| 股票 | 行业 | 综合分 | 10日研究胜率 | 有效样本/日期组 | 区间 |', '|---|---|---:|---:|---:|---|']
        for c in r['candidates']:
            p = c['probability']
            lines += [f'| {safe(c["name"])} {c["symbol"]} | {safe(c["industry"])} | {c["score"]} | {p["probability"] if p["probability"] is not None else "尚不可估计"} | {p["n"]}/{p["cohorts"]} | {p["low"]}–{p["high"]} |']
        lines += ['', '排除统计：'+safe(s['exclusion_counts']), '', '每只股票的排除理由及完整候选见同编号JSON。候选按固定综合分排序，前3只留档；概率仅作旁证，不能声称它们是全市场真实胜率最高。', '']
    p = r['portfolio']
    if p:
        lines += ['## 虚拟账户', '', f'估值日期 {p["last_date"]} · 状态 {p["valuation_status"]} · 总资产 {p["equity"]} · 现金 {p["cash"]} · 持仓 {len(p["positions"])}只',
                  f'净值 {p["nav"]}；同期基准收益 {p["benchmark_return_pct"]}%；超额 {p["excess_pp"]}个百分点；最大回撤 {p["max_drawdown_pct"]}%。',
                  f'已平仓 {p.get("closed_trades",0)}笔；实测虚拟交易胜率 {p.get("trade_win_rate")}%。', '',
                  '成本假设：每边滑点0.1%，佣金0.03%且至少5元，过户费0.001%，卖出税费0.05%；仅为模拟参数，非券商报价。',
                  '只在预测留档后的下一合格日期模拟开盘成交。9:20之后生成的计划最早下一自然日开始等待实际基准交易日。',
                  '开盘偏离参考价超过±3%、接近涨跌停、一字行情或缺价不成交；T+1，收盘触发止盈/止损后下一交易日开盘退出，可能跳空超损。',
                  '最多3只、单只30%、计划单笔风险2%；回撤20%暂停加仓并排队退出。除权变化或持仓缺价暂停整个账本推进，等待可核验数据。', '']
    lines += ['## 预测验收', '', f'冻结预测 {len(r["forecasts"])} 条；5/10/20日已验收记录 {len(r["outcomes"])} 条。预测标签与实际虚拟成交盈亏分开统计。', '',
              '验收等待真实交易日自然到期；跳过成交不删除预测。错误归因先展示可计算结果，因果判断留待复核，不编造责任百分比。', '']
    lines += ['- '+safe(i) for i in r['issues']]
    lines += ['- '+safe(i) for i in (r['calibration'] or {}).get('limitations', [])]
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
    immutable(path, report)
    path.with_suffix('.md').write_text(render(report))
    (path.parent/'README.md').write_text(render(report))
    print('Alpha engine:', report['status'], 'forecasts:', len(report['forecasts']))
