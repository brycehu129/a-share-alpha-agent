"""Append-only daily shadow account. No broker API; no retrospective entries."""
import copy
from datetime import datetime, time
from alpha_model import POLICY


def fee(gross, side):
    return round(max(POLICY['minimum_commission'], gross*POLICY['commission']) +
                 gross*POLICY['transfer_fee'] + (gross*POLICY['sell_tax'] if side == 'sell' else 0), 2)


def initial(day, benchmark):
    return {'cash': POLICY['capital'], 'equity': POLICY['capital'], 'nav': 1.0,
            'positions': [], 'trades': [], 'attempted': [], 'last_date': day,
            'inception_date': day, 'benchmark_start': benchmark, 'benchmark_return_pct': 0,
            'excess_pp': 0, 'peak': POLICY['capital'], 'max_drawdown_pct': 0,
            'drawdown_pct': 0, 'paused': False, 'curve': [{'date': day, 'nav': 1.0, 'benchmark_nav': 1.0}],
            'valuation_status': 'current', 'issues': []}


def tradable(bar, previous, code):
    if bar is None or previous is None or float(bar['volume_raw']) <= 0:
        return False
    # Conservative price-limit proxy, not exchange-certified limit prices.
    # ST/new listings excluded upstream. Any near-limit OPEN blocks that fill.
    cap = 0.195 if code.startswith(('sh688', 'sz300', 'sz301')) else 0.095
    return float(bar['high']) > float(bar['low']) and abs(float(bar['open'])/float(previous['close'])-1) < cap


def corporate_event(raw, adjusted, entry_day, day):
    if any(d not in raw or d not in adjusted for d in (entry_day, day)):
        return True
    ratio0 = float(adjusted[entry_day]['close'])/float(raw[entry_day]['close'])
    ratio1 = float(adjusted[day]['close'])/float(raw[day]['close'])
    return abs(ratio1/ratio0-1) > 0.000001


def advance(previous, forecasts, raw, adjusted, benchmark, cutoff):
    state = copy.deepcopy(previous)
    state['issues'] = []
    state['valuation_status'] = 'current'
    bb = {b['date']: b for b in benchmark}
    dates = sorted(d for d in bb if d <= cutoff)
    if not dates or state['last_date'] not in dates:
        state['valuation_status'] = 'blocked'
        state['issues'].append('历史窗口未覆盖账本最后估值日，先补齐数据，不跳过未处理交易日')
        return state
    raw = {s: {b['date']: b for b in bars} for s, bars in raw.items()}
    adj = {s: {b['date']: b for b in bars} for s, bars in adjusted.items()}
    for day in [d for d in dates if d > state['last_date']]:
        index = dates.index(day)
        prev_day = dates[index-1] if index else None
        # Missing valuations or corporate actions freeze the entire day's ledger;
        # no fabricated stale NAV, dividends, split quantities or zero-value exits.
        blocked = [p['symbol'] for p in state['positions'] if day not in raw.get(p['symbol'], {}) or
                   corporate_event(raw.get(p['symbol'], {}), adj.get(p['symbol'], {}), p['entry_day'], day)]
        if blocked:
            state['valuation_status'] = 'blocked'
            state['issues'].append(day + ' 缺少持仓有效估值或发生除权变化，账本暂停推进：' + ','.join(blocked))
            break
        for pos in list(state['positions']):
            code = pos['symbol']
            bar = raw[code][day]
            if pos.get('exit_signal') and pos['entry_day'] < day and tradable(bar, raw[code].get(prev_day), code):
                price = round(float(bar['open'])*(1-POLICY['slippage']), 2)
                gross = round(price*pos['shares'], 2)
                costs = fee(gross, 'sell')
                pnl = round(gross-costs-pos['cost'], 2)
                state['cash'] = round(state['cash']+gross-costs, 2)
                state['trades'].append({'id': pos['id']+'-exit', 'prediction_id': pos['id'], 'symbol': code,
                    'side': 'sell', 'date': day, 'price': price, 'shares': pos['shares'], 'fee': costs,
                    'pnl': pnl, 'return_pct': round(pnl/pos['cost']*100, 4), 'reason': pos['exit_signal']})
                state['positions'].remove(pos)
        for f in sorted(forecasts, key=lambda f: (f['created_at'], f['id'])):
            if f['id'] in state['attempted'] or not f['paper_eligible']:
                continue
            created = datetime.fromisoformat(f['created_at'])
            # First observed benchmark session on/after the frozen eligibility
            # date only. A delayed job cannot backdate the morning forecast.
            entry_days = [d for d in dates if d >= f['eligible_from']]
            if not entry_days or day != entry_days[0]:
                continue
            state['attempted'].append(f['id'])
            reason = None
            code = f['symbol']
            bar = raw.get(code, {}).get(day)
            prev_bar = raw.get(code, {}).get(prev_day)
            if created.date().isoformat() > day or (created.date().isoformat() == day and created.time() >= time(9, 20)):
                reason = '盘前截止后生成，不回填成交'
            elif state['paused'] or len(state['positions']) >= POLICY['max_positions']:
                reason = '风险暂停或持仓已满'
            elif any(p['symbol'] == code for p in state['positions']):
                reason = '已持有该股票'
            elif not tradable(bar, prev_bar, code):
                reason = '缺价、无量、一字行情或开盘接近涨跌停'
            elif corporate_event(raw.get(code, {}), adj.get(code, {}), f['as_of'], day):
                reason = '参考日至入场日复权关系变化或无法核验'
            elif not POLICY['entry_gap_min'] <= float(bar['open'])/f['reference_price']-1 <= POLICY['entry_gap_max']:
                reason = '开盘偏离参考价超过3%'
            if reason:
                state['trades'].append({'id': f['id']+'-skip', 'prediction_id': f['id'], 'symbol': code,
                                        'side': 'skipped', 'date': day, 'reason': reason})
                continue
            price = round(float(bar['open'])*(1+POLICY['slippage']), 2)
            budget = min(state['equity']*POLICY['max_weight'],
                         state['equity']*POLICY['risk_per_trade']/POLICY['stop_pct'], state['cash']-10)
            shares = max(0, int(budget/price/100)*100)
            if code.startswith('sh688') and shares < 200:
                shares = 0
            while shares and shares*price+fee(shares*price, 'buy') > state['cash']:
                shares -= 100
            if shares <= 0 or (code.startswith('sh688') and shares < 200):
                state['trades'].append({'id': f['id']+'-skip', 'prediction_id': f['id'], 'symbol': code,
                                        'side': 'skipped', 'date': day, 'reason': '资金不足最低模拟申报数量'})
                continue
            gross, costs = round(shares*price, 2), fee(shares*price, 'buy')
            state['cash'] = round(state['cash']-gross-costs, 2)
            state['positions'].append({'id': f['id'], 'symbol': code, 'name': f['name'], 'shares': shares,
                'entry_day': day, 'entry_price': price, 'cost': round(gross+costs, 2), 'exit_signal': None})
            state['trades'].append({'id': f['id']+'-entry', 'prediction_id': f['id'], 'symbol': code,
                'side': 'buy', 'date': day, 'price': price, 'shares': shares, 'fee': costs})
        for pos in state['positions']:
            price = float(raw[pos['symbol']][day]['close'])
            pos['mark'] = price
            pos['value'] = round(price*pos['shares'], 2)
            ret = price/pos['entry_price']-1
            held = sum(pos['entry_day'] <= d <= day for d in dates)
            # Close-only signal, next session open fill; never pretend an intraday
            # stop was available from daily OHLC, especially on the T+1 entry day.
            if not pos['exit_signal']:
                pos['exit_signal'] = ('收盘触发止损' if ret <= -POLICY['stop_pct'] else
                    '收盘触发止盈' if ret >= POLICY['target_pct'] else '持有期到期' if held >= POLICY['hold_sessions'] else None)
        state['equity'] = round(state['cash']+sum(p['value'] for p in state['positions']), 2)
        state['nav'] = round(state['equity']/POLICY['capital'], 6)
        state['peak'] = max(state['peak'], state['equity'])
        dd = (1-state['equity']/state['peak'])*100
        state['drawdown_pct'] = round(dd, 4)
        state['max_drawdown_pct'] = round(max(state['max_drawdown_pct'], dd), 4)
        if dd >= POLICY['drawdown_pause']*100:
            state['paused'] = True
            for pos in state['positions']:
                pos['exit_signal'] = '账户回撤风控退出'
        if dd >= POLICY['drawdown_hard_stop']*100:
            state['issues'].append('账户触及50%硬上限；实际可成交价格仍可能导致进一步亏损')
        bn = float(bb[day]['close'])/state['benchmark_start']
        state['benchmark_return_pct'] = round((bn-1)*100, 4)
        state['excess_pp'] = round((state['nav']-bn)*100, 4)
        state['last_date'] = day
        state['curve'].append({'date': day, 'nav': state['nav'], 'benchmark_nav': round(bn, 6)})
    sells = [t for t in state['trades'] if t['side'] == 'sell']
    state['closed_trades'] = len(sells)
    state['trade_win_rate'] = round(sum(t['pnl'] > 0 for t in sells)/len(sells)*100, 2) if sells else None
    return state
