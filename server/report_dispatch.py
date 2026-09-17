"""Route delayed GitHub events by actual local time, with phase checkpoints."""
import argparse
import json
import os
from datetime import datetime, time
from pathlib import Path
from collect_quotes import CST


def choose(now, state):
    today = now.date().isoformat()
    clock = now.time()
    if clock < time(7, 10) or clock >= time(23):
        return None
    slot = 'prepare' if clock < time(8, 40) else 'premarket' if clock < time(15, 35) else 'close'
    phase = 'close_final' if slot == 'close' and clock >= time(18, 20) else slot
    if slot == 'premarket' and clock >= time(9, 35):
        phase = 'premarket_after_open'
    prior = state.get('checkpoints', {}).get(phase, {})
    if prior.get('report_date') == today and prior.get('status') in ('ready', 'closed'):
        return None
    return slot, phase


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--history', type=Path, required=True)
    a = p.parse_args()
    path = a.history / 'operations.json'
    state = json.loads(path.read_text()) if path.exists() else {}
    selected = choose(datetime.now(CST), state)
    with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
        f.write('run=' + ('true' if selected else 'false') + '\n')
    if selected:
        with open(os.environ['GITHUB_ENV'], 'a') as f:
            f.write('REPORT_SLOT=' + selected[0] + '\nREPORT_PHASE=' + selected[1] + '\n')
    print('Actual-time report routing:', selected or 'already complete / outside reporting hours')


if __name__ == '__main__':
    main()
