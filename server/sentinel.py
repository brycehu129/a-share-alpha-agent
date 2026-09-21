"""自选股哨兵：把规则触发、告警推送、情景研判、收盘对账串起来。

**告警和研判是两个进程，这是刻意的。**

盘中 tick 进程（每分钟一轮，systemd 给它 50 秒）只做快的事：跑规则、**立刻**推一条只含规则层
事实的告警、把事件和当时的事实存进待分析队列。AI 分析（自适应思考，可能 30–90 秒）由另一个
独立进程稍后取队列处理。这样：AI 慢了、挂了、被限流了，都不影响你在几秒内收到告警——
最需要快的东西不该被最慢的东西拖住。告警文字必须自己就能读懂（谁、现价、为什么触发、
你的成本/止损），情景研判是随后追加的一条，不是告警的前提。

规则见 sentinel_rules.py，情景与校验见 scenario_analyst.py，资金流与盘口事实见 money_flow.py，
留档与收盘对账见 scenario_ledger.py。

只推可操作的事件；系统只提醒、永远不下单。
"""
import argparse
import fcntl
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import flow_recorder
import intraday_engine as ie
import live_check
import live_quote
import minute_data
import money_flow
import portfolio_book
import scenario_analyst
import scenario_ledger
import sentinel_rules as sr
from collect_quotes import CST

INDEX_SYMBOLS = ['sh000001', 'sz399006', 'sh000300']       # 大盘环境：一起取，同一批请求，不额外花钱
INDEX_LABEL = {'sh000001': '上证', 'sz399006': '创业板', 'sh000300': '沪深300'}
ALERT_MINUTE_BUDGET_S = 15     # 告警阶段取分时（均价线）的总时间预算：超了就不再取，告警照发
ALERT_FLOW_BUDGET_S = 30       # 告警阶段联网取资金流的截止时刻（自 after_tick 开始计）：过了就不再取，告警照发
FLOW_RECORD_MAX_AGE_S = 600    # 采集器留存的资金流不超过这个年龄就直接用，省一次联网
PENDING_MAX_AGE_S = 900        # 待分析事件超过 15 分钟就不再分析：那时的情景早已过时
AI_DAILY_MAX = int(os.environ.get('SENTINEL_AI_MAX_PER_DAY', '15'))
HINT_LABEL = {'hold': '继续持有', 'add': '可考虑买入/加仓', 'reduce': '可考虑减仓', 'wait': '观望',
              't_sell_high': '高位，可考虑用底仓先卖（做T）', 't_buy_low': '低位，可考虑买回（做T）'}
DISCLAIMER = '研究参考，不构成投资建议；系统只提醒、不下单，决定由你自己做。'


def sdir(directory=None):
    return scenario_ledger.sentinel_dir(directory)


def _append(directory, name, record):
    d = sdir(directory)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / name, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, path)


# --- 推送 -------------------------------------------------------------------

def wecom_send(text):
    """推企业微信，分段。失败只返回原因，绝不抛出——推送失败不能让哨兵每分钟崩一次。"""
    from wecom_push import ConfigError, PushError, load_config, send_wecom_message
    from postclose_report import chunk_text
    path = os.environ.get('CONFIG_PATH') or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                         'data', 'webapp_config.json')
    try:
        hook = load_config(path).get('webhook_url')
    except (ConfigError, OSError, ValueError) as exc:
        return {'sent': False, 'reason': '读取 webhook 配置失败: %s' % exc}
    if not hook:
        return {'sent': False, 'reason': '尚未配置企业微信 webhook'}
    chunks = chunk_text(text)
    for i, chunk in enumerate(chunks):
        try:
            send_wecom_message(hook, chunk)
        except PushError as exc:
            return {'sent': i > 0, 'chunks': i, 'reason': str(exc)}
    return {'sent': True, 'chunks': len(chunks)}


# --- 告警文字（必须自己就能读懂）------------------------------------------------

def format_alert(block):
    """block: {'symbol','name','events':[...],'quote','holding','day','urgent'}。"""
    q = block['quote']
    last, change = float(q['last']), float(q.get('change_pct') or 0)
    head = '%s%s %s  现价 %.2f（%+.2f%%）' % ('【紧急】' if block['urgent'] else '【哨兵】', block['name'], block['symbol'], last, change)
    lines = [head] + ['• ' + e['detail'] for e in block['events']]
    day = block.get('day') or {}
    ctx = []
    if day.get('vwap'):
        ctx.append('均价线 %.2f' % day['vwap'])
    if day.get('day_low') and day.get('day_high'):
        ctx.append('日内 %.2f–%.2f' % (day['day_low'], day['day_high']))
    if q.get('volume_ratio'):
        ctx.append('量比 %s' % q['volume_ratio'])
    if ctx:
        lines.append('｜'.join(ctx))
    flow = money_flow.flow_line(block.get('flow'), block.get('book'))
    if flow:
        lines.append(flow)
    h = block.get('holding')
    if h:
        own = ['持仓 %d股 成本 %.2f（%+.2f%%）' % (h['shares'], h['cost_price'], h['unrealized_pct'])]
        if h.get('stop_price'):
            own.append('止损 %.2f' % h['stop_price'])
        if h.get('target_price'):
            own.append('目标 %.2f' % h['target_price'])
        lines.append('｜'.join(own))
    return '\n'.join(lines)


def format_scenarios(entry, name, last):
    lines = ['【情景研判】%s %s  现价 %.2f' % (name, entry['symbol'], last), '现状：' + entry['current_read']]
    for i, sc in enumerate(entry['scenarios']):
        arrow = '↑' if sc['direction'] == 'up' else '↓'
        lines.append('情景%s %s %s：若%s %.2f → 目标 %.2f–%.2f；触及 %.2f 则判断失效%s' % (
            'ABC'[i], arrow, sc['label'], '站上' if sc['direction'] == 'up' else '跌破', sc['trigger_price'],
            sc['target_low'], sc['target_high'], sc['invalidate_price'],
            '（' + sc['trigger_condition'] + '）' if sc.get('trigger_condition') else ''))
    if entry.get('watch_metrics'):
        lines.append('需要盯：' + '；'.join(entry['watch_metrics'][:3]))
    lines.append('倾向：%s｜依据强度 %d/5（自评，不是胜率）' % (HINT_LABEL.get(entry['action_hint'], entry['action_hint']), entry['confidence']))
    lines.append('%s 情景已存档，收盘后自动对账。' % DISCLAIMER)
    return '\n'.join(lines)


# --- 哨兵（注册进盘中引擎的评估器 + 事后处理）----------------------------------

class Sentinel:
    def __init__(self, history, book_fn=None, series_fn=None, send_fn=None, minute_fn=None, directory=None,
                 flow_fn=None, flow_dir=None):
        self.history = Path(history)
        self.directory = directory
        self.book_fn = book_fn or (lambda: (portfolio_book.load('holdings'), portfolio_book.load('watchlist')))
        self.series_fn = series_fn or (lambda s: live_check.load_series(self.history, s)[0])
        self.send_fn = send_fn or wecom_send
        self.minute_fn = minute_fn or minute_data.fetch_minute
        self.flow_fn = flow_fn or money_flow.fetch_flow
        self.flow_dir = flow_dir
        self.cache, self.market = {}, {}

    def symbols(self):
        holdings, watch = self.book_fn()
        return sorted({r['symbol'] for r in holdings} | {r['symbol'] for r in watch}) + INDEX_SYMBOLS

    def evaluate(self, tick):
        holdings, watch = self.book_fn()
        hold, wl = {h['symbol']: h for h in holdings}, {w['symbol']: w for w in watch}
        quotes = tick['quotes']
        self.market = {INDEX_LABEL[s]: float(quotes[s]['change_pct']) for s in INDEX_SYMBOLS if s in quotes}
        signals = []
        for symbol in sorted(set(hold) | set(wl)):
            q = quotes.get(symbol)
            if not q:
                continue
            h, w = hold.get(symbol), wl.get(symbol)
            name = q.get('name') or (h or w).get('name') or symbol
            bars = self.series_fn(symbol)
            facts, issues = live_check.price_facts(bars, q) if bars else ({}, ['本地没有日线缓存，均线类信号不可用'])
            if (facts.get('adjustment_drift_pct') or 0) > live_check.ADJUST_TOLERANCE_PCT:
                facts = {}          # 除权/缓存过期：均线不可信，宁可不触发均线类信号
            limits = live_check.limit_facts(q)
            day, minute = None, None
            if h:
                signals += sr.holding_signals({**h, 'name': name}, q, facts, limits)
                if h.get('t_base_shares'):
                    try:
                        minute = tick['minutes'](symbol)
                        day = sr.day_facts(q, minute)
                        signals += sr.t_signals({**h, 'name': name}, q, day)
                    except minute_data.MinuteError as exc:
                        issues.append('分时数据不可用，做T提示暂停：%s' % exc)
                if w:               # 既持有又在自选：持仓信号已覆盖，只保留自选独有的买入区间
                    signals += [x for x in sr.watch_signals({**w, 'name': name}, q, facts, limits)
                                if x['key'].startswith('buy-zone')]
            else:
                signals += sr.watch_signals({**w, 'name': name}, q, facts, limits)
            signals += sr.node_signals(symbol, name, tick['node_due'])
            self.cache[symbol] = {'quote': q, 'facts': facts, 'limits': limits, 'holding': h, 'watch': w,
                                  'name': name, 'day': day, 'minute': minute, 'issues': issues}
        return signals

    # -- 告警 ------------------------------------------------------------------
    def after_tick(self, summary, now=None):
        """引擎跑完一轮后调用：为本轮触发的哨兵事件推告警并入队。返回处理摘要。"""
        now = now or datetime.now(CST)
        events = [e for e in summary.get('events', []) if e['kind'].startswith('sentinel.')]
        if not events or summary.get('dry_run'):
            return {'alerts': 0}
        by_symbol = {}
        for e in events:
            by_symbol.setdefault(e['symbol'], []).append(e)
        blocks, pending_ids = [], []
        started = time.monotonic()
        for symbol, evs in by_symbol.items():
            c = self.cache.get(symbol)
            if not c:
                continue
            minute, day = c['minute'], c['day']
            # 告警要带均价线和图：需要分时；取不到就没有，不影响告警。每只最多 20 秒，5 只就是
            # 100 秒，而 tick 服务的 systemd 时限只有 50 秒——被杀掉的话告警反而发不出去。所以
            # 给整个取分时阶段一个总预算，超了就不再取。
            if minute is None and time.monotonic() - started < ALERT_MINUTE_BUDGET_S:
                try:
                    minute = self.minute_fn(symbol, now)
                    if minute['trade_date'] != now.date().isoformat():
                        minute = None
                    else:
                        day = sr.day_facts(c['quote'], minute)
                except Exception:
                    minute = None
            holding = live_check.holding_facts(c['holding'], c['quote']) if c['holding'] else None
            levels = scenario_analyst.key_levels(c['quote'], c['facts'], day or {}, c['holding'], c['limits'])
            pending_ids.append(self._enqueue(now, symbol, c, evs, day, holding, levels, minute))
            # 固定时间节点（09:45/13:05/14:30）本身不是"可操作事件"——单独推一条"开盘方向确立"
            # 没有任何信息，5 只股票 × 3 个节点每天就是 15 条噪音。它的价值是触发情景研判：
            # 节点事件只入队，等研判进程给出带价位的情景后才推那一条有内容的消息。
            # 和真正的事件同时触发时，才附在告警里作为背景。
            actionable = [e for e in evs if not e['kind'].startswith('sentinel.node_')]
            if not actionable:
                continue
            # 资金流是事实展示，不是告警的前提：取不到就没有这一行，告警照发。
            flow = self._flow(symbol, now, started)
            blocks.append({'symbol': symbol, 'name': c['name'], 'events': evs, 'quote': c['quote'], 'holding': holding,
                           'day': day, 'urgent': any(e['severity'] == 'urgent' for e in evs),
                           'flow': flow, 'book': money_flow.book_facts(c['quote'])})
        if not blocks:
            return {'alerts': 0, 'pending': pending_ids}
        blocks.sort(key=lambda b: not b['urgent'])
        texts = [format_alert(b) for b in blocks]
        text = '\n\n'.join(texts) + '\n\n' + DISCLAIMER
        result = self.send_fn(text)
        # blocks：按股票拆开的一份（界面按股票查告警用）。text 仍是推送出去的完整原文。
        stored = [{'symbol': b['symbol'], 'name': b['name'], 'urgent': b['urgent'], 'text': t,
                   'price': b['quote'].get('last'), 'change_pct': b['quote'].get('change_pct'),
                   'events': [{'kind': e['kind'], 'severity': e['severity'], 'detail': e['detail']} for e in b['events']],
                   'flow': money_flow.compact(b['flow']), 'book': b['book']} for b, t in zip(blocks, texts)]
        _append(self.directory, 'alerts-%s.jsonl' % now.strftime('%Y-%m-%d'),
                {'at': now.isoformat(), 'symbols': [b['symbol'] for b in blocks], 'text': text, 'push': result,
                 'blocks': stored,
                 'events': [{'key': e['key'], 'kind': e['kind'], 'evidence': e.get('evidence')} for e in events]})
        return {'alerts': len(blocks), 'push': result, 'pending': pending_ids}

    def _flow(self, symbol, now, started):
        """告警当时的资金流。优先用采集器最近一次留存（不联网），没有再联网取一次；总预算用完就放弃。"""
        try:
            rows = flow_recorder.series(self.flow_dir or ie.data_dir(), now.date().isoformat(), symbol)
        except OSError:
            rows = []
        if rows and (now - datetime.fromisoformat(rows[-1]['at'])).total_seconds() <= FLOW_RECORD_MAX_AGE_S:
            return rows[-1]
        if time.monotonic() - started >= ALERT_FLOW_BUDGET_S:
            return None
        try:
            return self.flow_fn(symbol, now)
        except Exception:
            return None

    def _enqueue(self, now, symbol, c, events, day, holding, levels, minute):
        pid = '%s-%s-%s' % (now.strftime('%Y%m%d-%H%M%S'), symbol, uuid.uuid4().hex[:6])
        recent = [(b['t'], b['price']) for b in (minute['bars'][-30:] if minute else [])][::3]
        entry = {'symbol': symbol, 'name': c['name'], 'roles': [r for r, ok in (('holding', c['holding']), ('watchlist', c['watch'])) if ok],
                 'triggers': [{'kind': e['kind'], 'detail': e['detail']} for e in events],
                 'quote': {k: c['quote'].get(k) for k in ('last', 'previous_close', 'open', 'high', 'low', 'change_pct',
                                                          'volume_ratio', 'turnover_pct', 'amplitude_pct')},
                 'price_facts': c['facts'], 'day': day, 'limit_facts': c['limits'], 'holding': holding,
                 'watch': {k: c['watch'].get(k) for k in ('intent', 'buy_low', 'buy_high')} if c['watch'] else None,
                 'key_levels': levels, 'recent_minutes': recent, 'issues': c['issues']}
        _write_json(sdir(self.directory) / 'pending' / (pid + '.json'), {
            'id': pid, 'created_at': now.isoformat(), 'symbol': symbol, 'market': self.market,
            'node': max(events, key=lambda e: e['severity'] == 'urgent')['kind'],
            'is_holding': bool(c['holding']), 't_base': (c['holding'] or {}).get('t_base_shares') or 0,
            'limits': {k: c['limits'].get(k) for k in ('limit_up', 'limit_down')}, 'entry': entry})
        return pid


# --- 研判进程 ---------------------------------------------------------------

def ai_count(directory, day):
    p = sdir(directory) / ('ai-%s.json' % day)
    return json.loads(p.read_text())['calls'] if p.exists() else 0


def bump_ai(directory, day, meta):
    p = sdir(directory) / ('ai-%s.json' % day)
    data = json.loads(p.read_text()) if p.exists() else {'calls': 0, 'log': []}
    data['calls'] += 1
    data['log'].append({k: meta.get(k) for k in ('requested_at', 'model', 'input_tokens', 'output_tokens', 'status')})
    _write_json(p, data)


def analyze_pending(directory=None, now=None, snapshot_fn=None, analyze_fn=None, send_fn=None, ai_ok_fn=None,
                    max_calls=None):
    """取待分析队列，调一次 AI，校验、留档、推情景。返回摘要。永远不抛出。"""
    now = now or datetime.now(CST)
    day = now.strftime('%Y-%m-%d')
    snapshot_fn = snapshot_fn or live_quote.snapshot
    analyze_fn = analyze_fn or scenario_analyst.analyze
    send_fn = send_fn or wecom_send
    max_calls = AI_DAILY_MAX if max_calls is None else max_calls
    d = sdir(directory)
    files = sorted((d / 'pending').glob('*.json')) if (d / 'pending').is_dir() else []
    if not files:
        return {'processed': 0}
    d.mkdir(parents=True, exist_ok=True)
    lock = open(d / '.analyst.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return {'skipped': '上一轮分析仍在运行'}
    try:
        return _analyze(d, directory, files, now, day, snapshot_fn, analyze_fn, send_fn, ai_ok_fn, max_calls)
    except Exception as exc:
        return {'error': '%s: %s' % (type(exc).__name__, str(exc)[:200])}
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _finish(d, path, status, **extra):
    done = d / 'done'
    done.mkdir(exist_ok=True)
    item = json.loads(path.read_text(encoding='utf-8'))
    _write_json(done / path.name, {**item, 'status': status, 'finished_at': datetime.now(CST).isoformat(), **extra})
    path.unlink()


def _analyze(d, directory, files, now, day, snapshot_fn, analyze_fn, send_fn, ai_ok_fn, max_calls):
    started_at = time.monotonic()
    items = []
    for p in files:
        it = json.loads(p.read_text(encoding='utf-8'))
        age = (now - datetime.fromisoformat(it['created_at'])).total_seconds()
        if age > PENDING_MAX_AGE_S:
            _finish(d, p, 'expired', reason='超过 %d 秒未处理，情景已过时' % PENDING_MAX_AGE_S)
        else:
            items.append((p, it))
    if not items:
        return {'processed': 0, 'expired': len(files)}
    if ai_ok_fn is None:
        import claude_client
        ai_ok_fn = claude_client.available
    ok, why = ai_ok_fn()
    if not ok:
        for p, _ in items:
            _finish(d, p, 'no_ai', reason=why)
        return {'processed': 0, 'no_ai': why}
    if ai_count(directory, day) >= max_calls:
        for p, _ in items:
            _finish(d, p, 'budget', reason='今日 AI 调用已达上限 %d 次，只推规则层告警' % max_calls)
        return {'processed': 0, 'budget': True}

    # 合并同一只股票的多条待分析事件：触发原因并到一起，用最新的那份事实
    merged = {}
    for p, it in items:
        cur = merged.setdefault(it['symbol'], {'files': [], 'item': it})
        cur['files'].append(p)
        if it is not cur['item']:
            it['entry']['triggers'] = cur['item']['entry']['triggers'] + it['entry']['triggers']
            cur['item'] = it

    # 研判前重新取一次报价：AI 要 30–90 秒，价格可能已经越过它将要给的触发价。
    snap = snapshot_fn(list(merged))
    fresh = {q['symbol']: q for q in snap['quotes']
             if q.get('age_seconds') is not None and q['age_seconds'] <= 180 and q.get('quote_date') == day}
    payload_entries, kept = [], {}
    for sym, m in merged.items():
        if sym not in fresh:
            for p in m['files']:
                _finish(d, p, 'stale_quote', reason='研判前取不到新鲜报价，不分析')
            continue
        entry = m['item']['entry']
        entry['quote']['last'] = fresh[sym]['last']
        entry['quote_refreshed_at'] = now.isoformat()
        payload_entries.append(entry)
        kept[sym] = m
    if not payload_entries:
        return {'processed': 0, 'stale': True}
    market = next(iter(kept.values()))['item'].get('market') or {}
    data, meta = analyze_fn(scenario_analyst.build_payload(payload_entries, market))
    bump_ai(directory, day, meta)
    if data is None:
        for m in kept.values():
            for p in m['files']:
                _finish(d, p, 'ai_failed', meta=meta)
        return {'processed': 0, 'ai_failed': meta.get('status'), 'error': meta.get('error')}

    dropped, texts, to_record = [], [], []
    returned = {e.get('symbol'): e for e in data.get('stocks', [])}
    for sym, m in kept.items():
        item, last = m['item'], float(fresh[sym]['last'])
        e = returned.get(sym)
        if e is None:
            dropped.append('%s: 模型未返回' % sym)
            continue
        e, problems = scenario_analyst.validate_entry(
            e, last, item['limits'].get('limit_up'), item['limits'].get('limit_down'),
            is_holding=item['is_holding'], t_base=item['t_base'])
        dropped += ['%s: %s' % (sym, x) for x in problems]
        if not e['scenarios']:
            continue                        # 没有一个合格情景就不推：没有可对账的东西，只会是噪音
        texts.append(format_scenarios(e, item['entry']['name'], last))
        to_record.append((sym, item, last, e))
    # 一次研判只推**一条**合并消息，而不是每只股票一条：同一轮研判常常涵盖好几只（比如 09:45 节点
    # 五只一起），逐只推一天就是几十条，违背"只推可操作事件"。
    push = send_fn('\n\n'.join(texts)) if texts else {'sent': False, 'reason': '没有合格的情景'}
    # 情景的"发布时间"= 用户真正收到它的时刻 = 分析开始时间 + AI 实际耗时。用 now 参数而不是
    # datetime.now()：既真实，也让回放/测试里情景和它所属的交易日时间轴保持一致。
    issued = (now + timedelta(seconds=time.monotonic() - started_at)).isoformat()
    pushed = len(texts) if push.get('sent') else 0
    for sym, item, last, e in to_record:
        scenario_ledger.record(directory, day, issued, sym, item['entry']['name'], item['node'], last, e['scenarios'],
                               e['action_hint'], e['confidence'], item['is_holding'],
                               meta={'prompt_version': meta.get('prompt_version'), 'model': meta.get('model'),
                                     'triggers': [t['kind'] for t in item['entry']['triggers']]},
                               delivered=bool(push.get('sent')))
    for m in kept.values():
        for p in m['files']:
            _finish(d, p, 'done', meta={k: meta.get(k) for k in ('model', 'input_tokens', 'output_tokens', 'prompt_version')},
                    problems=dropped)
    return {'processed': len(kept), 'pushed': pushed, 'dropped': dropped}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['analyze', 'reconcile', 'weekly'])
    p.add_argument('--day', default=datetime.now(CST).strftime('%Y-%m-%d'))
    p.add_argument('--no-push', action='store_true')
    a = p.parse_args()
    import llm_settings
    llm_settings.apply()      # 页面保存的 key/模型优先于环境变量
    if a.command == 'analyze':
        print(json.dumps(analyze_pending(), ensure_ascii=False, default=str))
        return 0
    if a.command == 'reconcile':
        r = scenario_ledger.reconcile(None, a.day, minute_data.fetch_minute)
        judged = [x for x in r['results'] if x['outcome']]
        print('%s 对账 %d 条，有判定 %d 条' % (a.day, len(r['results']), len(judged)))
        if datetime.strptime(a.day, '%Y-%m-%d').weekday() == 4 and not a.no_push:     # 周五顺带推周报
            print(wecom_send(scenario_ledger.render_weekly(scenario_ledger.weekly(None, a.day))))
        return 0
    print(scenario_ledger.render_weekly(scenario_ledger.weekly(None, a.day)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
