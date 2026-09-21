"""持仓的系统参考价位：止损位、止盈位、可做T的底仓，都由系统算，不由用户声明。

**为什么不再让用户填。** 系统已经在盘中实时盯着这些股票，"什么价位该走"应该由规则给出，而不是靠用户事先
设一个数字、没设就永远不提醒。规则直接复用策略自己的出场规格（exec_spec.neutral_exit）：

- 止损位 = 成本价 × (1 − 止损幅度)，止损幅度 = ATR14% × 1.5，夹在 3%–8%；
- 止盈位 = 成本价 × (1 + 止盈幅度)，止盈幅度 = 止损幅度 × 盈亏比 1.5，封顶 12%；
- 做T底仓 = 今天可卖的老仓（T+1：当天买入的卖不出去，做T只能先卖老仓再买回），按最小交易单位取整。

成本价就是你买入时填的价格（多次买入取加权平均），所以这两个价位是"从你的成本算起"的。
波动率算不出来（日线不足 15 根、有缺口）时退回典型 ATR 3.5%，并标 nominal，页面会写明"按典型波动估算"。
"""
import exec_spec
import portfolio_book
from shortterm_model import atr_pct


def exit_levels(bars, cost, before=None):
    """bars：日线列表（按日期升序，含 date/high/low/close）。before：只用早于该日期的日线，
    避免把当天还没走完的那一根算进 ATR。"""
    prior = [b for b in bars if before is None or b['date'] < before]
    dates = [b['date'] for b in prior]
    atr = atr_pct({b['date']: b for b in prior}, dates) if len(dates) >= 15 else None
    spec = exec_spec.neutral_exit(atr)
    usable = atr is not None and atr > 0           # 0 = 一字板/停牌造成的假 ATR，和算不出来一样处理
    cost = float(cost)
    return {'stop_pct': spec['stop_pct'], 'target_pct': spec['target_pct'], 'atr_pct': atr if usable else None,
            'nominal': not usable,
            'stop_price': round(cost * (1 - spec['stop_pct']), 2),
            'target_price': round(cost * (1 + spec['target_pct']), 2)}


def tradable_base(holding):
    """今天可以拿来做T的底仓：可卖数量按最小交易单位向下取整。"""
    lot = portfolio_book.lot_size(holding['symbol'])
    sellable = holding.get('sellable_shares', holding['shares'])
    return int(sellable) // lot * lot


def enrich(holding, bars, quote_date=None):
    """账本行 + 系统算出的止损/止盈位与做T底仓。返回新 dict，不改传入的。
    键名沿用 stop_price / target_price / t_base_shares，下游（告警、AI 输入）不用改。"""
    levels = exit_levels(bars, holding['cost_price'], quote_date)
    return {**holding, 'stop_price': levels['stop_price'], 'target_price': levels['target_price'],
            't_base_shares': tradable_base(holding), 'levels': levels}
