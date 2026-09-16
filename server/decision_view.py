"""Explain frozen plans and ledger facts without changing trading decisions."""
from alpha_model import POLICY


def decisions(agent):
    portfolio=agent.get('portfolio') or {}
    forecasts=sorted(agent.get('forecasts',[]),key=lambda f:f['created_at'],reverse=True)
    latest={}
    by_id={f['id']:f for f in forecasts}
    for f in forecasts:
        latest.setdefault(f['symbol'],f)
    positions={p['symbol']:p for p in portfolio.get('positions',[])}
    candidates={c['symbol']:c for c in agent.get('candidates',[])}
    codes=list(dict.fromkeys(list(positions)+list(candidates)+list(latest)))[:100]
    result=[]
    for code in codes:
        pos=positions.get(code)
        f=by_id.get(pos['id']) if pos else latest.get(code)
        candidate=candidates.get(code)
        policy=(f or {}).get('policy') or agent.get('policy') or POLICY
        ledger=[t for t in portfolio.get('trades',[]) if t.get('prediction_id')==(f or pos or {}).get('id')]
        sell=next((t for t in reversed(ledger) if t['side']=='sell'),None)
        skip=next((t for t in reversed(ledger) if t['side']=='skipped'),None)
        reasons=[]
        if pos:
            state='pending_sell' if pos.get('exit_signal') else 'holding'
            reasons=[pos.get('exit_signal') or '已有模拟买入记录，尚未触发收盘退出条件。']
            if portfolio.get('valuation_status')=='blocked':
                reasons+=portfolio.get('issues',[]) or ['缺少有效估值，等待补齐数据后再处理。']
        elif sell:
            state='exited';reasons=[sell.get('reason','已模拟卖出')]
        elif skip:
            state='skipped';reasons=[skip['reason'],'本次入场尝试已结束，不会自动补买。']
        elif f and f.get('paper_eligible'):
            state='waiting_buy';reasons=['冻结记录允许条件式模拟入场；尚无成交记录。']
            if portfolio.get('paused'):reasons.append('当前账户风控暂停，新入场会跳过。')
            if len(positions)>=policy['max_positions']:reasons.append('当前持仓已满，执行时仍须核验剩余名额。')
            if portfolio.get('valuation_status')=='blocked':reasons.append('账本估值暂停，先恢复有效估值。')
        else:
            state='observing'
            if f:
                reasons=f.get('plan_reasons') or ['该冻结记录只允许观察；旧记录未保存具体拦截原因，不能用当前条件倒推。']
            elif candidate:
                reasons=['目前仅候选前3名进入冻结计划检查，其余保留观察。' if list(candidates).index(code)>=3 else '尚未生成冻结计划，请查看本轮数据和参考价状态。']
            else:reasons=['尚无允许入场的冻结计划。']
        ref=(f or {}).get('reference_price')
        entry=pos.get('entry_price') if pos else None
        result.append({'symbol':code,'name':(pos or f or candidate or {}).get('name',code),
            'state':state,'reasons':reasons,'prediction_id':(f or {}).get('id'),
            'created_at':(f or {}).get('created_at'),'as_of':(f or {}).get('as_of'),
            'eligible_from':(f or {}).get('eligible_from'),'reference_price':ref,
            'entry_low':round(ref*(1+policy['entry_gap_min']),4) if ref else None,
            'entry_high':round(ref*(1+policy['entry_gap_max']),4) if ref else None,
            'stop_price':round(entry*(1-policy['stop_pct']),4) if entry else None,
            'target_price':round(entry*(1+policy['target_pct']),4) if entry else None,
            'shares':pos.get('shares') if pos else None,'entry_price':entry,
            'hold_sessions':policy['hold_sessions'],'max_weight_pct':policy['max_weight']*100,
            'ledger':ledger[-10:]})
    return result
