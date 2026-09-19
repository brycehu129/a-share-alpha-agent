"""Explain frozen plans and ledger facts without changing trading decisions."""
from alpha_model import POLICY
from exec_spec import EXEC_MODE, entry_zone
from shortterm_model import ARCHIVE_SIZE

STATUS_TEXT = {'expired': '当日有效时段内未触发，已过期', 'voided': '入场前已作废', 'skipped': '已跳过'}
EXIT_TEXT = {'stop': '止损', 'breakeven_stop': '保本止损', 'target': '止盈', 'time_stop_day1': '首日收盘不及入场价，次日退出',
             'hold_expiry': '持有到期', 'drawdown_pause': '回撤暂停'}


def _conditional_state(f, exec02):
    """条件执行（exec-0.2）计划的真实状态来自盘中账本，不能拿 exec-0.1 那本账去推断——
    那样一条早已过期的计划会永远显示"待入场"。返回 (state, reasons, 持仓或None) 或 None（账本里没有它）。"""
    plan = next((p for p in exec02.get('plans', []) if p['id'] == f['id']), None)
    if plan is None:
        return None
    position = next((p for p in exec02.get('positions', []) if p['id'] == f['id']), None)
    status = plan['status']
    if status == 'watching':
        return 'waiting_buy', ['盘中引擎观察中（%s）' % (plan.get('last_note') or '尚未触发'), '仅当日 09:30–14:00 有效，当日未触发即过期，不补买。'], None
    if status == 'filled':
        if position:
            return 'holding', ['模拟持仓中：止损 %.2f，目标 %.2f%s' % (position['stop'], position['target'],
                                                          '，保本止损已武装' if position['breakeven_armed'] else '')], position
        sell = next((t for t in reversed(exec02.get('trades', [])) if t['side'] == 'sell' and t['prediction_id'] == f['id']), None)
        return 'exited', ['已模拟卖出：' + (EXIT_TEXT.get(sell['reason'], sell['reason']) + '，盈亏 %+.2f%%' % sell['return_pct']
                                         if sell else '（成交明细已滚出快照窗口）')], None
    return 'skipped', ['%s：%s' % (STATUS_TEXT.get(status, status), plan.get('reason') or '—'), '本计划不会补买。'], None


def decisions(agent):
    portfolio=agent.get('portfolio') or {}
    exec02=agent.get('exec02') or {}
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
        cstate=_conditional_state(f,exec02) if f and f.get('execution_mode')==EXEC_MODE else None
        cpos=None
        if cstate:
            state,reasons,cpos=cstate
        elif pos:
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
            if f.get('execution_mode')=='conditional-intraday-v1':
                reasons.append('本计划最早允许日期：'+f.get('eligible_from','待核验')+'；在该日期起首个交易日的 09:30–14:00 内，现价进入入场区间才成交，当日未触发即过期，不补买。')
            else:
                reasons.append('本计划最早允许日期：'+f.get('eligible_from','待核验')+'；仅在该日期起首个交易日的开盘窗口检查，不代表今日已检查。')
            prior_skip=next((t for t in reversed(portfolio.get('trades',[])) if t.get('symbol')==code and t.get('side')=='skipped' and t.get('prediction_id')!=f['id']),None)
            if prior_skip:
                reasons.append('上一计划 '+prior_skip['date']+' 已跳过：'+prior_skip['reason'])
            if portfolio.get('paused'):reasons.append('当前账户风控暂停，新入场会跳过。')
            if len(positions)>=policy['max_positions']:reasons.append('当前持仓已满，执行时仍须核验剩余名额。')
            if portfolio.get('valuation_status')=='blocked':reasons.append('账本估值暂停，先恢复有效估值。')
        else:
            state='observing'
            if f:
                reasons=f.get('plan_reasons') or ['该冻结记录只允许观察；旧记录未保存具体拦截原因，不能用当前条件倒推。']
            elif candidate:
                reasons=['目前只有候选前%d名留档做研究（其中前%d名允许模拟成交），其余保留观察。'%(ARCHIVE_SIZE,POLICY['max_positions']) if list(candidates).index(code)>=ARCHIVE_SIZE else '尚未生成冻结计划，请查看本轮数据和参考价状态。']
            else:reasons=['尚无允许入场的冻结计划。']
        ref=(f or {}).get('reference_price')
        entry=pos.get('entry_price') if pos else None
        entry_low=round(ref*(1+policy['entry_gap_min']),4) if ref else None
        entry_high=round(ref*(1+policy['entry_gap_max']),4) if ref else None
        stop=round(entry*(1-policy['stop_pct']),4) if entry else None
        target=round(entry*(1+policy['target_pct']),4) if entry else None
        hold=policy['hold_sessions']
        if f and f.get('execution_mode')==EXEC_MODE and f.get('exec_spec') and ref:
            # exec-0.2 的入场区间是按 track 分化的（突破：向上确认到追高上限；回调：只设上限），
            # 不是旧的对称 ±3%；显示旧区间会让人以为 09:31 跌到 -3% 也会买。
            lo,hi,_=entry_zone(f['exec_spec']['entry'],ref)
            entry_low=None if lo is None else round(lo,4)
            entry_high=round(hi,4)
            hold=f['exec_spec']['exit']['hold_sessions']
        if cpos:
            entry,stop,target=cpos['entry_price'],cpos['stop'],cpos['target']
        result.append({'symbol':code,'name':(pos or f or candidate or {}).get('name',code),
            'strategy_type':(f or candidate or {}).get('strategy_type'),
            'state':state,'reasons':reasons,'prediction_id':(f or {}).get('id'),
            'created_at':(f or {}).get('created_at'),'as_of':(f or {}).get('as_of'),
            'eligible_from':(f or {}).get('eligible_from'),'reference_price':ref,
            'entry_low':entry_low,'entry_high':entry_high,
            'stop_price':stop,'target_price':target,
            'shares':(cpos or pos or {}).get('shares'),'entry_price':entry,
            'execution_mode':(f or {}).get('execution_mode'),
            'hold_sessions':hold,'max_weight_pct':policy['max_weight']*100,
            'ledger':ledger[-10:]})
    return result
