"""规则化补仓提示：亏损仓在满足一整套硬否决之后，才可能给出"考虑补仓"的建议，而且股数由
"补仓后总风险不超预算"反推，不是"想把成本摊到多少"——那是马丁格尔的思路，越跌越买，亏得
越多加得越猛，一旦判断错了，亏得也越多。这里反过来：亏得越深，止损线离现价越远（止损幅度
本来就随 ATR 走），同样的风险预算下能补的股数反而越少。纯函数，没有 I/O——账户资金、补仓
历史、大盘/板块/资金流、分诊结果都由调用方（sentinel.py）算好传进来。

**硬否决**（任一命中就不给提示，不摊低成本这件事本身有讨论空间，代码不替你做这个判断，只负责
在明显不该加的时候不递话筒）：
1. 本行结论已经是"彻底卖出"或"可暂时卖出"——有卖出信号在场，不谈补仓。
2. 分诊为"趋势已坏"，或分诊不出结果（日线不足）。
3. 策略的大盘趋势评分低于暂停线（和买入信号共用同一道闸）。
4. 大盘系统性杀跌，或板块整体杀跌（复用做T 环境的阈值 T_MARKET_CRASH/T_SECTOR_DUMP）。
5. 已涨停，或距涨停很近——追高不是补仓。
6. 补仓次数已达上限（默认 2 次）。
7. 距上次买入不足最短间隔（默认 3 个交易日）——不能靠"无限次小额补仓"绕开风险预算的意图落空。
8. 现价距上次买入价的跌幅不够一个 ATR 倍数（默认 1.0）——离得太近算不上"更便宜的价格"。
9. 账户没填写权益/现金——给不出股数建议，明说，不猜一个默认值。
10. 资金流数据缺失——保守起见不给提示（和 t_evaluate 的"缺数据就说缺数据"是同一个纪律）。

**支持条件**（必须全部满足）：分诊结果是"仍是回调"或"区间震荡"；命中回调反弹形态的价格门槛
（sentinel_rules.buy_setup 的 pullback track）；主力资金近 30 分钟没有明显净流出。

**仓位算法**：设补仓 n 手（每手 = 最小交易单位）、价格 P、原持仓 N 股成本 C：
    N' = N + n；C' = (C×N + P×n) / N'；S' = max(C'×(1−止损幅度), 移动止损位)
    约束：(C' − S') × N' ≤ 账户权益 × 单笔风险预算(2%)
         C' × N' ≤ 账户权益 × 单只上限(30%)
         n × P ≤ 可用现金
n 是满足全部约束的最大整手数——风险预算和单只上限都随 n 单调变紧，用二分足够，不需要解析解。
"""
import math

import book_triage
import portfolio_book
import sentinel_rules as sr

ADD_MAX_TIMES = 2
ADD_MIN_GAP_DAYS = 3
ADD_MIN_DROP_ATR = 1.0
RISK_PER_TRADE_PCT = 2.0        # 单笔风险预算：占账户权益的百分比
MAX_WEIGHT_PCT = 30.0           # 单只上限：占账户权益的百分比
SUPPORTED_BUCKETS = ('pullback', 'range')


def _veto(h, holding_action, triage, market_pause, market, sector, limits, add_state, days_since_last_add,
         last_buy_price, atr_pct, last, account, flow):
    if holding_action in ('exit', 'reduce'):
        return '本行结论是%s，不给补仓提示' % ('彻底卖出' if holding_action == 'exit' else '可暂时卖出')
    bucket = triage.get('bucket')
    if bucket is None:
        return '日线不足，分诊不出结果，不给补仓提示'
    if bucket == 'broken':
        return '分诊为"趋势已坏"，不建议补仓'
    if market_pause:
        return '大盘趋势评分低于暂停线，策略暂停新增仓位'
    if market is not None and market <= sr.T_MARKET_CRASH:
        return '大盘大跌（%+.2f%%），系统性下跌，不是补仓的时候' % market
    if sector is not None and sector <= sr.T_SECTOR_DUMP:
        return '板块整体杀跌（中位 %+.2f%%），不是个股的日内波动' % sector
    if limits.get('at_limit_up') or (limits.get('to_limit_up_pct') is not None and limits['to_limit_up_pct'] <= sr.NEAR_LIMIT_PCT):
        return '已涨停或距涨停很近，追高不是补仓'
    if (add_state.get('add_count') or 0) >= ADD_MAX_TIMES:
        return '补仓次数已达上限（%d 次）' % ADD_MAX_TIMES
    if days_since_last_add is not None and days_since_last_add < ADD_MIN_GAP_DAYS:
        return '距上次买入不足 %d 个交易日' % ADD_MIN_GAP_DAYS
    if last_buy_price and atr_pct:
        drop_pct = (last_buy_price - last) / last_buy_price
        if drop_pct < ADD_MIN_DROP_ATR * atr_pct:
            return '现价距上次买入价跌幅不够 %.1f 倍 ATR，价格离得太近，算不上"更便宜"' % ADD_MIN_DROP_ATR
    if not account:
        return '未填写账户资金，无法给出补仓股数建议'
    if flow is None:
        return '资金流数据取不到，保守起见不给补仓提示'
    return None


def _support(triage, buy_setup_ok, flow):
    bucket = triage.get('bucket')
    if bucket not in SUPPORTED_BUCKETS:
        return False, '分诊为"%s"，不是回调也不是区间震荡' % book_triage.BUCKET_LABEL.get(bucket, bucket)
    if not buy_setup_ok:
        return False, '现价没有命中回调反弹形态的价格门槛'
    if flow is not None and flow.get('main_30m', 0) < 0 and abs(flow['main_30m']) > 0:
        # 只否决"明显净流出"；flow 存在但接近 0 不当作否决，避免把噪声当成流出。
        if flow['main_30m'] < -1e6:
            return False, '主力近 30 分钟明显净流出'
    return True, None


def _max_add_shares(cost, shares, price, stop_pct, trail_stop, equity_base, cash, lot):
    """满足风险预算/单只上限/现金约束的最大补仓股数（lot 的整数倍）。约束都随 n 单调变紧，
    直接从最大可能股数往下试，第一个满足的就是答案——不用真二分，股数空间本来就小。"""
    if price <= 0 or equity_base <= 0:
        return 0
    max_by_cash = int(cash // price)
    max_n = min(max_by_cash, int(equity_base * (MAX_WEIGHT_PCT / 100) / price))
    max_n = max_n // lot * lot
    n = max_n
    while n > 0:
        total = shares + n
        new_cost = (cost * shares + price * n) / total
        new_stop = max(new_cost * (1 - stop_pct), trail_stop or 0)
        risk = (new_cost - new_stop) * total
        weight = new_cost * total
        if risk <= equity_base * (RISK_PER_TRADE_PCT / 100) + 1e-6 and weight <= equity_base * (MAX_WEIGHT_PCT / 100) + 1e-6:
            return n
        n -= lot
    return 0


def evaluate(h, q, facts, limits, triage, holding_action, market_pause=None, market=None, sector=None,
            flow=None, add_state=None, account=None, last_buy_price=None, days_since_last_add=None,
            buy_setup_ok=None):
    """返回 {'ok': bool, 'veto': str|None, 'support': str|None, 'add_shares': int|None,
    'preview': {...}|None}。ok=False 时其余字段仅供参考，不构成提示。

    h：book_levels.enrich 之后的持仓行（cost_price/shares/stop_price/levels）。
    triage：book_triage.triage(...) 的结果。add_state：book_state.get(symbol) 的结果（默认 {}）。
    account：portfolio_book.load_account() 的结果（None = 未填写）。
    last_buy_price：上一次买入价（没有专门记录时用成本价近似，未经验证的口径）。
    buy_setup_ok：sentinel_rules.buy_setup(q, facts) 里 track=='pullback' 且 passed 的判定，
    调用方算好传进来（复用买入信号同一套价格门槛，不重新实现一遍）。"""
    add_state = add_state or {}
    last = float(q['last'])
    atr_pct = h.get('levels', {}).get('atr_pct') or 0.035
    stop_pct = h.get('levels', {}).get('stop_pct', 0.08)

    reason = _veto(h, holding_action, triage, market_pause, market, sector, limits, add_state,
                   days_since_last_add, last_buy_price, atr_pct, last, account, flow)
    if reason:
        return {'ok': False, 'veto': reason, 'support': None, 'add_shares': None, 'preview': None}

    ok, why = _support(triage, bool(buy_setup_ok), flow)
    if not ok:
        return {'ok': False, 'veto': None, 'support': why, 'add_shares': None, 'preview': None}

    lot = portfolio_book.lot_size(h['symbol'])
    trail_stop = h['stop_price'] if h.get('stop_source') == 'trail' else None
    n = _max_add_shares(float(h['cost_price']), int(h['shares']), last, stop_pct, trail_stop,
                        account['equity_base'], account['cash'], lot)
    if n <= 0:
        return {'ok': False, 'veto': '按当前风险预算算不出至少一手的补仓股数（可能是现金不足，或单只已接近上限）',
                'support': None, 'add_shares': None, 'preview': None}

    old_cost, old_shares = float(h['cost_price']), int(h['shares'])
    new_shares = old_shares + n
    new_cost = round((old_cost * old_shares + last * n) / new_shares, 4)
    new_stop = round(max(new_cost * (1 - stop_pct), trail_stop or 0), 2)
    old_risk = round((old_cost - h['stop_price']) * old_shares, 2)
    new_risk = round((new_cost - new_stop) * new_shares, 2)
    return {'ok': True, 'veto': None, 'support': None, 'add_shares': n,
            'preview': {'old_cost': old_cost, 'new_cost': new_cost, 'old_stop': h['stop_price'], 'new_stop': new_stop,
                       'old_max_loss': old_risk, 'new_max_loss': new_risk}}


def preview_text(preview):
    """补仓预览的三句话——敞口变大这件事不能藏，必须说清楚。"""
    return [
        '补仓后加权成本 %.2f → %.2f，止损位同步下移到 %.2f' % (preview['old_cost'], preview['new_cost'], preview['new_stop']),
        '这笔的最大亏损从 %.0f 元变成 %.0f 元' % (preview['old_max_loss'], preview['new_max_loss']),
        '摊低的是成本线，不是风险；判断错了亏得更多',
    ]
