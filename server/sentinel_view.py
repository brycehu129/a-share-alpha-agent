"""后台 /sentinel 页面：今日告警、情景、分时图、收盘对账结果。只读磁盘上的留档，不联网。

图是告警**当时**生成并存盘的 SVG，页面只是把它读出来——所以打开页面不会触发任何请求，
看到的就是当时你收到告警时的那张图，而不是现在的走势。
"""
import html
import json
import re
from datetime import datetime
from pathlib import Path

import scenario_ledger
import webapp_views
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


def render_sentinel_page(day=None, directory=None):
    day = safe_day(day)
    d = scenario_ledger.sentinel_dir(directory)
    alerts = _jsonl(d / ('alerts-%s.jsonl' % day))
    scenarios = scenario_ledger.load_joined(directory, [day])
    ai_path = d / ('ai-%s.json' % day)
    ai_calls = json.loads(ai_path.read_text())['calls'] if ai_path.exists() else 0
    pending = len(list((d / 'pending').glob('*.json'))) if (d / 'pending').is_dir() else 0

    parts = ['<h1>哨兵</h1>',
             '<div class="panel"><form method="get" action="/sentinel" style="display:flex;gap:8px;align-items:end">'
             '<div style="flex:1"><label>日期</label><input type="text" name="day" value="%s"></div>'
             '<button type="submit" style="margin:0">查看</button></form>'
             '<p class="muted">今日告警 %d 条 · 情景 %d 条 · AI 调用 %d 次 · 待研判 %d 条。'
             '系统只提醒、不下单；情景是研究参考，不构成投资建议。</p></div>'
             % (html.escape(day), len(alerts), len(scenarios), ai_calls, pending)]

    parts.append('<h2>告警</h2>')
    if not alerts:
        parts.append('<p class="muted">这一天没有告警。</p>')
    for a in reversed(alerts[-30:]):
        push = a.get('push') or {}
        status = '已推送' if push.get('sent') else '未推送：%s' % html.escape(str(push.get('reason', '')))
        parts.append('<div class="panel"><div class="muted">%s · %s</div>'
                     '<pre style="white-space:pre-wrap;font:inherit;margin:6px 0 0">%s</pre></div>'
                     % (html.escape(a['at'][11:19]), status, html.escape(a['text'])))

    parts.append('<h2>情景与对账</h2>')
    if not scenarios:
        parts.append('<p class="muted">这一天没有情景。</p>')
    else:
        rows = []
        for r in sorted(scenarios, key=lambda r: r['issued_at']):
            sc = r['scenario']
            outcome = scenario_ledger.OUTCOME_LABEL.get(r['outcome'], '尚未对账' if r['outcome'] is None else r['outcome'])
            rows.append('<tr><td>%s</td><td>%s<br><span class="muted">%s</span></td><td>%s %s</td>'
                        '<td class="num">%.2f</td><td class="num">%.2f–%.2f</td><td class="num">%.2f</td>'
                        '<td class="num">%d</td><td>%s</td><td>%s</td></tr>' % (
                            html.escape(r['issued_at'][11:19]), html.escape(r.get('name') or ''), html.escape(r['symbol']),
                            '↑' if sc['direction'] == 'up' else '↓', html.escape(sc['label']), sc['trigger_price'],
                            sc['target_low'], sc['target_high'], sc['invalidate_price'], r['confidence'],
                            html.escape(HINT_LABEL.get(r['action_hint'], r['action_hint'])), html.escape(outcome)))
        parts.append('<table><thead><tr><th>时间</th><th>股票</th><th>情景</th><th class="num">触发价</th>'
                     '<th class="num">目标区间</th><th class="num">失效价</th><th class="num">依据强度</th>'
                     '<th>倾向</th><th>收盘对账</th></tr></thead><tbody>%s</tbody></table>' % ''.join(rows))
        parts.append('<p class="muted">依据强度是模型对判断依据的自评（1–5），<strong>不是胜率</strong>。'
                     '对账只用情景发布之后的分钟收盘价；分钟内的瞬间触及看不到，"未触发"可能有漏判。'
                     '情景命中率不等于交易盈利。</p>')

    charts = sorted((d / 'charts').glob('*-%s-*.svg' % day.replace('-', ''))) if (d / 'charts').is_dir() else []
    if charts:
        parts.append('<h2>告警时的分时图</h2><p class="muted">这是告警<strong>当时</strong>存下的图，不是现在的走势。'
                     '只画事实：分时、均价线、均线、日内高低、你的成本与止损；不画预测线。</p>')
        for c in charts[-8:]:
            svg = _safe_svg(c.read_text(encoding='utf-8'))
            if svg:
                parts.append('<div class="panel">%s<div class="muted">%s</div></div>' % (svg, html.escape(c.name)))

    try:
        weekly = scenario_ledger.render_weekly(scenario_ledger.weekly(directory, day))
    except Exception:
        weekly = None
    if weekly:
        parts.append('<h2>情景周报（截至该日的 7 天）</h2><div class="panel"><pre style="white-space:pre-wrap;font:inherit;margin:0">%s</pre></div>'
                     % html.escape(weekly))
    return webapp_views.page('哨兵', '/sentinel', ''.join(parts))
