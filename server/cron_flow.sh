#!/bin/bash
# 资金流留存入口（交易日每 5 分钟），由 alpha-shadow-flow.timer 调起。
#
# 和 cron_intraday.sh 刻意分开：资金流是额外的外部请求（非官方接口，可能慢或被限流），
# 放进每分钟的 tick 会拖垮告警。这里有自己的 40 秒预算，取不到就记一行 error。
# 同样不跑测试套件、不取流水线锁、不 source cron_common.sh（理由见 cron_intraday.sh）。
set -euo pipefail
cd "$(dirname "$0")/.."
export HISTORY_DIR="${HISTORY_DIR:-$PWD/.history}"
exec python3 -u server/flow_recorder.py --history "$HISTORY_DIR"
