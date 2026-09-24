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

import alert_ledger
import book_levels
import book_state
import book_verdict
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
import t_context
from collect_quotes import CST

INDEX_SYMBOLS = ['sh000001', 'sz399006', 'sh000300']       # 大盘环境：一起取，同一批请求，不额外花钱
INDEX_LABEL = {'sh000001': '上证', 'sz399006': '创业板', 'sh000300': '沪深300'}
ALERT_MINUTE_BUDGET_S = 15     # 告警阶段取分时（均价线）的总时间预算：超了就不再取，告警照发
ALERT_FLOW_BUDGET_S = 30       # 告警阶段联网取资金流的截止时刻（自 after_tick 开始计）：过了就不再取，告警照发
FLOW_RECORD_MAX_AGE_S = 600    # 采集器留存的资金流不超过这个年龄就直接用，省一次联网
PENDING_MAX_AGE_S = 900        # 待分析事件超过 15 分钟就不再分析：那时的情景早已过时
AI_DAILY_MAX = int(os.environ.get('SENTINEL_AI_MAX_PER_DAY', '15'))
HINT_LABEL = {'hold': '继续持有', 'add': '可考虑买入/加仓', 'reduce': '可考虑减仓', 'wait': '观望',
              't_sell_high': '高位，可考虑用老仓先卖（做T）', 't_buy_low': '低位，可考虑买回（做T）'}
# 2026-09-24 起两类推送（哨兵告警 format_alert、情景研判 format_scenario_message）都不再附
# 免责声明——用户反馈每条消息都念一遍是噪音；系统只提醒不下单这件事写在 STRATEGY.md/页面说明里。


TRIGGER_LABEL = {
    'sentinel.stop_hit': '触及止损位',
    'sentinel.target_hit': '触及止盈位',
    'sentinel.below_cost': '跌破成本',
    'sentinel.ma20_break': '跌破MA20',
    'sentinel.ma60_break': '跌破MA60',
    'sentinel.volume_surge_down': '放量下跌',
    'sentinel.low20_break_volume': '放量跌破20日低点',
    'sentinel.near_limit_down': '逼近跌停',
    'sentinel.near_limit_up': '逼近涨停',
    'sentinel.high20_break_volume': '放量突破20日高点',
    'sentinel.volume_surge': '量比突增',
    'sentinel.buy_signal': '具备买入信号',
    'sentinel.t_sell_high': '日内高位',
    'sentinel.t_buy_low': '日内低位',
    'sentinel.intraday_support_reclaim': '上穿盘中支撑',
    'sentinel.intraday_resistance_reject': '跌回盘中阻力',
    'sentinel.node_0945': '09:45 节点',
    'sentinel.node_1305': '13:05 节点',
    'sentinel.node_1430': '14:30 节点',
}


def kind_label(kind):
    """信号 kind → 人看的短标签。未收录的 kind 退化成"去掉前缀、下划线转空格"，不报错。"""
    if kind in TRIGGER_LABEL:
        return TRIGGER_LABEL[kind]
    tail = str(kind or '').split('.')[-1]
    if tail.startswith('node_') and len(tail) == 9:
        return '%s:%s 节点' % (tail[5:7], tail[7:9])
    return tail.replace('_', ' ')


def _nearest_scenario(scenarios, direction, last):
    """给定情景列表和现价，挑"方向匹配、还没被触及"的里离现价最近的一个。up 挑触发价最低的
    （最快达到）、down 挑触发价最高的——都是"最近发生"的那一个。"""
    items = [sc for sc in (scenarios or []) if sc.get('direction') == direction]
    if direction == 'up':
        items = [sc for sc in items if sc.get('trigger_price', 0) > last]
        return min(items, key=lambda sc: sc['trigger_price']) if items else None
    items = [sc for sc in items if sc.get('trigger_price', 0) < last]
    return max(items, key=lambda sc: sc['trigger_price']) if items else None


def render_glance(up, down):
    """一行速览：只用于给两个已经挑好的情景（可以是 None）渲染文字。up/down 的形状见
    scenario_analyst._SCENARIO（至少要有 trigger_price/target_low/target_high）。"""
    parts = []
    if up:
        parts.append('上破 %.2f 看 %.2f–%.2f' % (up['trigger_price'], up['target_low'], up['target_high']))
    if down:
        parts.append('下破 %.2f 转弱' % down['trigger_price'])
    return '；'.join(parts) if parts else '暂无明确触发价，先观望'


def scenario_glance(entry, last):
    return render_glance(_nearest_scenario(entry.get('scenarios'), 'up', last),
                         _nearest_scenario(entry.get('scenarios'), 'down', last))


def _action_brief(hint):
    return {
        'hold': '偏持有',
        'add': '偏等确认后再加',
        'reduce': '偏先减仓',
        'wait': '先观望',
        't_sell_high': '偏高抛做T',
        't_buy_low': '偏低吸做T',
    }.get(hint, hint)


def _change_pct(quote, last):
    prev = (quote or {}).get('previous_close')
    return round((last / float(prev) - 1) * 100, 2) if prev else None


def _holding_line(holding):
    """情景消息里的持仓行：成本、综合盈亏（浮动+已实现，book_pnl.combined 算出来的）、
    等效成本、今天可卖老仓。holding 没传 trades 时没有 combined_pct/effective_cost，
    这一行就只有成本——不强求上游一定要算好综合盈亏。"""
    if not holding:
        return None
    parts = ['持仓 %d股 成本 %.2f' % (holding['shares'], holding['cost_price'])]
    if holding.get('combined_pct') is not None:
        parts.append('综合 %+.2f%%' % holding['combined_pct'])
    if holding.get('effective_cost') is not None and holding['effective_cost'] != holding['cost_price']:
        parts.append('等效成本 %.2f' % holding['effective_cost'])
    if holding.get('t_base_shares'):
        parts.append('可卖老仓 %d' % holding['t_base_shares'])
    return '｜'.join(parts)


def _trigger_line(triggers):
    """这条研判是被哪些哨兵规则触发的（entry['triggers']，_enqueue 时已经留档，
    此前 format_scenarios 没用上）。同名 kind 只列一次。"""
    if not triggers:
        return None
    seen, labels = set(), []
    for t in triggers:
        label = kind_label(t.get('kind'))
        if label not in seen:
            seen.add(label)
            labels.append(label)
    return '触发：' + '、'.join(labels)


def _scenario_line(sc):
    """情景必须自己就能对账：触发价、目标区间、失效价都是数值（scenario_analyst.validate_scenario
    已经校验过）。失效价是唯一能让人自证"这条判断错了"的字段，2026-09-22 那版精简时被砍掉——
    这里加回来，而且是每个情景强制带。"""
    up = sc['direction'] == 'up'
    arrow, trig_verb, inv_verb = ('↑', '站上', '跌回') if up else ('↓', '跌破', '站回')
    return '%s %s｜%s %.2f → %.2f–%.2f｜%s %.2f 则不成立' % (
        arrow, sc['label'], trig_verb, sc['trigger_price'], sc['target_low'], sc['target_high'],
        inv_verb, sc['invalidate_price'])


def format_scenarios(entry, name, last, change_pct=None, holding=None, triggers=None):
    """单只股票的情景块。entry 是 scenario_analyst.validate_entry() 校验过的研判结果
    （不合格的情景已经被丢弃，剩下的价位都能直接信）。change_pct/holding/triggers 都可选——
    没传就少几行，不是硬性依赖，方便单独测试或降级场景。"""
    head = '▌%s %s  %.2f' % (name, entry['symbol'], last)
    if change_pct is not None:
        head += '（%+.2f%%）' % change_pct
    lines = [head]
    hl = _holding_line(holding)
    if hl:
        lines.append(hl)
    tl = _trigger_line(triggers)
    if tl:
        lines.append(tl)
    if entry.get('current_read'):
        lines.append('现状：' + entry['current_read'][:80])
    for sc in entry.get('scenarios') or []:
        lines.append(_scenario_line(sc))
    lines.append('倾向：%s｜依据强度 %d/5（自评，不是胜率）' % (_action_brief(entry.get('action_hint')), entry['confidence']))
    if entry.get('watch_metrics'):
        lines.append('盯盘：' + '；'.join(entry['watch_metrics'][:2]))
    if entry.get('caveats'):
        lines.append('局限：' + '；'.join(entry['caveats'][:2]))
    return '\n'.join(lines)


def format_scenario_message(blocks, issued_hhmm):
    """把多只股票的情景块拼成一条推送。抬头带时间戳和只数（多条堆在企业微信里此前分不清
    先后）。2026-09-24 起去掉了末尾的免责声明/存档提示——用户看多了觉得是噪音；情景照常
    存档、照常在 15:20 对账，只是不再每条消息都念一遍。

    blocks: [(entry, name, last, change_pct, holding, triggers), ...]，顺序即推送顺序。"""
    texts = [format_scenarios(*b) for b in blocks]
    head = '【情景研判 %s】共 %d 只' % (issued_hhmm, len(blocks))
    return '\n\n'.join([head] + texts)


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
    if day.get('support') and day.get('resistance'):
        ctx.append('支撑/阻力 %.2f / %.2f' % (day['support'], day['resistance']))
    if day.get('macd_state'):
        labels = {
            'bullish_above_zero': 'MACD 多头且在零轴上',
            'bullish_below_zero': 'MACD 多头但仍在零轴下',
            'bearish_above_zero': 'MACD 转弱但仍在零轴上',
            'bearish_below_zero': 'MACD 空头且在零轴下',
        }
        ctx.append(labels.get(day['macd_state'], 'MACD %s' % day['macd_state']))
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
        # combined_pnl 只在 holding_facts 传了 trades 时才有（book_pnl.combined）；旧调用没传就没有，
        # 这一行照旧只显示浮动盈亏，不强行要求所有调用方都先取一遍成交流水。
        if h.get('realized_trades'):
            own.append('已实现 %+.0f（%d笔）' % (h['realized_pnl_net'], h['realized_trades']))
        if h.get('combined_pct') is not None:
            own.append('综合 %+.2f%%' % h['combined_pct'])
        if h.get('effective_cost') is not None and h['effective_cost'] != h['cost_price']:
            own.append('等效成本 %.2f' % h['effective_cost'])
        if h.get('stop_price'):
            own.append('系统止损位 %.2f' % h['stop_price'])
        if h.get('target_price'):
            own.append('系统止盈位 %.2f' % h['target_price'])
        lines.append('｜'.join(own))
    return '\n'.join(lines)


# --- 哨兵（注册进盘中引擎的评估器 + 事后处理）----------------------------------

class Sentinel:
    def __init__(self, history, book_fn=None, series_fn=None, send_fn=None, minute_fn=None, directory=None,
                 flow_fn=None, flow_dir=None, market_pause_fn=None, sector_fn=None, trades_fn=None,
                 book_state_dir=None):
        self.history = Path(history)
        self.directory = directory
        # 默认账本：持仓行带上"今天可卖数量"（按成交流水的 T+1 算），做T底仓与之挂钩。
        self.book_fn = book_fn or (lambda: (portfolio_book.with_sellable(portfolio_book.load('holdings')),
                                            portfolio_book.load('watchlist')))
        # 告警里的「综合盈亏/等效成本」要用到成交流水；单独取一份，不强行绑进 book_fn 的返回形状。
        self.trades_fn = trades_fn or (lambda: portfolio_book.load('trades'))
        self.market_pause_fn = market_pause_fn or (lambda: book_verdict.market_pause(self.history))
        self.sector_fn = sector_fn            # (symbol, day) -> {'industry','change','n'} | None；不传就用真实行情
        self.series_fn = series_fn or (lambda s: live_check.load_series(self.history, s)[0])
        self.send_fn = send_fn or wecom_send
        self.minute_fn = minute_fn or minute_data.fetch_minute
        self.flow_fn = flow_fn or money_flow.fetch_flow
        self.flow_dir = flow_dir
        self.book_state_dir = book_state_dir  # 移动止损用的历史最高价存这里；不传就用 book_state 自己的默认目录
        self.cache, self.market = {}, {}

    def symbols(self):
        holdings, watch = self.book_fn()
        return sorted({r['symbol'] for r in holdings} | {r['symbol'] for r in watch}) + INDEX_SYMBOLS

    def evaluate(self, tick):
        holdings, watch = self.book_fn()
        hold, wl = {h['symbol']: h for h in holdings}, {w['symbol']: w for w in watch}
        quotes = tick['quotes']
        self.market = {INDEX_LABEL[s]: float(quotes[s]['change_pct']) for s in INDEX_SYMBOLS if s in quotes}
        market_pause = self.market_pause_fn() if wl else None
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
            day, minute, t = None, None, None
            if h:
                # 止损/止盈位与做T底仓由系统算（成本价来自买入记录），不再读用户声明。移动止损要用到
                # 自建仓以来的历史最高价：先落盘（首次调用会从本地日线把开仓以来的高点补齐），再传给
                # enrich() 算三条止损里最紧的一条。
                peak = book_state.touch_peak(symbol, float(q['last']), tick['now'], bars, h.get('opened_on'),
                                             directory=self.book_state_dir)
                h = book_levels.enrich({**h, 'name': name}, bars, q.get('quote_date'), peak_price=peak)
                signals += sr.holding_signals(h, q, facts, limits)
                try:
                    minute = tick['minutes'](symbol)
                except minute_data.MinuteError as exc:
                    issues.append('分时数据不可用，盘中支撑阻力/做T提示不含均价线：%s' % exc)
                day = sr.day_facts(q, minute)
                signals += sr.intraday_reversal_signals(h, q, day)
                if h['t_base_shares']:
                    t = sr.t_evaluate(h, q, day, self._env_fn(symbol, q, tick), limits)
                    if t['unavailable']:
                        issues.append(t['unavailable'])
                    signals += sr.t_signals(h, q, day, evaluation=t)
            if w:                   # 既持有又在自选：持仓信号已覆盖卖出侧，买入信号照常（可能是加仓机会）
                own = sr.watch_signals({**w, 'name': name}, q, facts, limits, market_pause)
                signals += [x for x in own if not h or x['key'].startswith('buy-signal')]
            signals += sr.node_signals(symbol, name, tick['node_due'])
            self.cache[symbol] = {'quote': q, 'facts': facts, 'limits': limits, 'holding': h, 'watch': w,
                                  'name': name, 'day': day, 'minute': minute, 'issues': issues, 't': t}
        return signals

    def _env_fn(self, symbol, quote, tick):
        """做T 的环境（大盘/板块/资金流）。返回一个懒取函数：只有价格位置满足时才会真的联网。"""
        now, directory = tick['now'], self.flow_dir or ie.data_dir()
        sector = self.sector_fn or (lambda sym, day: t_context.sector_change(self.history, sym, day, now, cache_dir=directory))
        flow = lambda sym, day: t_context.flow_facts(directory, sym, day, now)
        return lambda: t_context.build_env(symbol, tick['quotes'], quote.get('quote_date'), sector, flow)

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
        trades = self.trades_fn()
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
            holding = live_check.holding_facts(c['holding'], c['quote'], trades) if c['holding'] else None
            levels = scenario_analyst.key_levels(c['quote'], c['facts'], day or {}, c['holding'], c['limits'])
            # 固定时间节点（09:45/13:05/14:30）本身不是"可操作事件"——单独推一条"开盘方向确立"
            # 没有任何信息，5 只股票 × 3 个节点每天就是 15 条噪音。它的价值是触发情景研判：
            # 节点事件只入队，等研判进程给出带价位的情景后才推那一条有内容的消息。
            # 和真正的事件同时触发时，才附在告警里作为背景。
            # watch 档同理不实时推（below_cost/量比突增/分时穿越这类"观察级"信号）：仍写进
            # audit trail（stored，标 pushed=false）+ 页面抽屉，只是不刷屏；但**不单独消耗 AI
            # 研判额度**——一个 below_cost 犯不上花一次情景研判，除非同一轮还有别的东西触发它。
            has_node = any(e['kind'].startswith('sentinel.node_') for e in evs)
            non_node = [e for e in evs if not e['kind'].startswith('sentinel.node_')]
            actionable = [e for e in non_node if e['severity'] != 'watch']
            if actionable or has_node:
                pending_ids.append(self._enqueue(now, symbol, c, evs, day, holding, levels, minute))
            watch_only = not actionable and bool(non_node)
            if not actionable and not watch_only:
                continue                     # 只有节点事件：不建 block，只在上面按需入队触发情景研判
            block = {'symbol': symbol, 'name': c['name'], 'events': evs, 'quote': c['quote'], 'holding': holding,
                     'day': day, 'urgent': any(e['severity'] == 'urgent' for e in evs), 'watch_only': watch_only,
                     'flow': None, 'book': money_flow.book_facts(c['quote'])}
            if actionable:
                # 资金流是事实展示，不是告警的前提：取不到就没有这一行，告警照发。只在真要推送时才取。
                block['flow'] = self._flow(symbol, now, started)
            blocks.append(block)
        if not blocks:
            return {'alerts': 0, 'pending': pending_ids}
        blocks.sort(key=lambda b: (b['watch_only'], not b['urgent']))
        push_blocks = [b for b in blocks if not b['watch_only']]
        texts = {id(b): format_alert(b) for b in blocks}
        if push_blocks:
            # 2026-09-24 起不再附免责声明：用户反馈每条都念一遍是噪音。
            push_text = '\n\n'.join(texts[id(b)] for b in push_blocks)
            result = self.send_fn(push_text)
        else:
            result = {'sent': False, 'reason': '本轮只有观察级信号，不实时推送'}
        # blocks：按股票拆开的一份（界面按股票查告警用）。watch_only 的块 text 仍然生成（供页面抽屉
        # 展示"发生了什么"），但不计入 push_text，也不单独调用 send_fn。
        stored = [{'symbol': b['symbol'], 'name': b['name'], 'urgent': b['urgent'], 'text': texts[id(b)],
                   'pushed': not b['watch_only'], 'price': b['quote'].get('last'), 'change_pct': b['quote'].get('change_pct'),
                   'events': [{'kind': e['kind'], 'severity': e['severity'], 'detail': e['detail']} for e in b['events']],
                   'flow': money_flow.compact(b['flow']), 'book': b['book']} for b in blocks]
        _append(self.directory, 'alerts-%s.jsonl' % now.strftime('%Y-%m-%d'),
                {'at': now.isoformat(), 'symbols': [b['symbol'] for b in blocks],
                 'text': push_text if push_blocks else '', 'push': result, 'blocks': stored,
                 'events': [{'key': e['key'], 'kind': e['kind'], 'evidence': e.get('evidence')} for e in events]})
        return {'alerts': len(push_blocks), 'push': result, 'pending': pending_ids}

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
                 'watch': {'note': c['watch'].get('note') or None} if c['watch'] else None,
                 'key_levels': levels, 'recent_minutes': recent, 'issues': c['issues'],
                 't_context': {k: c['t'][k] for k in ('side', 'ok', 'support', 'veto', 'env')} if c.get('t') and c['t']['side'] else None}
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
        ai_ok_fn = lambda: claude_client.available(os.environ.get('SENTINEL_MODEL') or None)
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

    dropped, blocks, to_record = [], [], []
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
        entry = item['entry']
        blocks.append((e, entry['name'], last, _change_pct(entry.get('quote'), last),
                       entry.get('holding'), entry.get('triggers')))
        to_record.append((sym, item, last, e))
    # 情景的"发布时间"= 用户真正收到它的时刻 = 分析开始时间 + AI 实际耗时。用 now 参数而不是
    # datetime.now()：既真实，也让回放/测试里情景和它所属的交易日时间轴保持一致。
    issued = (now + timedelta(seconds=time.monotonic() - started_at)).isoformat()
    # 一次研判只推**一条**合并消息，而不是每只股票一条：同一轮研判常常涵盖好几只（比如 09:45 节点
    # 五只一起），逐只推一天就是几十条，违背"只推可操作事件"。
    push = send_fn(format_scenario_message(blocks, issued[11:16])) if blocks else {'sent': False, 'reason': '没有合格的情景'}
    pushed = len(blocks) if push.get('sent') else 0
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
    p.add_argument('--history', type=Path, default=Path(os.environ.get('HISTORY_DIR', '.history')))
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
        print('%s 情景对账 %d 条，有判定 %d 条' % (a.day, len(r['results']), len(judged)))
        # 规则层告警的机制核对 + 结果标签：和情景对账同一个 15:20 窗口，不新增定时任务。
        ar = alert_ledger.reconcile(None, None, a.history, a.day)
        print('%s 告警机制核对 %d 条（不一致 %d）；结果标签 %d 条（有结论 %d）' % (
            a.day, ar['audit']['checked'], len(ar['audit']['mismatches']), len(ar['outcomes']),
            sum(1 for x in ar['outcomes'] if x['outcome'])))
        if not a.no_push:
            print(wecom_send(alert_ledger.render_daily(a.day, ar['audit'], ar['outcomes'])))
        if datetime.strptime(a.day, '%Y-%m-%d').weekday() == 4 and not a.no_push:     # 周五顺带推两份周报
            print(wecom_send(scenario_ledger.render_weekly(scenario_ledger.weekly(None, a.day))))
            print(wecom_send(alert_ledger.render_weekly(alert_ledger.weekly(None, None, a.history, a.day))))
        return 0
    print(scenario_ledger.render_weekly(scenario_ledger.weekly(None, a.day)))
    print(alert_ledger.render_weekly(alert_ledger.weekly(None, None, a.history, a.day)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
