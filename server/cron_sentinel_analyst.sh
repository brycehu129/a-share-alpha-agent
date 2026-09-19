#!/bin/bash
# 哨兵情景研判入口（交易日每分钟的第 30 秒），由 alpha-shadow-sentinel-analyst.timer 调起。
#
# 和 cron_intraday.sh（每分钟的第 0 秒）是两个独立进程，这是刻意的：告警要快（毫秒级、
# 只含规则层事实），AI 研判要慢（30–90 秒）。放在同一个进程里，慢的会拖住快的——
# 而且 tick 服务只有 50 秒时限，会把等 AI 的进程直接杀掉。
#
# 没有待研判的事件时几乎零成本（不取报价、不调 AI）；上一轮还在跑就直接跳过，不排队。
# 不跑测试套件、不取流水线锁，理由同 cron_intraday.sh。
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -u server/sentinel.py analyze
