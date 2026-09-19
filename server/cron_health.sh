#!/bin/bash
# 系统健康检查入口（每 5 分钟），由 alpha-shadow-health.timer 调起。
#
# 和 cron_backup.sh 同理：**不先跑测试套件，不取流水线锁**——检查器要在别的东西坏掉的时候照常工作。
# 只读文件、调一次 systemctl，不碰 .history 的 git 状态。
set -euo pipefail
cd "$(dirname "$0")/.."
export HISTORY_DIR="${HISTORY_DIR:-$PWD/.history}"
exec python3 -u server/health_check.py run
