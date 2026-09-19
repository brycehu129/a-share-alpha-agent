"""Small, credential-free dashboard snapshot exported from verified archives."""
import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path


def verified(path, compact=False):
    envelope = json.loads(path.read_text())
    payload = envelope['payload']
    options = {'separators': (',', ':')} if compact else {}
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, **options).encode()).hexdigest()
    if digest != envelope['sha256']:
        raise ValueError('Source checksum mismatch: ' + path.name)
    return payload


def latest(history, folder, compact=False):
    entries = [(p, verified(p, compact)) for p in (history / folder).glob('*.json')]
    return max(entries, key=lambda x: (x[1]['generated_at'], x[0].name)) if entries else (None, None)


def build(history):
    result = {'schema_version': 1, 'exported_at': datetime.now(timezone(timedelta(hours=8))).isoformat(),
              'market': None, 'research': None, 'sync': None, 'analysis': None, 'sources': []}
    for kind, folder, compact in [('market', 'records', True), ('research', 'research', True),
                                 ('sync', 'tushare_data/runs', False), ('analysis', 'tushare_analysis', False)]:
        path, d = latest(history, folder, compact)
        if d is None:
            continue
        md = str(path.relative_to(history).with_suffix('.md'))
        if kind == 'market':
            md = 'reports/' + path.stem + '.md'
        result['sources'].append({'kind': kind, 'id': path.stem, 'generated_at': d['generated_at'],
            'url': 'https://github.com/brycehu129/a-share-alpha-agent/blob/market-data/' + md})
        if kind == 'market':
            u = d.get('universe', {})
            dates = Counter(q['quote_at'][:10] for q in u.get('quotes', []))
            breadth = None
            if len(dates) == 1:
                changes = [(Decimal(str(q['last'])) / Decimal(str(q['previous_close'])) - 1) for q in u['quotes']]
                breadth = {'up': sum(v > 0 for v in changes), 'down': sum(v < 0 for v in changes), 'flat': sum(v == 0 for v in changes)}
            result[kind] = {'generated_at': d['generated_at'], 'status': d['status'], 'summary': d['summary'],
                'quotes': [{k: q[k] for k in ('name', 'symbol', 'last', 'change_pct', 'quote_at')} for q in d['quotes']],
                'universe': {'status': u.get('status'), 'listed': len(u.get('members', [])), 'quoted': len(u.get('quotes', [])),
                    'coverage_pct': u.get('coverage_pct'), 'dates': dict(dates), 'breadth': breadth}}
        elif kind == 'research':
            result[kind] = {k: d.get(k) for k in ('generated_at', 'status', 'factor_cutoff', 'industry_provider', 'mapping', 'source_id', 'source_generated_at')}
            result[kind].update(sample_count=len(d.get('sample', [])), factor_count=len(d.get('rankings', [])),
                rankings=d.get('rankings', [])[:30], industries=d.get('industry_ranking', [])[:12])
        elif kind == 'sync':
            result[kind] = {k: d.get(k) for k in ('generated_at', 'status', 'calendar_range', 'calendar_agree', 'target_days', 'saved_days', 'pending_days', 'errors', 'stock_counts')}
        else:
            result[kind] = {k: d.get(k) for k in ('generated_at', 'status', 'start_date', 'end_date', 'missing_dates', 'issues')}
            result[kind].update(rankings=d.get('rankings', [])[:30], factor_count=len(d.get('rankings', [])))
    brief_path, brief = latest(history, 'briefs')
    if brief is not None:
        result['sources'].append({'kind': '盘前/收盘日报', 'id': brief_path.stem, 'generated_at': brief['generated_at'],
            'url': 'https://github.com/brycehu129/a-share-alpha-agent/blob/market-data/briefs/' + brief_path.stem + '.md'})
    agent_path, agent = latest(history, 'agent')
    result['agent'] = None
    if agent is not None:
        screening = agent.get('screen')
        portfolio = agent.get('portfolio')
        result['agent'] = {k: agent.get(k) for k in ('generated_at', 'version', 'status', 'target', 'issues', 'calibration', 'calibration_short', 'candidates', 'policy')}
        result['agent']['screen'] = ({k: screening[k] for k in ('cutoff', 'listed', 'eligible', 'valid', 'coverage_pct', 'complete', 'market_score', 'regime', 'market_score_pause', 'exclusion_counts')} if screening else None)
        result['agent']['portfolio'] = ({**portfolio, 'trades': portfolio['trades'][-30:], 'curve': portfolio['curve'][-250:], 'attempted': []} if portfolio else None)
        # .get(), not f[k]: older frozen forecast records predate 'hotmoney'
        # and are immutable -- they can never gain the key, so the export must
        # tolerate its absence rather than KeyError on every historical record.
        result['agent']['forecasts'] = [{k: f.get(k) for k in ('id', 'created_at', 'as_of', 'eligible_from', 'symbol', 'name', 'score', 'strategy_type', 'probability', 'reference_price', 'paper_eligible', 'hotmoney')} for f in agent['forecasts'][:30]]
        result['agent']['outcomes'] = agent['outcomes'][:30]
        from decision_view import decisions
        result['agent']['decisions'] = decisions(agent)
        # exec-0.2 虚拟账户快照与候选池证据三层：旧报告没有这两项，用 .get 容忍缺失。
        result['agent']['exec02'] = agent.get('exec02')
        result['agent']['evidence'] = agent.get('evidence')
        result['agent']['baseline'] = agent.get('baseline')
        result['agent']['forecast_count'] = len(agent['forecasts'])
        result['agent']['outcome_count'] = len(agent['outcomes'])
        result['sources'].append({'kind': '候选预测与虚拟组合', 'id': agent_path.stem, 'generated_at': agent['generated_at'],
            'url': 'https://github.com/brycehu129/a-share-alpha-agent/blob/market-data/agent/' + agent_path.stem + '.md'})
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--history', type=Path, required=True)
    args = parser.parse_args()
    dest = args.history / 'dashboard/latest.json'
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(build(args.history), ensure_ascii=False, indent=2))
    print('Dashboard snapshot exported from verified archives')
