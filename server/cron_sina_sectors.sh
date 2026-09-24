#!/bin/bash
# 个股→新浪行业映射表重建（交易日盘前一次），由 alpha-shadow-sina-sectors.timer 调起。
#
# 这张表只在**东财板块取不到、退回新浪兜底**时才用得上（东财是板块主力，个股行业在它的
# 响应里白送）。但兜底要能用，表就必须是新的，所以每个交易日重建一次。
#
# 为什么单独一个任务：48 个板块逐个翻页约 100 次请求、实测约 130 秒。放进 08:40 的日线
# 流程会挤占那条必须在 09:20 前跑完的预算；放进 5 分钟一轮的扫描器则直接超时。
#
# 成分股盘中不变，所以盘前跑一次就够；load_map() 有 3 天过期保护，漏跑一两次不致命——
# 过期表会被拒收，扫描器退回"板块数据不可用"而不是拿旧表算。
#
# 同样不跑测试套件、不取流水线锁、不 source cron_common.sh（理由见 cron_intraday.sh）。
set -euo pipefail
cd "$(dirname "$0")/.."
export HISTORY_DIR="${HISTORY_DIR:-$PWD/.history}"
exec python3 -u server/sina_sectors.py --build
