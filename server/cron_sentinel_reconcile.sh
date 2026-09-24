#!/bin/bash
# 收盘后对账入口（工作日 15:20），由 alpha-shadow-sentinel-reconcile.timer 调起。
#
# 用当天完整的分钟数据逐条判定今天发出的情景，写 reconcile-<日期>.json；周五顺带推一份
# 周报到企业微信。**这个机制必须和哨兵推送同时上线**：推送每天出现之后，你对"它准不准"的
# 印象会被选择性记忆绑架，只有机械记录能对抗。
#
# 2026-09-24 起同一次运行还跑规则层告警的机制核对 + 结果标签（alert_ledger.py）：
# stop_hit/target_hit/buy_signal 这些规则告警此前从没有人验证过发的对不对；同样每天推日报，
# 周五推周报，写 alert_audit-<日期>.json。
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -u server/sentinel.py reconcile
