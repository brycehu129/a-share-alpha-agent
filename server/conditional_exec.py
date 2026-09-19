"""exec-0.2 条件入场 + 盘中止损 + 保本止损，跑在 intraday_engine 上。

一个**独立的模拟账户**（10 万虚拟本金），和 agent/ 里 exec-0.1 那个账户互不相干：
旧账户只承载 0.3 版留下的 09:30–09:35 窗口计划，新计划一律走这里。两个账户各管各的虚拟
本金，不存在重复占用真钱；也不会互相污染各自的样本统计。

账本存 server/data/private/intraday/ledger-exec-0.2.json（引擎同目录，单写者，原子写）。
不放进 git 是因为盘中每分钟都可能变，而 .history 由几个持有流水线锁的任务顺序写；
让一个不取锁的每分钟进程去写 git 会和它们互相踩。代价是账本没有异地备份，且暂时
不会出现在 dashboard 里——每日报告并入是后续步骤。

四条纪律（和 intraday_engine / opening_observer 一脉相承）：

1. **只在实际观测到的报价上成交或触发。** 引擎只把"新鲜且属于今天"的报价交进来；两次
   轮询之间价格曾经碰过某个位置，没观测到就是没发生，绝不事后补记。
2. **成交价是观测到的价格（再叠滑点），不是触发位。** 跳空低开穿过止损位时，真实成交
   只会更差，不能假装在止损位成交。
3. **T+1。** 入场当天不能卖出，所以"盘中止损/止盈"从入场次日才生效；入场当天只记录
   浮盈是否已达保本武装线。
4. **保守跳过。** 接近涨跌停、复权关系对不上、MA20 无法核验、持仓已满、账户回撤暂停——
   都不成交，记录原因，不猜。
"""
import argparse
import json
import math
import os
from datetime import datetime, time as clock_time
from pathlib import Path

import live_check
from alpha_portfolio import fee
from collect_quotes import CST
from exec_spec import EXEC_MODE, breakeven_win_rate, entry_zone
from intraday_engine import _atomic_write, data_dir
from shortterm_model import EXECUTION_VERSION, SHORT_POLICY
from tushare_sync import read

LEDGER_NAME = 'ledger-exec-0.2.json'
NEAR_LIMIT_PCT = 0.5          # 买入时距涨停不足 0.5% 就不算能成交
PLAN_FREEZE_CUTOFF = clock_time(9, 20)


def parse_hhmm(text):
    return clock_time(int(text[:2]), int(text[3:]))


# --- 账本 -------------------------------------------------------------------

def ledger_path(directory=None):
    return (directory or data_dir()) / LEDGER_NAME


def new_ledger(now):
    cap = SHORT_POLICY['capital']
    return {'ledger_version': EXECUTION_VERSION, 'created_at': now.isoformat(), 'capital': cap,
            'cash': float(cap), 'equity': float(cap), 'peak': float(cap), 'paused': False,
            'positions': [], 'trades': [], 'plans': {}, 'plans_loaded_for': None, 'issues': []}


def load_ledger(directory, now):
    path = ledger_path(directory)
    if not path.exists():
        return new_ledger(now)
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except ValueError:
        # 账本损坏不能悄悄重置成一个空的 10 万——那等于抹掉了所有持仓和成交。
        os.replace(path, path.with_suffix('.broken'))
        ledger = new_ledger(now)
        ledger['issues'].append('账本文件损坏已隔离为 .broken，从空账本重新开始：%s' % now.isoformat())
        return ledger


def save_ledger(directory, ledger):
    _atomic_write(ledger_path(directory), json.dumps(ledger, ensure_ascii=False, indent=1))


# --- 交易日历与计划 -----------------------------------------------------------

def default_dates(history):
    """两个交易所日历一致的开市日期列表；缺失/不一致返回 None（宁可不执行，也不猜节假日）。"""
    path = history / 'tushare_data/trade_cal.json'
    if not path.exists():
        return None
    calendars = read(path)['calendars']
    sets = []
    for exchange in ('SSE', 'SZSE'):
        sets.append(sorted(datetime.strptime(r['cal_date'], '%Y%m%d').date().isoformat()
                           for r in calendars[exchange] if str(r['is_open']) == '1'))
    return sets[0] if sets[0] == sets[1] else None


def default_plans(history, previous):
    """只读截止日 == 上一交易日的计划文件。文件名 select-<版本>-<截止日>-<代码>.json 里带着
    截止日，先按文件名过滤，不必把几个月累积的全部预测每天读一遍。"""
    folder = history / 'predictions'
    if not folder.is_dir():
        return []
    return [read(p) for p in sorted(folder.glob('select-*-%s-*.json' % previous))]


def first_session(eligible_from, dates):
    return next((d for d in dates if d >= eligible_from), None)


def sessions_completed(dates, entry_day, today):
    """从入场日起到今天之前（含入场日）已经完整走完的交易日数。"""
    return sum(1 for d in dates if entry_day <= d < today)


def admit(f, today, previous, dates):
    """一条冻结计划能不能成为今天的活跃计划。返回 (plan 字段, 不通过的原因)。"""
    if f.get('execution_mode') != EXEC_MODE or not f.get('paper_eligible'):
        return None, None                      # 不是给本执行器的，或只做研究留档：与本账户无关，不记录
    plan = {'id': f['id'], 'symbol': f['symbol'], 'name': f.get('name'), 'track': f.get('strategy_type'),
            'reference_price': f.get('reference_price'), 'as_of': f.get('as_of'),
            'eligible_from': f.get('eligible_from'), 'rank': f.get('rank'),
            'spec': f.get('exec_spec'), 'day': today, 'status': 'watching', 'reason': None}
    created = datetime.fromisoformat(f['created_at']).astimezone(CST)
    if not plan['spec'] or not plan['reference_price']:
        return plan, '计划缺少 exec_spec 或参考价'
    if f.get('as_of') != previous:
        return plan, '计划依据不是上一交易日，不执行过期计划'
    if first_session(f['eligible_from'], dates) != today:
        return plan, '今天不是该计划的首个允许交易日'
    if created.date().isoformat() >= today and created.time() >= PLAN_FREEZE_CUTOFF:
        return plan, '计划未在当日 09:20 之前冻结'
    return plan, None


# --- 成本与保本价 -------------------------------------------------------------

def sell_net(price, shares, entry_cost, policy):
    """以观测价 price 卖出后的净盈亏（含卖出滑点、佣金、过户费、印花税，扣掉建仓总成本）。"""
    gross = round(price * (1 - policy['slippage']) * shares, 2)
    return gross - fee(gross, 'sell', policy) - entry_cost


def breakeven_price(entry_price, shares, entry_cost, policy):
    """让这笔交易净盈亏刚好 ≥ 0 的最低观测价，向上取整到分。

    不用计划里写的 entry×1.0031 近似：那个数假设双边成本都要从入场价之上补，但入场价
    本身已经含了 0.1% 滑点，会高估约 0.1 个百分点。这里直接对真实费用函数二分求解，
    "保本止损"才真的保本。"""
    lo, hi = entry_price, entry_price * 1.05
    for _ in range(50):
        mid = (lo + hi) / 2
        if sell_net(mid, shares, entry_cost, policy) >= 0:
            hi = mid
        else:
            lo = mid
    return math.ceil(hi * 100 - 1e-9) / 100


def size_shares(ledger, price, stop_pct, symbol, policy):
    budget = min(ledger['equity'] * policy['max_weight'],
                 ledger['equity'] * policy['risk_per_trade'] / stop_pct,
                 ledger['cash'] - 10)
    shares = max(0, int(budget / price / 100) * 100)
    min_lot = 200 if symbol.startswith('sh688') else 100
    while shares and shares * price + fee(shares * price, 'buy', policy) > ledger['cash']:
        shares -= 100
    return shares if shares >= min_lot else 0


# --- 执行器 -----------------------------------------------------------------

class Executor:
    def __init__(self, history, directory=None, plans_fn=None, dates_fn=None, series_fn=None,
                 policy=None):
        self.history = Path(history)
        self.directory = directory or data_dir()
        self.plans_fn = plans_fn or (lambda previous: default_plans(self.history, previous))
        self.dates_fn = dates_fn or (lambda: default_dates(self.history))
        self.series_fn = series_fn or (lambda symbol: live_check.load_series(self.history, symbol)[0])
        self.policy = policy or SHORT_POLICY
        self.ledger = None
        self.dates = None

    # -- 准备：加载账本、滚动过期、加载今天的计划 ---------------------------------
    def prepare(self, now, dry_run=False):
        today = now.date().isoformat()
        self.ledger = ledger = load_ledger(self.directory, now)
        before = json.dumps(ledger, sort_keys=True)
        for plan in ledger['plans'].values():
            if plan['status'] == 'watching' and plan['day'] < today:
                # 前一天没观测到窗口结束（引擎当时没在跑）：留在 watching 会让它带着过期的
                # 参考价活到今天，所以显式过期，并写明原因。
                plan.update(status='expired', reason='当日窗口内未触发，且未观测到窗口结束')
        try:
            self.dates = self.dates_fn()
        except Exception as exc:
            self.dates = None
            self._issue(ledger, '交易日历不可用(%s)，本轮不加载计划、不做按天数的退出' % type(exc).__name__)
        if self.dates and today in self.dates and ledger['plans_loaded_for'] != today:
            previous = max((d for d in self.dates if d < today), default=None)
            if previous:
                for f in self.plans_fn(previous):
                    plan, why = admit(f, today, previous, self.dates)
                    if plan is None or plan['id'] in ledger['plans']:
                        continue
                    if why:
                        plan.update(status='skipped', reason=why)
                    ledger['plans'][plan['id']] = plan
                ledger['plans_loaded_for'] = today
        if not dry_run and json.dumps(ledger, sort_keys=True) != before:
            save_ledger(self.directory, ledger)
        return ledger

    @staticmethod
    def _issue(ledger, text):
        if text not in ledger['issues'][-5:]:
            ledger['issues'] = (ledger['issues'] + [text])[-50:]

    def watch_symbols(self, now, dry_run=False):
        """需要盯的股票：今天还在观察的计划 + 所有持仓。"""
        ledger = self.prepare(now, dry_run)
        today = now.date().isoformat()
        symbols = {p['symbol'] for p in ledger['plans'].values() if p['status'] == 'watching' and p['day'] == today}
        symbols.update(p['symbol'] for p in ledger['positions'])
        return sorted(symbols)

    # -- 一轮 -----------------------------------------------------------------
    def step(self, tick):
        now, today = tick['now'], tick['now'].date().isoformat()
        if self.ledger is None:
            self.prepare(now, tick.get('dry_run', False))
        ledger, policy = self.ledger, self.policy
        before = json.dumps(ledger, sort_keys=True)
        quotes, signals = tick['quotes'], []
        facts_cache = {}

        def facts_for(symbol):
            """用实时价重算均线并核对复权：live_check.price_facts 会在实时昨收与缓存上一
            交易日收盘对不上时给出 issues，这里据此拒绝成交。"""
            if symbol not in facts_cache:
                bars = self.series_fn(symbol)
                facts_cache[symbol] = live_check.price_facts(bars, quotes[symbol]) if bars \
                    else ({}, ['本地没有日线缓存，无法核验复权与MA20'])
            return facts_cache[symbol]

        # 先把所有持仓按最新观测价标价并重算净值/回撤，再逐个判断退出。顺序反了的话，触发
        # 回撤暂停的那一轮，"回撤退出"信号要拖到下一轮才生效——多一分钟风险暴露。
        for pos in ledger['positions']:
            q = quotes.get(pos['symbol'])
            if q:
                pos['mark'], pos['mark_at'] = float(q['last']), q.get('quote_at')
        self._mark_equity(ledger, policy)
        for pos in list(ledger['positions']):
            q = quotes.get(pos['symbol'])
            if q:
                signals += self._manage_position(ledger, pos, q, tick, today, policy)
        for plan in ledger['plans'].values():
            if plan['status'] == 'watching' and plan['day'] == today:
                signals += self._manage_plan(ledger, plan, quotes, tick, facts_for, policy)
        if not tick.get('dry_run') and json.dumps(ledger, sort_keys=True) != before:
            save_ledger(self.directory, ledger)
        return signals

    # -- 计划：入场 -------------------------------------------------------------
    def _manage_plan(self, ledger, plan, quotes, tick, facts_for, policy):
        now = tick['now']
        entry = plan['spec']['entry']
        start, end = (parse_hhmm(t) for t in entry['window'])
        if now.time() < start:
            return []
        if now.time() >= end:
            plan.update(status='expired', reason='有效时段结束仍未触发', status_at=now.isoformat())
            return [self._note('plan', plan, '计划过期：%s 至 %s 内未触发' % tuple(entry['window']))]
        q = quotes.get(plan['symbol'])
        if not q:
            return []
        last = float(q['last'])
        lo, hi, void = entry_zone(entry, plan['reference_price'])
        facts, issues = facts_for(plan['symbol'])
        signals = [self._level('entry-watch', plan['id'], plan['symbol'],
                               (lo, 'up') if lo is not None else (hi, 'down'))]
        if last <= void:
            plan.update(status='voided', reason='跌破作废线 %.2f（现价 %.2f）' % (void, last), status_at=now.isoformat())
            return signals + [self._note('plan', plan, plan['reason'])]
        ma20 = facts.get('ma20')
        if entry['void_below_ma20']:
            if ma20 is None:
                plan['last_note'] = 'MA20 无法核验，不成交'
                return signals
            if last < ma20:
                plan.update(status='voided', reason='跌破 MA20 %.2f（现价 %.2f）' % (ma20, last), status_at=now.isoformat())
                return signals + [self._note('plan', plan, plan['reason'])]
        if last > hi:
            plan['last_note'] = '现价 %.2f 高于追高上限 %.2f，不追' % (last, hi)
            return signals
        if lo is not None and last < lo:
            plan['last_note'] = '现价 %.2f 尚未向上确认（需 ≥ %.2f）' % (last, lo)
            return signals
        # 走到这里条件成立；下面是"能不能成交"的保守检查。
        drift = facts.get('adjustment_drift_pct')
        if drift is not None and drift > live_check.ADJUST_TOLERANCE_PCT:
            plan.update(status='skipped', reason='复权/除权无法核验（昨收偏差 %.2f%%）' % drift, status_at=now.isoformat())
            return signals + [self._note('plan', plan, plan['reason'])]
        if not facts:
            plan.update(status='skipped', reason='缺日线缓存，无法核验复权', status_at=now.isoformat())
            return signals + [self._note('plan', plan, plan['reason'])]
        limits = live_check.limit_facts(q)
        if limits.get('to_limit_up_pct') is not None and limits['to_limit_up_pct'] < NEAR_LIMIT_PCT:
            plan['last_note'] = '接近涨停，保守不买（本轮重试）'
            return signals
        refusal = self._entry_refusal(ledger, plan, policy)
        if refusal:
            plan.update(status='skipped', reason=refusal, status_at=now.isoformat())
            return signals + [self._note('plan', plan, refusal)]
        return signals + self._open_position(ledger, plan, q, facts, tick, policy)

    def _entry_refusal(self, ledger, plan, policy):
        if ledger['paused']:
            return '账户回撤风控暂停，不开新仓'
        if len(ledger['positions']) >= policy['max_positions']:
            return '持仓已满（%d 只）' % policy['max_positions']
        if any(p['symbol'] == plan['symbol'] for p in ledger['positions']):
            return '已经持有该股票'
        return None

    def _open_position(self, ledger, plan, q, facts, tick, policy):
        now = tick['now']
        exit_spec = plan['spec']['exit']
        price = round(float(q['last']) * (1 + policy['slippage']), 2)
        shares = size_shares(ledger, price, exit_spec['stop_pct'], plan['symbol'], policy)
        if not shares:
            plan.update(status='skipped', reason='资金不足最低模拟申报数量', status_at=now.isoformat())
            return [self._note('plan', plan, plan['reason'])]
        gross = round(price * shares, 2)
        cost_fee = fee(gross, 'buy', policy)
        total = round(gross + cost_fee, 2)
        stop_base = price * (1 - exit_spec['stop_pct'])
        if exit_spec.get('stop_floor') == 'ma20_at_fill' and facts.get('ma20'):
            stop_base = max(stop_base, facts['ma20'])       # 取较高者：MA20 在入场价 3% 以内时更紧
        evidence = {'batch_sha256': q.get('batch_sha256'), 'batch_fetched_at': q.get('batch_fetched_at'),
                    'quote_at': q.get('quote_at'), 'observed_at': now.isoformat(), 'observed_last': q['last']}
        pos = {'id': plan['id'], 'symbol': plan['symbol'], 'name': plan['name'], 'track': plan['track'],
               'shares': shares, 'entry_day': now.date().isoformat(), 'entry_price': price, 'cost': total,
               'stop_base': round(stop_base, 4), 'target_price': round(price * (1 + exit_spec['target_pct']), 4),
               'breakeven_price': breakeven_price(price, shares, total, policy), 'breakeven_armed': False,
               'peak_price': float(q['last']), 'mark': float(q['last']), 'spec': plan['spec'],
               'ma20_at_fill': facts.get('ma20'), 'entry_evidence': evidence}
        ledger['cash'] = round(ledger['cash'] - total, 2)
        ledger['positions'].append(pos)
        ledger['trades'].append({'id': plan['id'] + '-entry', 'prediction_id': plan['id'], 'symbol': plan['symbol'],
                                 'side': 'buy', 'date': pos['entry_day'], 'price': price, 'shares': shares, 'fee': cost_fee,
                                 'reason': '条件触发：现价满足入场区间', 'execution_mode': EXEC_MODE, **evidence})
        plan.update(status='filled', reason='成交 %d 股 @ %.2f' % (shares, price), status_at=now.isoformat())
        self._mark_equity(ledger, policy)
        return [self._note('paper-entry', plan, '模拟买入 %s %d股 @ %.2f，止损 %.2f，目标 %.2f'
                           % (plan['name'] or plan['symbol'], shares, price, pos['stop_base'], pos['target_price']))]

    # -- 持仓：盘中止损 / 保本 / 止盈 / 时间退出 ---------------------------------
    def _stop_level(self, pos):
        return max(pos['stop_base'], pos['breakeven_price']) if pos['breakeven_armed'] else pos['stop_base']

    def _manage_position(self, ledger, pos, q, tick, today, policy):
        last = float(q['last'])
        pos['mark'], pos['mark_at'] = last, q.get('quote_at')
        pos['peak_price'] = max(pos.get('peak_price', last), last)
        spec = pos['spec']['exit']
        signals = []
        if not pos['breakeven_armed'] and last >= pos['entry_price'] * (1 + spec['breakeven_arm_pct']):
            # 入场当天也能武装（只是记录，不卖）：浮盈已经到过保本线，之后回落就该保本走。
            pos['breakeven_armed'] = True
            pos['breakeven_armed_at'] = tick['now'].isoformat()
        signals.append(self._level('stop-watch', pos['id'], pos['symbol'], (self._stop_level(pos), 'down')))
        signals.append(self._level('target-watch', pos['id'], pos['symbol'], (pos['target_price'], 'up')))
        if pos['entry_day'] >= today:
            return signals                                # T+1：入场当天不能卖
        reason = self._exit_reason(ledger, pos, q, today)
        if not reason:
            return signals
        limits = live_check.limit_facts(q)
        if limits.get('at_limit_down'):
            pos['exit_blocked'] = '跌停封死，无法卖出，退出信号保留（%s）' % reason
            return signals
        return signals + self._close_position(ledger, pos, q, reason, tick, policy)

    def _exit_reason(self, ledger, pos, q, today):
        last = float(q['last'])
        level = self._stop_level(pos)
        if last <= level:
            return 'breakeven_stop' if pos['breakeven_armed'] and level > pos['stop_base'] else 'stop'
        if last >= pos['target_price']:
            return 'target'
        if ledger['paused']:
            return 'drawdown_pause'
        spec = pos['spec']['exit']
        if self.dates:
            previous = max((d for d in self.dates if d < today), default=None)
            # 入场日的收盘价就是今天报价里的"昨收"；不需要等到收盘那一轮，也就不怕漏掉它。
            if spec['day1_close_rule'] and previous == pos['entry_day'] and float(q['previous_close']) <= pos['entry_price']:
                return 'time_stop_day1'
            if sessions_completed(self.dates, pos['entry_day'], today) >= spec['hold_sessions']:
                return 'hold_expiry'
        return None

    def _close_position(self, ledger, pos, q, reason, tick, policy):
        now = tick['now']
        price = round(float(q['last']) * (1 - policy['slippage']), 2)      # 观测价再扣滑点，不是止损位
        gross = round(price * pos['shares'], 2)
        cost_fee = fee(gross, 'sell', policy)
        pnl = round(gross - cost_fee - pos['cost'], 2)
        ledger['cash'] = round(ledger['cash'] + gross - cost_fee, 2)
        ledger['positions'].remove(pos)
        evidence = {'batch_sha256': q.get('batch_sha256'), 'batch_fetched_at': q.get('batch_fetched_at'),
                    'quote_at': q.get('quote_at'), 'observed_at': now.isoformat(), 'observed_last': q['last']}
        ledger['trades'].append({'id': pos['id'] + '-exit', 'prediction_id': pos['id'], 'symbol': pos['symbol'],
                                 'side': 'sell', 'date': now.date().isoformat(), 'price': price, 'shares': pos['shares'],
                                 'fee': cost_fee, 'pnl': pnl, 'return_pct': round(pnl / pos['cost'] * 100, 4),
                                 'reason': reason, 'stop_level_at_exit': round(self._stop_level(pos), 4),
                                 'execution_mode': EXEC_MODE, **evidence})
        self._mark_equity(ledger, policy)
        return [self._note('paper-exit', pos, '模拟卖出 %s @ %.2f（%s），盈亏 %.2f' % (
            pos['name'] or pos['symbol'], price, reason, pnl), severity='urgent' if 'stop' in reason else 'normal')]

    def _mark_equity(self, ledger, policy):
        ledger['equity'] = round(ledger['cash'] + sum(p['mark'] * p['shares'] for p in ledger['positions']), 2)
        ledger['peak'] = max(ledger['peak'], ledger['equity'])
        drawdown = 1 - ledger['equity'] / ledger['peak']
        ledger['drawdown_pct'] = round(drawdown * 100, 4)
        if drawdown >= policy['drawdown_pause']:
            ledger['paused'] = True                       # 与旧账户一致：永久暂停开新仓，持仓排队退出

    # -- 信号构造 ---------------------------------------------------------------
    @staticmethod
    def _level(prefix, ident, symbol, level):
        """只用于"差一点"记录的信息型信号：永远不会推送，引擎据 level 记录最近距离。"""
        price, direction = level
        return {'key': '%s:%s' % (prefix, ident), 'symbol': symbol, 'kind': prefix, 'active': False,
                'level': {'price': price, 'direction': direction}}

    @staticmethod
    def _note(prefix, item, text, severity='normal'):
        status = item.get('status') or 'open'
        return {'key': '%s:%s:%s' % (prefix, item['id'], status), 'symbol': item['symbol'],
                'kind': prefix, 'active': True, 'severity': severity, 'detail': text}


def summary(ledger):
    sells = [t for t in ledger['trades'] if t['side'] == 'sell']
    plans = {}
    for p in ledger['plans'].values():
        plans[p['status']] = plans.get(p['status'], 0) + 1
    return {'equity': ledger['equity'], 'cash': ledger['cash'], 'positions': len(ledger['positions']),
            'closed': len(sells), 'wins': sum(t['pnl'] > 0 for t in sells), 'plans': plans,
            'paused': ledger['paused'], 'drawdown_pct': ledger.get('drawdown_pct', 0)}


def attach(history, directory=None, **kwargs):
    """把执行器注册进盘中引擎，返回执行器（调用方用它的 watch_symbols 决定要盯哪些股票）。"""
    import intraday_engine
    executor = Executor(history, directory, **kwargs)
    intraday_engine.register(executor.step)
    return executor


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--status', action='store_true', help='打印账本摘要与各计划状态')
    a = p.parse_args()
    path = ledger_path()
    if not path.exists():
        print('账本尚未创建：%s' % path)
        return 0
    ledger = json.loads(path.read_text(encoding='utf-8'))
    print(json.dumps(summary(ledger), ensure_ascii=False, indent=2))
    for plan in ledger['plans'].values():
        print('  %-9s %-6s %-9s %s' % (plan['symbol'], plan['name'], plan['status'], plan.get('reason') or plan.get('last_note') or ''))
    for pos in ledger['positions']:
        print('  持仓 %s %d股 入%.2f 止损%.2f 目标%.2f 保本武装=%s' % (
            pos['symbol'], pos['shares'], pos['entry_price'], pos['stop_base'], pos['target_price'], pos['breakeven_armed']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
