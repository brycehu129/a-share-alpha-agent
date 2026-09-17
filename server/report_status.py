"""Publish report lifecycle separately from market snapshots, including failures."""
import argparse
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from collect_quotes import CST
from session_brief import calendar_state
from tushare_sync import read

SLOTS = {'10 23 * * 0-4':'prepare','40 0 * * 1-5':'premarket',
         '35 7 * * 1-5':'close','20 10 * * 1-5':'close'}


def evaluate(agent, expected, job_status, market):
    if job_status != 'success':
        return 'failed'
    if market == 'closed':
        return 'closed'
    if market != 'open' or not agent or agent.get('status') != 'ready':
        return 'partial'
    return 'ready' if agent.get('screen', {}).get('cutoff') == expected else 'partial'


def update(history, run_id, phase, job_status='success'):
    now=datetime.now(CST)
    path=history/'operations.json'
    state=json.loads(path.read_text()) if path.exists() else {'reports':{}}
    slot=os.environ.get('REPORT_SLOT') or SLOTS.get(os.environ.get('REPORT_SCHEDULE',''),'manual')
    today=now.date().isoformat()
    calendar=[{'date':(now.date()+timedelta(days=i)).isoformat(),
               'state':calendar_state(history,(now.date()+timedelta(days=i)).strftime('%Y%m%d'))} for i in range(-30,32)]
    market=next(x['state'] for x in calendar if x['date']==today)
    previous=max((x['date'] for x in calendar if x['date']<today and x['state']=='open'),default='')
    expected=previous if slot in ('prepare','premarket') else today
    old=state.get('latest_run',{})
    run={'id':run_id,'slot':slot,'report_date':today,'expected_date':expected,
         'started_at':old.get('started_at',now.isoformat()) if old.get('id')==run_id else now.isoformat(),
         'updated_at':now.isoformat(),'status':'running','data_date':None,'issues':[],
         'url':'https://github.com/brycehu129/a-share-alpha-agent/actions/runs/'+run_id.split('-')[0]}
    if phase=='finish':
        ap=history/'agent'/(run_id+'.json')
        agent=read(ap) if ap.exists() else None
        run['status']=evaluate(agent,expected,job_status,market)
        if slot=='prepare' and job_status=='success':
            sp=history/'tushare_data/runs'/(run_id+'.json')
            run['status']='ready' if sp.exists() and read(sp)['status']=='success' else 'partial'
        if agent:
            run['data_date']=(agent.get('screen') or {}).get('cutoff')
            run['issues']=agent.get('issues',[])[:5]
        if run['status']=='partial' and run['data_date']!=expected:
            run['issues'].append('行情日期尚未达到本报告要求，保留旧数据等待补齐。')
        if job_status!='success':
            run['issues'].append('整理任务未成功结束，请查看运行记录中的失败步骤。')
        if slot=='premarket' and now.hour>=9:
            run['issues'].append('盘前报告延迟补生成；以实际生成时间为准，不代表开盘前已知，也不回填开盘成交。')
        run['finished_at']=now.isoformat()
        state.setdefault('checkpoints',{})[os.environ.get('REPORT_PHASE') or slot]=run
    if slot in ('premarket','close'):
        state['reports'][slot]=run
    state.update(updated_at=now.isoformat(),latest_run=run,calendar=calendar,
                 schedule={'prepare':'07:10','premarket_start':'08:40','premarket_target':'09:00',
                           'close_start':'15:35','close_final':'18:20','timezone':'Asia/Shanghai'})
    path.write_text(json.dumps(state,ensure_ascii=False,indent=2))
    print('Report lifecycle:',slot,run['status'])


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--history',type=Path,required=True)
    p.add_argument('--run-id',required=True)
    p.add_argument('--phase',choices=['start','finish'],required=True)
    p.add_argument('--job-status',default='success')
    a=p.parse_args()
    update(a.history,a.run_id,a.phase,a.job_status)
