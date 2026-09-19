"""Record missed entry windows without requesting quotes or changing positions."""
from datetime import datetime, time
from tushare_sync import read


def expire(state, forecasts, dates, now):
    if not state:
        return
    today = now.date().isoformat()
    held = {p['id'] for p in state.get('positions', [])}
    for f in forecasts:
        if not f.get('paper_eligible') or f['id'] in state['attempted'] or f['id'] in held:
            continue
        # 条件触发计划的有效时段是 09:30–14:00，由 conditional_exec 自己判过期；
        # 15:35 的日线流程不能按"09:35 窗口已过"把它记成跳过。
        if f.get('execution_mode') == 'conditional-intraday-v1':
            continue
        # Require calendar coverage at the start; never guess holidays.
        if not dates or f['eligible_from'] < dates[0]:
            continue
        due = next((d for d in dates if d >= f['eligible_from']), None)
        if due is None or due > today or (due == today and now.time() < time(9,35)):
            continue
        state['attempted'].append(f['id'])
        state['trades'].append({'id':f['id']+'-skip','prediction_id':f['id'],
            'symbol':f['symbol'],'side':'skipped','date':today,
            'reason':'未在首个允许交易日09:30–09:35完成入场；窗口已过，不补造成交。'})


def reconcile(history, report, now):
    path = history / 'tushare_data/trade_cal.json'
    if not path.exists():
        return
    calendars = read(path)['calendars']
    dates = [sorted(datetime.strptime(r['cal_date'],'%Y%m%d').date().isoformat()
                    for r in calendars[e] if str(r['is_open'])=='1') for e in ('SSE','SZSE')]
    if dates[0] != dates[1]:
        return
    expire(report.get('portfolio'), report.get('forecasts', []), dates[0], now)
