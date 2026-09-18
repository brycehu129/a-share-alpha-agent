#!/bin/bash
# systemd timer entry point replacing daily-agent.yml's `schedule:` triggers
# (07:10 / 08:40 / 15:35 / 18:20 plus hourly 08:17-20:17 recovery checks,
# all Beijing time). Run by alpha-shadow-daily.timer; not meant to be
# invoked by hand except for manual testing.
#
# Ports the workflow's step conditions (continue-on-error vs. required,
# the collect/slot gate) into plain bash. See .github/workflows/daily-agent.yml
# for the GitHub Actions version this replaces.
set -euo pipefail
cd "$(dirname "$0")/.."

exec 9>/var/lock/alpha-shadow-pipeline.lock
flock -w 1500 9

source server/cron_common.sh

GH_OUTPUT="$(mktemp)"; GH_ENV="$(mktemp)"
trap 'rm -f "$GH_OUTPUT" "$GH_ENV"' EXIT
GITHUB_OUTPUT="$GH_OUTPUT" GITHUB_ENV="$GH_ENV" python3 server/report_dispatch.py --history "$HISTORY_DIR"
if ! grep -q '^run=true$' "$GH_OUTPUT"; then
  echo "Outside reporting hours, or this phase is already complete."
  exit 0
fi
set -a; source "$GH_ENV"; set +a
echo "Report slot: ${REPORT_SLOT:-?}  phase: ${REPORT_PHASE:-?}"

python3 -m unittest discover -s server -p 'test_*.py' || { echo "FAILED: test suite" >&2; exit 1; }

JOB_STATUS="success"
fail_required() { echo "FAILED (required step): $*" >&2; JOB_STATUS="failure"; }
warn_optional() { echo "WARN (continue-on-error step failed): $*" >&2; }

bash server/publish_report_status.sh start || { echo "FAILED: publish start status" >&2; exit 1; }
# From here on, always publish a finish status, mirroring `if: always()`.
trap 'REPORT_ID="$REPORT_ID" bash server/publish_report_status.sh finish "$JOB_STATUS" || true; rm -f "$GH_OUTPUT" "$GH_ENV"' EXIT

python3 -u server/tushare_sync.py --history "$HISTORY_DIR" --run-id "$REPORT_ID" || warn_optional tushare_sync

GATE_OUTPUT="$(mktemp)"
if GITHUB_OUTPUT="$GATE_OUTPUT" python3 server/session_brief.py --history "$HISTORY_DIR" --gate-only; then
  COLLECT="$(grep '^collect=' "$GATE_OUTPUT" | cut -d= -f2)"
else
  fail_required session_brief--gate-only
  COLLECT="false"
fi
rm -f "$GATE_OUTPUT"

if [ "$JOB_STATUS" = "success" ]; then
  if [ "$COLLECT" = "true" ] && [ "${REPORT_SLOT:-}" != "prepare" ]; then
    python3 -u server/report_pipeline.py --history "$HISTORY_DIR" --run-id "$REPORT_ID" || warn_optional report_pipeline
    python3 -u server/research_pipeline.py --history "$HISTORY_DIR" --run-id "$REPORT_ID" || warn_optional research_pipeline
  fi

  python3 server/tushare_analysis.py --history "$HISTORY_DIR" --run-id "$REPORT_ID" || fail_required tushare_analysis

  if [ "$JOB_STATUS" = "success" ]; then
    python3 -u server/tushare_bridge.py --history "$HISTORY_DIR" --run-id "$REPORT_ID" || fail_required tushare_bridge
  fi

  if [ "$JOB_STATUS" = "success" ] && [ "$COLLECT" = "true" ] && [ "${REPORT_SLOT:-}" != "prepare" ]; then
    python3 -u server/alpha_data.py --history "$HISTORY_DIR" --run-id "$REPORT_ID" --benchmark-only || warn_optional alpha_data--benchmark-only
    python3 -u server/alpha_engine.py --history "$HISTORY_DIR" --run-id "$REPORT_ID" || fail_required alpha_engine
  fi

  if [ "$JOB_STATUS" = "success" ]; then
    python3 server/session_brief.py --history "$HISTORY_DIR" --run-id "$REPORT_ID" || fail_required session_brief
    python3 server/dashboard_export.py --history "$HISTORY_DIR" || fail_required dashboard_export
  fi

  if [ "$JOB_STATUS" = "success" ]; then
    commit_and_push "Archive scheduled data brief $REPORT_ID" --all || fail_required commit_and_push
  fi
fi

[ "$JOB_STATUS" = "success" ]
