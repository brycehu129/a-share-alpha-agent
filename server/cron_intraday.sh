#!/bin/bash
# 盘中轮询定时入口（交易日每分钟一轮），由 alpha-shadow-intraday.timer 调起。
#
# 和 cron_daily_agent.sh / cron_opening_observer.sh 的三处刻意不同：
#
# 1. 不跑全量测试套件。那两个每天只跑几次；这个一分钟一轮，每轮先跑几十秒测试
#    根本来不及在下一分钟前完成。测试由部署前的 CI 和另外两个任务把关。
# 2. 不取 /var/lock/alpha-shadow-pipeline.lock。它只读本地的日线缓存与交易日历，
#    不碰 .history 的 git 状态；而日线流程一跑就是几十分钟，加锁只会让盘中轮询
#    整段时间被堵住。（读到 git reset 改写到一半的日历文件时，引擎会把日历判为
#    unknown 而不是运行，宁可漏一轮。）
# 3. 不 source cron_common.sh：那会 git fetch + reset --hard，每分钟折腾一次仓库。
#
# 重叠保护在引擎里：上一轮没结束就跳过本轮，不排队。
set -euo pipefail
cd "$(dirname "$0")/.."
export HISTORY_DIR="${HISTORY_DIR:-$PWD/.history}"
exec python3 -u server/intraday_engine.py --history "$HISTORY_DIR"
