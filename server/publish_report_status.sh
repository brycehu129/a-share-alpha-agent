#!/usr/bin/env bash
set -euo pipefail
python3 server/report_status.py --history .history --run-id "$REPORT_ID" --phase "$1" --job-status "${2:-success}"
git -C .history config user.name 'github-actions[bot]'
git -C .history config user.email '41898282+github-actions[bot]@users.noreply.github.com'
git -C .history add operations.json
git -C .history commit -m "Report lifecycle $REPORT_ID $1"
git -C .history push origin HEAD:market-data
