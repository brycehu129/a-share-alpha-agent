#!/bin/bash
# 私有数据每日快照入口（每天 17:30），由 alpha-shadow-backup.timer 调起。
#
# **故意不先跑全量测试**：cron_postclose.sh 等入口都会先跑一遍测试套件，测试失败就不往下走——
# 对备份来说这是错的，代码有 bug 的那天恰恰最需要一份能回退的快照。备份只依赖 backup.py 本身。
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -u server/backup.py snapshot
