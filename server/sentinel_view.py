"""哨兵的数据：今日告警、情景、收盘对账、采集健康、单只股票的告警与资金流（接口 /api/sentinel*、/api/book）。

这里只读磁盘上的留档，不联网——联网取"当前资金流"是 api_pages 那一层的事，取不到不能拖垮这里。
告警详情挂在持仓/自选的每一行上：按股票查靠的是每条告警里的 `blocks`（按股票拆开的一份）；
没有 `blocks` 的旧记录退回去按 `symbols` 匹配，并从推送原文里切出那只股票的段落。
"""
import json
import re
from collections import Counter
from datetime import datetime

import flow_recorder
import intraday_engine as ie
import money_flow
import scenario_ledger
from collect_quotes import CST

DAY_RE = re.compile(r'\d{4}-\d{2}-\d{2}')
SYMBOL_RE = re.compile(r'(sh|sz)\d{6}')
HINT_LABEL = {'hold': '继续持有', 'add': '买入/加仓', 'reduce': '减仓', 'wait': '观望',
              't_sell_high': '高位做T', 't_buy_low': '低位做T'}


def safe_day(raw):
    """日期参数直接拼进文件名，必须严格校验，否则 ?day=../../x 就是路径穿越。"""
    if raw and DAY_RE.fullmatch(raw):
        try:
            datetime.strptime(raw, '%Y-%m-%d')
            return raw
        except ValueError:
            pass
    return datetime.now(CST).strftime('%Y-%m-%d')


def safe_symbol(raw):
    """股票代码同样会拼进查找条件；只认沪深个股格式。"""
    return raw if isinstance(raw, str) and SYMBOL_RE.fullmatch(raw) else None


def _jsonl(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue           # 半行（写入中被读到）跳过，不让页面崩
    return out


def _intraday(intraday_dir):
    return intraday_dir if intraday_dir is not None else ie.data_dir()


def blocks_of(alert):
    """一条告警里按股票拆开的块。新记录直接有；旧记录按 symbols + 推送原文切出来（切不出就给整段原文）。"""
    if alert.get('blocks'):
        return alert['blocks']
    parts = re.split(r'\n\n+', alert.get('text') or '')
    out = []
    for sym in alert.get('symbols') or []:
        text = next((p for p in parts if sym in p.split('\n', 1)[0]), None)
        head = (text or '').split('\n', 1)[0]
        out.append({'symbol': sym, 'name': '', 'urgent': '【紧急】' in head, 'text': text or alert.get('text') or '',
                    'events': [], 'flow': None, 'book': None, 'price': None, 'change_pct': None})
    return out


def _push_fields(alert):
    push = alert.get('push') or {}
    return bool(push.get('sent')), '' if push.get('sent') else str(push.get('reason', ''))


def scenario_rows(scenarios, symbol=None):
    rows = []
    for r in sorted(scenarios, key=lambda r: r['issued_at']):
        if symbol and r['symbol'] != symbol:
            continue
        sc = r['scenario']
        outcome = scenario_ledger.OUTCOME_LABEL.get(r['outcome'], '尚未对账' if r['outcome'] is None else r['outcome'])
        rows.append({
            'time': r['issued_at'][11:19], 'name': r.get('name') or '', 'symbol': r['symbol'],
            'direction': sc['direction'], 'label': sc['label'], 'trigger_price': sc['trigger_price'],
            'target_low': sc['target_low'], 'target_high': sc['target_high'],
            'invalidate_price': sc['invalidate_price'], 'confidence': r['confidence'],
            'action_hint': HINT_LABEL.get(r['action_hint'], r['action_hint']), 'outcome': outcome})
    return rows


def _pick_scenario(rows, direction):
    """两份独立实现过一样的"挑最近情景"逻辑，字符串还一度不一致（兜底文案）——统一到
    sentinel.py（哨兵推送格式的权威来源），这里只是按 scenario_ledger.load_joined() 的行形状
    （{'scenario':..., 'price_at_issue':...}）转一层再委托过去。函数内 import：sentinel.py
    顶部无条件 `import fcntl`，只在真调用时才付这个代价（Windows 本地也不受影响）。"""
    if not rows:
        return None
    import sentinel
    return sentinel._nearest_scenario([r['scenario'] for r in rows], direction, rows[0]['price_at_issue'])


def _glance(rows):
    import sentinel
    return sentinel.render_glance(_pick_scenario(rows, 'up'), _pick_scenario(rows, 'down'))


def _kind_label(kind):
    import sentinel
    return sentinel.kind_label(kind)


def _source_label(rows):
    kinds = []
    for r in rows:
        for kind in r.get('triggers') or [r.get('node')]:
            if kind and kind not in kinds:
                kinds.append(kind)
    return ' + '.join(_kind_label(kind) for kind in kinds[:2]) if kinds else _kind_label(rows[0].get('node'))


def _outcome_summary(rows):
    counts = Counter(r.get('outcome') for r in rows)
    if counts.get(None) and len(counts) == 1:
        return '尚未对账'
    parts = []
    for key in scenario_ledger.OUTCOMES:
        count = counts.get(key, 0)
        if count:
            parts.append('%d%s' % (count, scenario_ledger.OUTCOME_LABEL[key]))
    if counts.get(None):
        parts.append('%d尚未对账' % counts[None])
    return ' / '.join(parts) if parts else '尚未对账'


def judgment_rows(scenarios, symbol=None):
    groups = {}
    for r in sorted(scenarios, key=lambda r: (r['issued_at'], r['symbol'], r['id'])):
        if symbol and r['symbol'] != symbol:
            continue
        key = (r['issued_at'], r['symbol'], r.get('node'))
        groups.setdefault(key, []).append(r)
    rows = []
    for (_, _, _), items in groups.items():
        first = items[0]
        rows.append({
            'time': first['issued_at'][11:19],
            'name': first.get('name') or '',
            'symbol': first['symbol'],
            'source': _source_label(items),
            'action_hint': HINT_LABEL.get(first['action_hint'], first['action_hint']),
            'glance': _glance(items),
            'outcome': _outcome_summary(items),
            'confidence': first['confidence'],
            'scenario_count': len(items),
        })
    return rows


def collection(day, symbol=None, intraday_dir=None, now=None):
    """采集健康：盘中轮询跑了几轮、断了几次、为什么跳过，以及资金流采集了几次。不联网。"""
    d = _intraday(intraday_dir)
    h = ie.health(d, day, now)
    fl = flow_recorder.collection_summary(d, day)
    out = {'ticks': h['ticks'], 'expected': h['expected'], 'first_tick': h['first_tick'], 'last_tick': h['last_tick'],
           'gap_count': h['gap_count'], 'gap_seconds': h['gap_seconds'], 'gaps': h['gaps'], 'skips': h['skips'],
           'flow_runs': fl['runs']}
    if symbol:
        f = fl['symbols'].get(symbol, {'ok': 0, 'error': 0, 'last_error': None})
        out.update(observations=h['observations'].get(symbol, 0), flow_ok=f['ok'], flow_error=f['error'],
                   flow_last_error=f['last_error'])
    else:
        out['symbols'] = len(h['observations'])
    return out


def sentinel_payload(day=None, directory=None, intraday_dir=None):
    """给"全天告警汇总"抽屉的结构化数据：告警、情景（含收盘对账）、采集健康、周报。"""
    day = safe_day(day)
    d = scenario_ledger.sentinel_dir(directory)
    alerts = _jsonl(d / ('alerts-%s.jsonl' % day))
    scenarios = scenario_ledger.load_joined(directory, [day])
    ai_path = d / ('ai-%s.json' % day)
    ai_calls = json.loads(ai_path.read_text())['calls'] if ai_path.exists() else 0
    pending = len(list((d / 'pending').glob('*.json'))) if (d / 'pending').is_dir() else 0

    alert_rows = []
    for a in reversed(alerts[-30:]):
        sent, reason = _push_fields(a)
        alert_rows.append({'time': a['at'][11:19], 'text': a['text'], 'sent': sent, 'reason': reason,
                           'symbols': list(a.get('symbols') or [])})
    try:
        weekly = scenario_ledger.render_weekly(scenario_ledger.weekly(directory, day))
    except Exception:
        weekly = None
    try:
        coll = collection(day, None, intraday_dir)
    except Exception:
        coll = None
    return {'day': day, 'alerts': alert_rows, 'judgments': judgment_rows(scenarios), 'scenarios': scenario_rows(scenarios), 'ai_calls': ai_calls,
            'pending': pending, 'weekly': weekly or '', 'collection': coll}


def alert_counts(day=None, directory=None):
    """{symbol: {'count': 今日告警条数, 'urgent': 其中紧急条数}}。给持仓/自选页每行的按钮用；
    读不出来就当没有——不能让一个坏文件拖垮持仓页。"""
    day = safe_day(day)
    try:
        alerts = _jsonl(scenario_ledger.sentinel_dir(directory) / ('alerts-%s.jsonl' % day))
    except OSError:
        return {}
    counts = {}
    for a in alerts:
        try:
            for b in blocks_of(a):
                c = counts.setdefault(b['symbol'], {'count': 0, 'urgent': 0})
                c['count'] += 1
                c['urgent'] += 1 if b.get('urgent') else 0
        except (KeyError, TypeError):
            continue
    return counts


def _hhmm(iso):
    return iso[11:13] + iso[14:16]


def book_series(symbol, day, intraday_dir=None):
    """bars-<day>.jsonl 里这只股票每分钟的外盘占比 {'HHMM': %}，用来配到资金流表的每一行上。"""
    path = _intraday(intraday_dir) / ('bars-%s.jsonl' % day)
    out = {}
    for r in _jsonl(path):
        if r.get('symbol') != symbol:
            continue
        facts = money_flow.book_facts(r)
        if 'outer_pct' in facts:
            out[_hhmm(r['at'])] = facts['outer_pct']
    return out


def recorded_table(symbol, day, intraday_dir=None):
    """资金流留存 → 每 30 分钟一行（含最后一行）的数据表，配上当时的外盘占比。"""
    rows = flow_recorder.series(_intraday(intraday_dir), day, symbol)
    if not rows:
        return []
    picks = [r for r in rows if r.get('as_of', '')[2:] in ('00', '30')]
    if picks[-1:] != rows[-1:]:
        picks.append(rows[-1])
    return with_outer([{'t': r['as_of'][:2] + ':' + r['as_of'][2:], **{k: r.get(k) for k in money_flow.FIELDS}}
                       for r in picks], symbol, day, intraday_dir)


def with_outer(table, symbol, day, intraday_dir=None):
    outer = book_series(symbol, day, intraday_dir)
    for row in table:
        hhmm = row['t'].replace(':', '')
        # 外盘占比按分钟对齐；那一分钟没有留存就取之前最近的一条（不向后取，避免用到"未来"的值）
        prior = [k for k in outer if k <= hhmm]
        row['outer_pct'] = outer[max(prior)] if prior else None
    return table


def symbol_payload(symbol, day=None, directory=None, intraday_dir=None):
    """某只股票当天的告警（含告警当时的资金流快照）、情景、留存的资金流表与采集次数。symbol 必须已通过 safe_symbol。"""
    day = safe_day(day)
    d = scenario_ledger.sentinel_dir(directory)
    alerts = []
    for a in reversed(_jsonl(d / ('alerts-%s.jsonl' % day))):
        sent, reason = _push_fields(a)
        for b in blocks_of(a):
            if b.get('symbol') == symbol:
                alerts.append({'time': a['at'][11:19], 'sent': sent, 'reason': reason, 'urgent': bool(b.get('urgent')),
                               'text': b.get('text') or '', 'events': b.get('events') or [], 'price': b.get('price'),
                               'change_pct': b.get('change_pct'), 'flow': b.get('flow'), 'book': b.get('book')})
    scenarios = scenario_rows(scenario_ledger.load_joined(directory, [day]), symbol)
    try:
        coll = collection(day, symbol, intraday_dir)
    except Exception:
        coll = None
    return {'symbol': symbol, 'day': day, 'judgments': judgment_rows(scenario_ledger.load_joined(directory, [day]), symbol),
            'alerts': alerts, 'scenarios': scenarios, 'collection': coll,
            'flow_table': recorded_table(symbol, day, intraday_dir)}
