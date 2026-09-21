#!/bin/bash
# 市场复盘数据定时入口（工作日 16:30 / 17:30 北京时间），由 alpha-shadow-review.timer 调起。
#
# 抓取当天涨跌停池 + 龙虎榜落到 server/data/market_review/，再算「次日关注」（规则打分，17:30 那次
# 再加 AI 点评）。这些都是公开行情，**不写 git**，也不碰持仓，所以不用日线流程的锁、也不用同步历史分支。
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
python3 -u server/next_day_watch.py $AI_FLAG
