#!/bin/bash
# 盘后分析定时入口（工作日 16:30 北京时间），由 alpha-shadow-postclose.timer 调起。
#
# 排在 15:35 那批日线流程之后：那批里的 alpha_engine 会用当天收盘数据生成新的
# 候选池，16:30 才能保证用上今天的候选，而不是昨天的。
#
# 和 cron_daily_agent.sh / cron_opening_observer.sh 的关键区别：**这个任务完全
# 不写 git**。盘后报告含真实持仓的成本价和数量，而 market-data 是公开分支——
# 报告只落到 server/data/private/，通过后台页面和企业微信看。
set -euo pipefail
cd "$(dirname "$0")/.."

exec 9>/var/lock/alpha-shadow-pipeline.lock
# 日线流程最长可能跑很久；等不到就这轮不出报告，下一个工作日再说，
# 总比读到半个归档、出一份算错的报告强。
flock -w 900 9

source server/cron_common.sh

# 锁已经被本脚本的 fd 9 持有。flock 按"打开文件描述"计，Python 里再 open 一次
# 会拿到不同的描述、自己等自己，所以明确告诉它别重复获取。
export PIPELINE_LOCK_HELD=1

python3 -m unittest discover -s server -p 'test_*.py'
python3 -u server/postclose_report.py --history "$HISTORY_DIR" --run-id "$REPORT_ID"
