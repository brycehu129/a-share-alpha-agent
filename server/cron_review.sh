#!/bin/bash
# 市场复盘数据定时入口（工作日 16:30 / 17:30 北京时间），由 alpha-shadow-review.timer 调起。
#
# 抓取当天涨跌停池 + 龙虎榜落到 server/data/market_review/，结算上一交易日「次日关注」名单的
# 兑现情况（watch_outcome.py，要在生成当日新名单之前跑，否则今天的文件会被 next_day_watch.py
# 覆盖 next_day_watch 键，watch_outcome 就找不到"当天的原始 pools"），再算当天新的「次日关注」
# （规则打分，17:30 那次再加 AI 点评）。这些都是公开行情，**不写 git**，也不碰持仓，所以不用
# 日线流程的锁、也不用同步历史分支。
# 东财接口偶尔不稳：抓取失败就以非零退出（health 告警会看到），下一次定时再补；已落盘的旧数据不受影响。
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
[ -f /etc/alpha-shadow.env ] && source /etc/alpha-shadow.env
set +a

python3 -u server/market_review.py

# 龙虎榜到 17:30 才补全，AI 点评只在这一次生成，免得白花一次调用。
AI_FLAG=""
if [ "$(TZ=Asia/Shanghai date +%H%M)" -ge 1725 ]; then AI_FLAG="--ai"; fi
python3 -u server/watch_outcome.py $AI_FLAG
python3 -u server/next_day_watch.py $AI_FLAG
