"""Scheduled factual brief, without predictions or synthetic portfolio results."""
import argparse
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dashboard_export import latest, verified
from tushare_sync import save


def calendar_state(history, date):
    states = []
    for exchange in ('SSE', 'SZSE'):
        path = history / 'tushare_data/calendars' / (exchange + '.json')
        if not path.exists():
            return 'unknown'
        rows = [r for r in verified(path)['rows'] if r['cal_date'] == date]
        if len(rows) != 1 or str(rows[0]['is_open']) not in ('0', '1'):
            return 'unknown'
        states.append(str(rows[0]['is_open']))
    return 'open' if states == ['1', '1'] else 'closed' if states == ['0', '0'] else 'unknown'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--history', type=Path, required=True)
    p.add_argument('--run-id')
    p.add_argument('--gate-only', action='store_true')
    args = p.parse_args()
    now = datetime.now(timezone(timedelta(hours=8)))
    state = calendar_state(args.history, now.strftime('%Y%m%d'))
    if args.gate_only:
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
                f.write('collect=' + ('false' if state == 'closed' else 'true') + '\n')
        print('Calendar:', state)
        return
    if not args.run_id or not re.fullmatch(r'\d+-\d+', args.run_id):
        raise ValueError('Invalid run ID')
    schedule = os.environ.get('REPORT_SCHEDULE', '')
    mode = '盘前数据整理' if schedule == '0 1 * * 1-5' else '收盘数据整理' if schedule == '30 7 * * 1-5' else '手动数据整理'
    report = {'id': args.run_id, 'generated_at': now.isoformat(), 'mode': mode, 'calendar': state, 'sources': [], 'status': 'data_only'}
    lines = [f'# A股 {mode}', '', f'生成时间：{now.isoformat()}（北京时间）', '', f'交易日状态：{state}。', '',
             '本阶段只整理已采集事实。盘前预测胜率、买卖信号、虚拟成交与盈亏复盘尚未启用。', '']
    if state == 'closed':
        lines += ['今日已核验休市，跳过行情及行业扫描；以下仅列历史记录。', '']
    elif state == 'unknown':
        lines += ['交易日历尚不完整，不能确认今日是否开市；不生成交易信号。', '']
    for name, folder, compact in [('市场快照', 'records', True), ('行业与样本研究', 'research', True), ('Tushare同步', 'tushare_data/runs', False), ('Tushare分析', 'tushare_analysis', False)]:
        path, payload = latest(args.history, folder, compact)
        if payload is None:
            lines += [f'- {name}：尚无数据。']
            continue
        relative = str(path.relative_to(args.history).with_suffix('.md'))
        if folder == 'records':
            relative = 'reports/' + path.stem + '.md'
        url = 'https://github.com/brycehu129/a-share-alpha-agent/blob/market-data/' + relative
        current = path.stem == args.run_id
        report['sources'].append({'kind': name, 'url': url, 'generated_at': payload['generated_at'], 'current_run': current})
        lines += [f'- [{name}]({url})：{payload["generated_at"]} · {payload["status"]} · ' + ('本次生成' if current else '历史记录，未冒充本次新数据')]
        if folder == 'records':
            lines += ['  ' + html.escape(payload['summary'])]
    lines += ['', 'GitHub定时任务可能延迟，以上以实际生成时间为准。网页“检查更新”读取最新已生成快照，不会启动新采集。', '']
    root = args.history / 'briefs'
    root.mkdir(exist_ok=True)
    path = root / (args.run_id + '.json')
    if path.exists():
        raise ValueError('Cannot overwrite brief')
    save(path, report)
    path.with_suffix('.md').write_text('\n'.join(lines))
    (root / 'README.md').write_text('\n'.join(lines))


if __name__ == '__main__':
    main()
