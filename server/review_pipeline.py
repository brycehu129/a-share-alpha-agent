"""Post-market review: classify misses, propose bounded auto-tuning, log blind spots.

Reads outcomes/predictions already produced by alpha_engine.py / shortterm_model.py
(never re-labels an outcome). Every matured outcome belongs to exactly one track:

  - 'mid'      -- the T+10 strategy, horizon 10.
  - 'breakout' -- the short-term breakout track, horizon 3.
  - 'pullback' -- the short-term pullback track, horizon 3.

Tracks are ALWAYS reported and calibrated separately (their win rates are never
pooled into one number) per the explicit design decision to validate breakout
and pullback independently. Two kinds of finding come out of every matured
outcome, regardless of track:

  (1) explainable by a feature this strategy already computes (deviation, volume
      ratio, regime, industry/stock score gap) -> feeds the bounded numeric
      tuning below.
  (2) not explainable by anything currently computed -> logged as a "blind spot"
      for a human decision on whether a new data source (sentiment/研报/消息)
      is worth adding. This module never ingests a new data source itself and
      never proposes one on its own initiative; it only surfaces the evidence.

Tuning is deliberately narrow: exactly three parameters, each capped per
adjustment, each gated behind the same sample bar alpha_model.estimate() uses
(n>=30, date-cohorts>=15), and every adjustment is written to an immutable
audit record before the live pointer is updated. Position sizing, risk-per-trade
and drawdown pause/stop are never touched here. 'market_score_pause' is a gate
shared by all three tracks (shortterm_model.screen_short() reuses it), so its
evidence pools outcomes from every track; 'overheat_coef' and 'industry_weight'
are internals of the mid-track score only and are evaluated on mid-track
outcomes exclusively -- a breakout/pullback outcome never moves them. Neither
short-term track has tunable parameters of its own yet: this module only
reports their win rates and blind spots until there's enough of a track record
to design bounded tuning for them, same as the mid-track got here first.
"""
import argparse
import statistics
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from tushare_sync import read, save

CST = timezone(timedelta(hours=8))
VERSION = 'review-0.2'
TRACK_HORIZON = {10: 'mid', 3: None}  # horizon 3 resolves via forecast['strategy_type']

# The only three knobs this module is allowed to touch, with hard floors/
# ceilings and a per-adjustment step cap. Defaults match the hardcoded values
# alpha_model.py used before tuning existed (40% industry / 60% stock weight,
# 2pts-per-%-over-5% overheat penalty, market_score pause at 40).
TUNABLE = {
    'industry_weight':    {'default': 0.40, 'step_cap': 0.05, 'floor': 0.20, 'ceiling': 0.60},
    'overheat_coef':      {'default': 2.0,  'step_cap': 0.5,  'floor': 0.5,  'ceiling': 4.0},
    'market_score_pause': {'default': 40,   'step_cap': 5,    'floor': 30,   'ceiling': 55},
}
GATE = {'min_n': 30, 'min_cohorts': 15}  # same bar as alpha_model.estimate()


def immutable(path, payload):
    if path.exists():
        if read(path) != payload:
            raise ValueError('Immutable record conflict: ' + path.name)
        return
    save(path, payload)


def current_tuning(history):
    path = history / 'tuning' / 'active.json'
    if not path.exists():
        return {k: v['default'] for k, v in TUNABLE.items()}, None
    state = read(path)
    return state['values'], state


def load_reviewed(history):
    """Outcome ids already folded into a prior review; never double-count."""
    seen = set()
    folder = history / 'tuning' / 'reviews'
    if folder.exists():
        for p in folder.glob('*.json'):
            seen.update(read(p).get('outcome_ids', []))
    return seen


def track_of(outcome, forecast):
    if outcome['horizon'] == 10:
        return 'mid'
    if outcome['horizon'] == 3:
        return forecast.get('strategy_type')  # 'breakout' | 'pullback' | None (unrecognized -> skipped)
    return None  # 1/2-day supplementary labels aren't a track of their own; not reviewed here


def classify(outcome, forecast):
    """Split a matured outcome into mechanically-explainable vs blind-spot.

    Only asserts what the stored features can support; never fabricates a
    cause. A miss earns 'explainable' the moment ANY tracked feature was near
    a known boundary (overheat, volume, weak regime, score-source disagreement)
    even if that isn't proven to be the sole cause -- it's evidence, not a
    verdict, exactly like the rest of this codebase's causal_attribution field.
    """
    f = forecast.get('features', {})
    reasons = []
    if f.get('deviation_pct', 0) > 8:
        reasons.append('deviation_near_cap')
    if f.get('volume_ratio', 0) > 2.5:
        reasons.append('volume_ratio_near_cap')
    if forecast.get('regime') == '偏弱':
        reasons.append('weak_regime')
    theme, leader = f.get('theme_score'), f.get('leader_score')
    if theme is not None and leader is not None and abs(theme - leader) > 25:
        reasons.append('industry_stock_score_gap')
    if reasons:
        return 'explainable', reasons
    if not outcome['win'] and outcome['excess_pp'] < -3:
        return 'blind_spot', []
    return 'explainable', ['no_dominant_factor']


def evidence(bucket_outcomes):
    n = len(bucket_outcomes)
    cohorts = len({o['entry_day'] for o in bucket_outcomes})
    if n < GATE['min_n'] or cohorts < GATE['min_cohorts']:
        return None
    win_rate = sum(o['win'] for o in bucket_outcomes) / n * 100
    mean_excess = statistics.mean(o['excess_pp'] for o in bucket_outcomes)
    return {'n': n, 'cohorts': cohorts, 'win_rate_pct': round(win_rate, 2), 'mean_excess_pp': round(mean_excess, 4)}


def clamp(value, spec, delta):
    step = max(-spec['step_cap'], min(spec['step_cap'], delta))
    return round(max(spec['floor'], min(spec['ceiling'], value + step)), 4)


def propose_adjustments(by_reason_pooled, by_reason_mid, current):
    """Compare each explainable-reason bucket against its own population's
    overall; only propose a step when BOTH sides clear the sample gate and the
    gap is large enough to be worth a bounded nudge. market_score_pause pools
    evidence across all three tracks (it gates all of them); overheat_coef and
    industry_weight are mid-track-only internals and use mid-track evidence
    exclusively, even if a breakout/pullback outcome also happened to show a
    large deviation -- that feature wasn't used to score those tracks."""
    proposals = []

    pooled_overall = evidence(by_reason_pooled.get('_all', []))
    if pooled_overall is not None:
        weak = evidence(by_reason_pooled.get('weak_regime', []))
        if weak and weak['win_rate_pct'] < pooled_overall['win_rate_pct'] - 15:
            new = clamp(current['market_score_pause'], TUNABLE['market_score_pause'], TUNABLE['market_score_pause']['step_cap'])
            if new != current['market_score_pause']:
                proposals.append({'parameter': 'market_score_pause', 'old': current['market_score_pause'], 'new': new,
                    'evidence': {'weak_regime': weak, 'overall': pooled_overall, 'pooled_across_tracks': True},
                    'logic': '偏弱regime组胜率显著低于整体（≥15pp，三条track合并统计，因为这个阈值三条track共用），'
                             '且两侧均满足n≥30/日期组≥15；市场评分暂停阈值上调一个步长（≤5点）。'})

    mid_overall = evidence(by_reason_mid.get('_all', []))
    if mid_overall is not None:
        dev = evidence(by_reason_mid.get('deviation_near_cap', []))
        if dev and dev['win_rate_pct'] < mid_overall['win_rate_pct'] - 10:
            new = clamp(current['overheat_coef'], TUNABLE['overheat_coef'], TUNABLE['overheat_coef']['step_cap'])
            if new != current['overheat_coef']:
                proposals.append({'parameter': 'overheat_coef', 'old': current['overheat_coef'], 'new': new,
                    'evidence': {'deviation_near_cap': dev, 'overall': mid_overall, 'track': 'mid'},
                    'logic': '中线track内，偏离MA20接近12%上限的候选组胜率显著低于该track整体（≥10pp）；'
                             '过热惩罚系数上调一个步长（≤0.5/百分点），加重该区间扣分。'})

        gap = evidence(by_reason_mid.get('industry_stock_score_gap', []))
        if gap and gap['win_rate_pct'] < mid_overall['win_rate_pct'] - 10:
            # Negative mean-excess vs overall -> stock-level score under-weighted;
            # positive -> industry-level score under-weighted. Direction only,
            # magnitude is always exactly one capped step.
            direction = -1 if gap['mean_excess_pp'] < mid_overall['mean_excess_pp'] else 1
            new = clamp(current['industry_weight'], TUNABLE['industry_weight'], TUNABLE['industry_weight']['step_cap'] * direction)
            if new != current['industry_weight']:
                proposals.append({'parameter': 'industry_weight', 'old': current['industry_weight'], 'new': new,
                    'evidence': {'industry_stock_score_gap': gap, 'overall': mid_overall, 'track': 'mid'},
                    'logic': '中线track内，行业分与个股分差距较大（>25分）的候选组胜率显著低于该track整体（≥10pp）；'
                             '行业权重调整一个步长（≤5个百分点），个股权重相应反向变动。'})
    return proposals


def run(history, run_id):
    now = datetime.now(CST)
    already = load_reviewed(history)
    fresh = []
    for p in sorted((history / 'outcomes').glob('*.json')):
        o = read(p)
        if o['horizon'] not in (3, 10) or p.stem in already:
            continue
        pred_path = history / 'predictions' / (o['prediction_id'] + '.json')
        if not pred_path.exists():
            continue
        forecast = read(pred_path)
        track = track_of(o, forecast)
        if track is None:
            continue  # unrecognized strategy_type on a 3-day outcome; skip rather than guess
        fresh.append((p.stem, o, forecast, track))

    report = {'id': run_id, 'version': VERSION, 'generated_at': now.isoformat(),
              'reviewed_n': len(fresh), 'outcome_ids': [oid for oid, _, _, _ in fresh],
              'by_track': {}, 'blind_spots': [], 'by_reason': {}, 'proposals': [], 'applied': None,
              'status': 'no_new_outcomes'}
    if not fresh:
        immutable(history / 'tuning' / 'reviews' / (run_id + '.json'), report)
        return report

    by_track_outcomes = defaultdict(list)
    by_reason_pooled = defaultdict(list)   # across all tracks -- only used for market_score_pause
    by_reason_mid = defaultdict(list)      # mid-track only -- used for overheat_coef / industry_weight
    for oid, o, forecast, track in fresh:
        by_track_outcomes[track].append(o)
        kind, reasons = classify(o, forecast)
        by_reason_pooled['_all'].append(o)
        if track == 'mid':
            by_reason_mid['_all'].append(o)
        if kind == 'blind_spot':
            report['blind_spots'].append({
                'track': track, 'outcome_id': oid, 'prediction_id': o['prediction_id'],
                'symbol': forecast['symbol'], 'name': forecast['name'],
                'entry_day': o['entry_day'], 'end_day': o['end_day'],
                'excess_pp': o['excess_pp'], 'mfe_pct': o['mfe_pct'], 'mae_pct': o['mae_pct'],
                'note': '现有K线特征（行业分/个股分/偏离/量比/市场评分）未显示可解释此次误判的主导因子；'
                        '不代表已确认原因，仅标记为当前特征集之外的观察对象。'})
        for r in reasons:
            by_reason_pooled[r].append(o)
            if track == 'mid':
                by_reason_mid[r].append(o)

    report['by_track'] = {t: v for t in by_track_outcomes if (v := evidence(by_track_outcomes[t])) is not None}
    report['by_reason'] = {k: v for k in by_reason_pooled if k != '_all' and (v := evidence(by_reason_pooled[k])) is not None}

    current, state = current_tuning(history)
    proposals = propose_adjustments(by_reason_pooled, by_reason_mid, current)
    report['proposals'] = proposals

    if proposals:
        new_values = dict(current)
        for adj in proposals:
            new_values[adj['parameter']] = adj['new']
        version_n = (state['version_n'] + 1) if state else 1
        new_state = {'version_n': version_n, 'updated_at': now.isoformat(), 'source_review': run_id,
                     'values': new_values, 'proposals_applied': proposals}
        # active.json is a live pointer (overwritten by design); the full
        # version history stays immutable under tuning/history/.
        save(history / 'tuning' / 'active.json', new_state)
        immutable(history / 'tuning' / 'history' / (run_id + '.json'), new_state)
        report['applied'] = new_state
        report['status'] = 'tuned'
    else:
        report['status'] = 'reviewed_no_adjustment'

    immutable(history / 'tuning' / 'reviews' / (run_id + '.json'), report)
    return report


TRACK_LABEL = {'mid': '中线T+10', 'breakout': '短线-突破', 'pullback': '短线-回调反弹'}


def render(r):
    lines = [f'# 盘后复盘 {r["id"]}', '', f'生成：{r["generated_at"]} · {r["version"]} · {r["status"]}',
             f'本轮新验收结果 {r["reviewed_n"]} 条。', '']
    if r['by_track']:
        lines += ['## 各track胜率（独立统计，互不混用；仅显示已满足样本门槛 n≥30 且日期组≥15 的track）', '']
        for t, v in r['by_track'].items():
            lines.append(f'- {TRACK_LABEL.get(t, t)}: n={v["n"]}, 日期组={v["cohorts"]}, 胜率={v["win_rate_pct"]}%, 平均超额={v["mean_excess_pp"]}pp')
        lines.append('')
    if r['by_reason']:
        lines += ['## 归因分组（仅统计已满足样本门槛 n≥30 且日期组≥15 的分组）', '']
        for k, v in r['by_reason'].items():
            lines.append(f'- {k}: n={v["n"]}, 日期组={v["cohorts"]}, 胜率={v["win_rate_pct"]}%, 平均超额={v["mean_excess_pp"]}pp')
        lines.append('')
    if r['proposals']:
        lines += ['## 本轮参数调整（已生效，供下一轮使用）', '']
        for p in r['proposals']:
            lines.append(f'- {p["parameter"]}: {p["old"]} → {p["new"]}。{p["logic"]}')
        lines.append('')
    else:
        lines += ['本轮未触发任何参数调整（样本不足门槛，或分组差异未达调整阈值）。', '']
    if r['blind_spots']:
        lines += [f'## 待观察盲区（{len(r["blind_spots"])} 条）', '',
                  '以下误判无法用现有K线特征解释，不代表已确认原因；仅供判断是否需要引入新数据源'
                  '（情绪/机构研报/消息等），不会自动接入任何新数据源。', '']
        for b in r['blind_spots']:
            lines.append(f'- [{TRACK_LABEL.get(b["track"], b["track"])}] {b["symbol"]} {b["name"]}'
                          f'（{b["entry_day"]}→{b["end_day"]}）：超额{b["excess_pp"]}pp，MFE {b["mfe_pct"]}%，MAE {b["mae_pct"]}%')
        lines.append('')
    lines.append('本模块只调整已约定的三个数值参数（行业/个股权重、过热惩罚系数、市场评分暂停阈值），'
                  '从不改动仓位/风险上限；每次调整均留档可查，是否引入新数据源始终由人工决定。')
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--history', type=Path, required=True)
    p.add_argument('--run-id', required=True)
    a = p.parse_args()
    report = run(a.history, a.run_id)
    (a.history / 'tuning' / 'reviews' / (a.run_id + '.md')).write_text(render(report))
    print('Review:', report['status'], 'proposals:', len(report['proposals']), 'blind spots:', len(report['blind_spots']))
