"""持仓的系统参考价位：止损位、止盈位、可做T的底仓，都由系统算，不由用户声明。

**为什么不再让用户填。** 系统已经在盘中实时盯着这些股票，"什么价位该走"应该由规则给出，而不是靠用户事先
设一个数字、没设就永远不提醒。规则直接复用策略自己的出场规格（exec_spec.neutral_exit）：

- 止损位 = 成本价 × (1 − 止损幅度)，止损幅度 = ATR14% × 1.5，夹在 3%–8%；
- 止盈位 = 成本价 × (1 + 止盈幅度)，止盈幅度 = 止损幅度 × 盈亏比 1.5，封顶 12%；
- 做T底仓 = 今天可卖的老仓（T+1：当天买入的卖不出去，做T只能先卖老仓再买回），按最小交易单位取整。

成本价就是你买入时填的价格（多次买入取加权平均），所以这两个价位是"从你的成本算起"的。
波动率算不出来（日线不足 15 根、有缺口）时退回典型 ATR 3.5%，并标 nominal，页面会写明"按典型波动估算"。

**2026-09-24 起止损位不再只锚成本价（见 effective_stop）。** 只锚成本价有个方向性缺陷：持仓越赚钱，
止损位（成本下方）离现价越远，`sentinel.target_hit` 一旦触发是状态型信号（carry=True），只推一次
就再也不提醒——系统对赚钱的仓位反而越来越沉默。现在改成三条止损取最紧（最高）的一条：成本止损（不变）、
保本止损（浮盈曾达到止盈幅度一半后启动，价位含真实双边费用）、移动止损（跟着历史最高价走，只在观测到
峰值后才启动）。历史最高价存在 `book_state.json`（book_state.py），不是这个模块的职责——这里只接收
调用方算好的 `peak_price`，本模块继续保持"没有 I/O"。
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
            'nominal': not usable, 'breakeven_arm_pct': spec['breakeven_arm_pct'],
            'stop_price': round(cost * (1 - spec['stop_pct']), 2),
            'target_price': round(cost * (1 + spec['target_pct']), 2)}


def tradable_base(holding):
    """今天可以拿来做T的底仓：可卖数量按最小交易单位向下取整。"""
    lot = portfolio_book.lot_size(holding['symbol'])
    sellable = holding.get('sellable_shares', holding['shares'])
    return int(sellable) // lot * lot


def effective_stop(cost, shares, levels, peak_price=None):
    """三条止损取最紧（最高）的一条：

    - **成本止损**：`levels['stop_price']`（不变）。
    - **保本止损**：只在浮盈曾经达到止盈幅度一半时才启动（`levels['breakeven_arm_pct']`，来自
      `exec_spec.neutral_exit` 的 `breakeven_arm_frac`）；保本价对真实双边费用（滑点+佣金+过户费+
      印花税）二分求解，复用 `conditional_exec.breakeven_price`——不用"成本×1.0031"的近似，那样会
      低估卖出成本，算出的"保本价"实际卖出还是亏的。是否已启动由 `peak_price`（历史曾经到过的最高价）
      判断，不需要额外存一个"已武装"的布尔位。
    - **移动止损**：只在观测到过峰值时才启动，`peak_price × (1 − TRAIL_ATR_MULT × ATR%)`；ATR
      算不出来时退回和成本止损同一个典型值，口径一致。

    延迟 import conditional_exec：它经 live_check 间接 import 本模块，模块顶层互相 import 会成环。
    返回 {'price', 'source'}，source ∈ 'cost'/'breakeven'/'trail'，页面据此说明这个止损位怎么来的。"""
    import conditional_exec
    from alpha_model import POLICY
    cost, shares = float(cost), int(shares)
    candidates = [('cost', levels['stop_price'])]
    arm_pct = levels.get('breakeven_arm_pct')
    if peak_price is not None and arm_pct and peak_price >= cost * (1 + arm_pct):
        candidates.append(('breakeven', conditional_exec.breakeven_price(cost, shares, round(cost * shares, 2), POLICY)))
    if peak_price is not None:
        atr = levels['atr_pct'] if levels.get('atr_pct') else exec_spec.NOMINAL_ATR_PCT
        candidates.append(('trail', round(peak_price * (1 - exec_spec.TRAIL_ATR_MULT * atr), 2)))
    source, price = max(candidates, key=lambda c: c[1])
    return {'price': price, 'source': source}


def enrich(holding, bars, quote_date=None, peak_price=None):
    """账本行 + 系统算出的止损/止盈位与做T底仓。返回新 dict，不改传入的。
    键名沿用 stop_price / target_price / t_base_shares，下游（告警、AI 输入）不用改；
    新增 stop_source（止损位是哪一条规则给出的）和 peak_price（原样透传，供页面展示）。

    peak_price 不传（旧调用方）时只有成本止损和保本止损生效，移动止损跳过——不强行要求所有
    调用方都先去 book_state.touch_peak 一次。"""
    levels = exit_levels(bars, holding['cost_price'], quote_date)
    stop = effective_stop(holding['cost_price'], holding['shares'], levels, peak_price)
    return {**holding, 'stop_price': stop['price'], 'stop_source': stop['source'], 'target_price': levels['target_price'],
            't_base_shares': tradable_base(holding), 'levels': levels, 'peak_price': peak_price}
