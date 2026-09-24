"""持仓的回本进度：综合盈亏（浮动 + 已实现）、等效成本、离回本还差多少。纯函数，没有 I/O。

**为什么要有这一层。** 持仓页原来只显示"现价 vs 成本价"的浮动盈亏。如果你曾经做过 T 或者分批减仓，
账面成本价（`cost_price`）不会变（`portfolio_book.sell()` 明确不改剩余持仓的成本价，只记已实现盈亏），
于是"我这只票到底亏不亏"这件事，账面浮亏和你的钱包实际情况就会对不上——已经落袋的正收益不体现在
浮动盈亏里。这里把两者合并成一个「综合盈亏」，并反推出一个「等效成本」：如果把已实现的部分摊回当前
持仓股数，成本线应该在哪。

**硬约束：`effective_cost` 是派生展示字段，绝不写回 `holdings.json`。** 账本的 `cost_price` 必须保持
券商口径（多次买入加权平均、卖出不改成本），否则账本不可回溯、系统止损/止盈位（`book_levels.py`，
按 `cost_price` 算）也会跟着失真。等效成本只用于页面/告警的"回本进度"展示。

已实现盈亏两个口径都给：`portfolio_book.sell()` 存的 `realized_pnl` 不含费用（和页面浮动盈亏的口径
一致，方便相加）；这里另外算一份含双边费用（滑点+佣金+过户费+印花税，用 `exec_spec.BUY_COST_PCT` /
`SELL_COST_PCT`）的"真实到手"版本，页面默认展示含费的。
"""
import exec_spec


def realized_pnl(trades, symbol):
    """该标的累计已实现盈亏。按 trades 里 side='sell' 汇总，不依赖 holdings——已清仓的标的也能算。

    返回 {'gross': 不含费用（和 portfolio_book.sell() 存的口径一致）, 'net': 含双边费用估算, 'trades': 笔数}。
    """
    sells = [t for t in trades if t.get('symbol') == symbol and t.get('side') == 'sell']
    gross = sum(t.get('realized_pnl') or 0 for t in sells)
    # 含费估算：每笔卖出的毛利润再扣一道「买入侧 + 卖出侧」的费用（按这笔卖出的成交额算，近似）。
    # 精确值需要知道这笔卖出对应哪几笔买入的实际建仓费用；这里用统一费率估算，和 contract_labels.py
    # 的近似口径（ROUND_TRIP_COST_PCT 统一扣）是同一个思路，不追求分厘不差。
    fee_pct = (exec_spec.BUY_COST_PCT + exec_spec.SELL_COST_PCT) / 100
    net = gross - sum(float(t['price']) * int(t['shares']) * fee_pct for t in sells)
    return {'gross': round(gross, 2), 'net': round(net, 2), 'trades': len(sells)}


def effective_cost(holding, realized_net):
    """把已实现盈亏（含费）摊回当前持仓股数后，等效的成本价。只在还有持仓、已实现是正数时才有意义
    （已实现是负数说明之前是割肉出的局，摊回去会把成本线抬高，容易误导——所以只在正数时调低成本线，
    负数时展示原始成本价，caveats 里说明）。"""
    shares = int(holding['shares'])
    cost = float(holding['cost_price'])
    if shares <= 0 or realized_net <= 0:
        return cost
    return round(cost - realized_net / shares, 4)


def to_breakeven_pct(last, eff_cost):
    """现价距等效成本线还差多少个百分点（正数=还没回本，负数=已经回本且盈利这么多）。"""
    if not eff_cost:
        return None
    return round((eff_cost / last - 1) * 100, 4)


def combined(holding, quote, realized):
    """综合盈亏 = 浮动盈亏（不含费，和页面现有口径一致） + 已实现盈亏（含费的 net）。

    holding: portfolio_book 的持仓行；quote: 报价（取 last）；realized: realized_pnl() 的返回。"""
    last = float(quote['last'])
    shares = int(holding['shares'])
    cost = float(holding['cost_price'])
    unrealized = round((last - cost) * shares, 2)
    combined_pnl = round(unrealized + realized['net'], 2)
    eff_cost = effective_cost(holding, realized['net'])
    base = cost * shares
    return {
        'unrealized_pnl': unrealized,
        'realized_pnl': realized['gross'],
        'realized_pnl_net': realized['net'],
        'realized_trades': realized['trades'],
        'combined_pnl': combined_pnl,
        'combined_pct': round(combined_pnl / base * 100, 4) if base else None,
        'effective_cost': eff_cost,
        'to_breakeven_pct': to_breakeven_pct(last, eff_cost) if eff_cost != cost else None,
    }
