#!/bin/bash
# systemd timer entry point replacing opening-observer.yml's `schedule:`
# triggers (previously 09:31 / 09:33 / 09:47 Beijing time on GitHub Actions).
# Run by alpha-shadow-opening.timer; not meant to be invoked by hand except
# for manual testing.
set -euo pipefail
cd "$(dirname "$0")/.."

exec 9>/var/lock/alpha-shadow-pipeline.lock
# Short wait: if a heavier job still holds the lock this deep into the
# 09:30-09:35 observation window, waiting longer only guarantees a miss.
flock -w 90 9

source server/cron_common.sh

python3 -m unittest discover -s server -p 'test_*.py'
python3 -u server/opening_observer.py --history "$HISTORY_DIR" --run-id "$REPORT_ID"
python3 server/dashboard_export.py --history "$HISTORY_DIR"

commit_and_push "Archive observed opening execution $REPORT_ID" --all
