#!/bin/bash
# 收盘后情景对账入口（工作日 15:20），由 alpha-shadow-sentinel-reconcile.timer 调起。
#
# 用当天完整的分钟数据逐条判定今天发出的情景，写 reconcile-<日期>.json；周五顺带推一份
# 周报到企业微信。**这个机制必须和哨兵推送同时上线**：推送每天出现之后，你对"它准不准"的
# 印象会被选择性记忆绑架，只有机械记录能对抗。
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -u server/sentinel.py reconcile
