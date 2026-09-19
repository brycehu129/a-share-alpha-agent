"""情景留档与收盘对账。**这个机制和哨兵推送必须同时上线，不能留到以后。**

推送一旦每天出现，你对"它准不准"的印象会迅速被选择性记忆绑架——记得住说对的那几次，
忘得掉说错的。只有机械记录能对抗这个：每条情景发出时就把它的数值条件存下来，收盘后用
当天的分时数据逐条判定。自选股情景当天收盘就能验收，比候选池 3 天的标签快得多。

**判定只用情景发布之后的分钟数据。** 发布之前的走势一概忽略——否则"事后诸葛亮"，
任何情景都能被说成对的。

五种结果（比计划里多一种）：

  not_triggered        触发价从未被触及，失效价也没碰
  invalidated_first    还没触发就先触及了失效价（判断在触发之前就错了）
  triggered_and_hit    触发后先到达目标区间
  triggered_but_failed 触发后先触及失效价
  triggered_unresolved 触发了，但直到收盘既没到目标也没触及失效价——**没有结论**

第五种必须单独存在：把它硬塞进"命中"或"失败"都会扭曲命中率。命中率只在"已有结论"
（命中 + 失败）里算。

**局限（写进每份汇总里）：** 分时数据只有分钟收盘价，分钟内的瞬间触及看不到，所以
not_triggered 可能有漏判；这一点对所有情景一视同仁，不会偏向任何一种结果。
**情景命中率 ≠ 交易盈利**：情景判对了仍可能因为滑点、流动性、时机而无法赚到钱。
"""
import argparse
import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import intraday_engine as ie
from collect_quotes import CST

OUTCOMES = ('triggered_and_hit', 'triggered_but_failed', 'triggered_unresolved',
            'invalidated_first', 'not_triggered')
OUTCOME_LABEL = {'triggered_and_hit': '触发且命中', 'triggered_but_failed': '触发但失败',
                 'triggered_unresolved': '触发但无结论', 'invalidated_first': '未触发先失效',
                 'not_triggered': '未触发'}
MIN_N_FOR_RATE = 20             # 已有结论的样本不足这个数，不给百分比，只给计数
LIMITATIONS = ['分时数据只有分钟收盘价，分钟内的瞬间触及看不到，not_triggered 可能有漏判（对所有情景一视同仁）。',
               '情景命中率不等于交易盈利：判对了仍可能因滑点、流动性、时机而赚不到钱。',
               '触发价离现价越远越难被触及；只看命中率会奖励"给一个几乎不会触发的情景"，'
               '所以汇总里同时给出触发率和触发距离。']


def sentinel_dir(directory=None):
    return (Path(directory) if directory else ie.data_dir().parent / 'sentinel')


def _issue_hhmm(iso):
    return datetime.fromisoformat(iso).astimezone(CST).strftime('%H%M')


def record(directory, day, issued_at, symbol, name, node, price_at_issue, scenarios, hint, confidence,
           is_holding, meta=None, delivered=True):
    """把一条已通过校验的个股研判里的每个情景各存一行。返回存下的 id 列表。"""
    d = sentinel_dir(directory)
    d.mkdir(parents=True, exist_ok=True)
    ids = []
    with open(d / ('scenarios-%s.jsonl' % day), 'a', encoding='utf-8') as f:
        for i, sc in enumerate(scenarios):
            sid = '%s-%s-%s-%d' % (day, symbol, datetime.fromisoformat(issued_at).strftime('%H%M%S'), i)
            f.write(json.dumps({'id': sid, 'day': day, 'symbol': symbol, 'name': name, 'node': node,
                                'issued_at': issued_at, 'price_at_issue': price_at_issue,
                                'action_hint': hint, 'confidence': confidence, 'is_holding': is_holding,
                                'delivered': delivered, 'scenario': sc, **(meta or {})}, ensure_ascii=False) + '\n')
            ids.append(sid)
    return ids


def load_scenarios(directory, day):
    path = sentinel_dir(directory) / ('scenarios-%s.jsonl' % day)
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding='utf-8').splitlines() if l.strip()]


def judge(rec, bars):
    """用发布之后的分钟收盘价判定一条情景。bars: minute_data 的 bars 列表。"""
    sc = rec['scenario']
    after = [b for b in bars if b['t'] > _issue_hhmm(rec['issued_at'])]      # 严格晚于发布那一分钟
    if not after:
        return {'outcome': None, 'note': '发布之后没有分钟数据'}
    up = sc['direction'] == 'up'
    trig, inv = sc['trigger_price'], sc['invalidate_price']
    reached = (lambda p, x: p >= x) if up else (lambda p, x: p <= x)         # 朝有利方向触及
    broke = (lambda p, x: p <= x) if up else (lambda p, x: p >= x)           # 朝失效方向触及
    tgt = sc['target_low'] if up else sc['target_high']

    t_idx = next((i for i, b in enumerate(after) if reached(b['price'], trig)), None)
    i_idx = next((i for i, b in enumerate(after) if broke(b['price'], inv)), None)
    res = {'trigger_at': None, 'resolve_at': None, 'close_price': after[-1]['price'],
           'first_bar_after_issue': after[0]['t'], 'bars_after_issue': len(after)}
    if t_idx is None:
        res['outcome'] = 'invalidated_first' if i_idx is not None else 'not_triggered'
        if i_idx is not None:
            res['resolve_at'] = after[i_idx]['t']
        return res
    if i_idx is not None and i_idx < t_idx:
        return {**res, 'outcome': 'invalidated_first', 'resolve_at': after[i_idx]['t']}
    res['trigger_at'] = after[t_idx]['t']
    post = after[t_idx:]
    h_idx = next((i for i, b in enumerate(post) if reached(b['price'], tgt)), None)
    f_idx = next((i for i, b in enumerate(post) if broke(b['price'], inv)), None)
    if h_idx is not None and (f_idx is None or h_idx < f_idx):
        return {**res, 'outcome': 'triggered_and_hit', 'resolve_at': post[h_idx]['t']}
    if f_idx is not None:
        return {**res, 'outcome': 'triggered_but_failed', 'resolve_at': post[f_idx]['t']}
    return {**res, 'outcome': 'triggered_unresolved'}


def reconcile(directory, day, minute_fetch, now=None):
    """收盘后逐条对账。分时数据不是当天的、或还没走到收盘，一律不判——不用不完整的数据下结论。"""
    now = now or datetime.now(CST)
    recs = load_scenarios(directory, day)
    results, minutes = [], {}
    for rec in recs:
        sym = rec['symbol']
        if sym not in minutes:
            try:
                m = minute_fetch(sym, now)
                minutes[sym] = m if m['trade_date'] == day else {'error': '分时数据日期是 %s，不是 %s' % (m['trade_date'], day)}
            except Exception as exc:
                minutes[sym] = {'error': '取分时失败: %s' % type(exc).__name__}
        m = minutes[sym]
        if 'error' in m:
            results.append({'id': rec['id'], 'outcome': None, 'note': m['error']})
        elif not m['complete']:
            results.append({'id': rec['id'], 'outcome': None, 'note': '分时数据尚未走完到 15:00，不下结论'})
        else:
            results.append({'id': rec['id'], **judge(rec, m['bars'])})
    payload = {'day': day, 'reconciled_at': now.isoformat(), 'basis': 'minute_close', 'results': results,
               'limitations': LIMITATIONS}
    d = sentinel_dir(directory)
    d.mkdir(parents=True, exist_ok=True)
    (d / ('reconcile-%s.json' % day)).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    return payload


def load_joined(directory, days):
    """把若干天的情景和对账结果按 id 合并。没对过账的情景 outcome 为 None，不参与统计。"""
    rows = []
    for day in days:
        path = sentinel_dir(directory) / ('reconcile-%s.json' % day)
        outcomes = {r['id']: r for r in json.loads(path.read_text(encoding='utf-8'))['results']} if path.exists() else {}
        for rec in load_scenarios(directory, day):
            r = outcomes.get(rec['id'], {})
            rows.append({**rec, 'outcome': r.get('outcome'), 'judge': r})
    return rows


def _bucket(conf):
    return '1-2' if conf <= 2 else '3' if conf == 3 else '4-5'


def _stats(rows):
    judged = [r for r in rows if r['outcome']]
    c = {o: sum(r['outcome'] == o for r in judged) for o in OUTCOMES}
    hit, fail = c['triggered_and_hit'], c['triggered_but_failed']
    triggered = hit + fail + c['triggered_unresolved']
    dist = [abs(r['scenario']['trigger_price'] / r['price_at_issue'] - 1) * 100 for r in judged if r.get('price_at_issue')]
    return {'n': len(judged), 'counts': c, 'triggered': triggered,
            'trigger_rate_pct': round(triggered / len(judged) * 100, 1) if len(judged) >= MIN_N_FOR_RATE else None,
            'hit_rate_pct': round(hit / (hit + fail) * 100, 1) if hit + fail >= MIN_N_FOR_RATE else None,
            'concluded': hit + fail,
            'median_trigger_distance_pct': round(statistics.median(dist), 2) if dist else None}


def summarize(rows):
    """按整体 / 节点类型 / 置信度档 / 股票 分组。百分比只在已有结论的样本 ≥20 时才给。"""
    # 只统计"你真的收到了"的情景。推送失败（webhook 没配/挂了）的情景你根本没看到，
    # 记进命中率等于统计了一批你无法据此行动的东西。
    undelivered = sum(r.get('delivered') is False for r in rows)
    rows = [r for r in rows if r.get('delivered') is not False]
    groups = {'overall': rows}
    by = {'node': defaultdict(list), 'confidence': defaultdict(list), 'symbol': defaultdict(list),
          'direction': defaultdict(list)}
    for r in rows:
        by['node'][r['node']].append(r)
        by['confidence'][_bucket(r['confidence'])].append(r)
        by['symbol'][r['symbol']].append(r)
        by['direction'][r['scenario']['direction']].append(r)
    return {'overall': _stats(rows), 'unjudged': sum(r['outcome'] is None for r in rows), 'undelivered': undelivered,
            **{k: {g: _stats(v) for g, v in sorted(d.items())} for k, d in by.items()},
            'limitations': LIMITATIONS}


def weekly(directory, end_day, days=7):
    end = datetime.strptime(end_day, '%Y-%m-%d')
    span = [(end - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(days - 1, -1, -1)]
    return {'from': span[0], 'to': span[-1], **summarize(load_joined(directory, span))}


def render_weekly(w):
    o = w['overall']
    fmt = lambda v: '样本不足，不给百分比' if v is None else '%s%%' % v
    lines = ['【哨兵情景周报 %s ~ %s】' % (w['from'], w['to']),
             '共对账 %d 条（另有 %d 条未能对账）；触发 %d 条，其中有结论 %d 条（命中 %d / 失败 %d），无结论 %d 条。' % (
                 o['n'], w['unjudged'], o['triggered'], o['concluded'], o['counts']['triggered_and_hit'],
                 o['counts']['triggered_but_failed'], o['counts']['triggered_unresolved']),
             '触发率：%s；有结论样本里的命中率：%s；触发价离现价中位 %s%%。' % (
                 fmt(o['trigger_rate_pct']), fmt(o['hit_rate_pct']), o['median_trigger_distance_pct'])]
    for title, key in (('按置信度', 'confidence'), ('按节点', 'node')):
        parts = ['%s：n=%d 命中率%s' % (g, s['n'], fmt(s['hit_rate_pct'])) for g, s in w[key].items() if s['n']]
        if parts:
            lines.append('%s：%s' % (title, '；'.join(parts)))
    lines += ['', '注意：' + LIMITATIONS[0], LIMITATIONS[1]]
    return '\n'.join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['reconcile', 'weekly'])
    p.add_argument('--day', default=datetime.now(CST).strftime('%Y-%m-%d'))
    p.add_argument('--dir', type=Path)
    a = p.parse_args()
    if a.command == 'reconcile':
        import minute_data
        r = reconcile(a.dir, a.day, minute_data.fetch_minute)
        judged = [x for x in r['results'] if x['outcome']]
        print('%s 对账 %d 条，其中有判定 %d 条' % (a.day, len(r['results']), len(judged)))
        for o in OUTCOMES:
            n = sum(x['outcome'] == o for x in judged)
            if n:
                print('  %s: %d' % (OUTCOME_LABEL[o], n))
    else:
        print(render_weekly(weekly(a.dir, a.day)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
