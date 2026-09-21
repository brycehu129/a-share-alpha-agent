"""哨兵页的数据：今日告警、情景、分时图、收盘对账结果（接口 /api/sentinel）。只读磁盘上的留档，不联网。

图是告警**当时**生成并存盘的 SVG，接口只是把它读出来——所以打开页面不会触发任何行情请求，
看到的就是当时你收到告警时的那张图，而不是现在的走势。
"""
import json
import re
from datetime import datetime
from pathlib import Path

import scenario_ledger
from collect_quotes import CST

DAY_RE = re.compile(r'\d{4}-\d{2}-\d{2}')
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


def _safe_svg(text):
    """图是我们自己生成的，但页面把它内联进 HTML，所以仍然拒绝任何带脚本/外链的内容。"""
    low = text.lower()
    if not low.lstrip().startswith('<svg') or any(x in low for x in ('<script', 'onload', 'onerror', 'javascript:', 'href=')):
        return None
    return text


def sentinel_payload(day=None, directory=None):
    """给前端的结构化数据：告警、情景（含收盘对账）、分时图 SVG、周报。全部来自磁盘留档，不联网。"""
    day = safe_day(day)
    d = scenario_ledger.sentinel_dir(directory)
    alerts = _jsonl(d / ('alerts-%s.jsonl' % day))
    scenarios = scenario_ledger.load_joined(directory, [day])
    ai_path = d / ('ai-%s.json' % day)
    ai_calls = json.loads(ai_path.read_text())['calls'] if ai_path.exists() else 0
    pending = len(list((d / 'pending').glob('*.json'))) if (d / 'pending').is_dir() else 0

    alert_rows = []
    for a in reversed(alerts[-30:]):
        push = a.get('push') or {}
        alert_rows.append({'time': a['at'][11:19], 'text': a['text'], 'sent': bool(push.get('sent')),
                           'reason': '' if push.get('sent') else str(push.get('reason', ''))})

    scenario_rows = []
    for r in sorted(scenarios, key=lambda r: r['issued_at']):
        sc = r['scenario']
        outcome = scenario_ledger.OUTCOME_LABEL.get(r['outcome'], '尚未对账' if r['outcome'] is None else r['outcome'])
        scenario_rows.append({
            'time': r['issued_at'][11:19], 'name': r.get('name') or '', 'symbol': r['symbol'],
            'direction': sc['direction'], 'label': sc['label'], 'trigger_price': sc['trigger_price'],
            'target_low': sc['target_low'], 'target_high': sc['target_high'],
            'invalidate_price': sc['invalidate_price'], 'confidence': r['confidence'],
            'action_hint': HINT_LABEL.get(r['action_hint'], r['action_hint']), 'outcome': outcome})

    charts = []
    chart_files = sorted((d / 'charts').glob('*-%s-*.svg' % day.replace('-', ''))) if (d / 'charts').is_dir() else []
    for c in chart_files[-8:]:
        svg = _safe_svg(c.read_text(encoding='utf-8'))
        if svg:
            charts.append({'name': c.name, 'svg': svg})

    try:
        weekly = scenario_ledger.render_weekly(scenario_ledger.weekly(directory, day))
    except Exception:
        weekly = None
    return {'day': day, 'alerts': alert_rows, 'scenarios': scenario_rows, 'ai_calls': ai_calls,
            'pending': pending, 'charts': charts, 'weekly': weekly or ''}
