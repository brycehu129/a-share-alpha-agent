#!/bin/bash
# 抗跌扫描入口（交易日每 5 分钟），由 alpha-shadow-resilience.timer 调起。
#
# 和 cron_intraday.sh / cron_flow.sh 一样：不跑测试套件、不取流水线锁、不 source
# cron_common.sh（理由见 cron_intraday.sh 的注释）。
#
# 单独一个进程而不是挂进盘中引擎的 tick：引擎按 symbol 轮询报价并跑条件入场/盘中止损，
# 是动钱的路径；这个扫描器是全市场发现型的，不看任何指定 symbol 的报价，也不该有能力
# 拖慢那条链路。东财 clist 接口密集请求会被限流（实测背靠背两轮就会连续 502），
# 取不到就沿用 15 分钟内上一次成功值并标 stale，不重试到超时。
set -euo pipefail
cd "$(dirname "$0")/.."
export HISTORY_DIR="${HISTORY_DIR:-$PWD/.history}"
exec python3 -u server/resilience_scan.py --history "$HISTORY_DIR"
