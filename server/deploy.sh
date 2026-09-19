#!/bin/bash
# Runs on the RackNerd server, triggered over SSH by deploy-racknerd.yml
# (see server/RACKNERD_DEPLOY.md). Pulls latest master, restarts the
# always-on webapp, and syncs the systemd units for the scheduled jobs
# (server/RACKNERD_CRON.md) so a schedule/logic change only needs a push —
# no manual server login for routine changes.
set -euo pipefail
cd /opt/alpha-shadow
git fetch origin master --quiet
git reset --hard origin/master

# AI 分析层是本仓库唯一的第三方依赖，只有盘后任务和后台页面用得上；装不上时
# 纯规则的日线流程和开盘观察照常工作，所以这里失败不中断部署。
pip3 install --quiet --upgrade anthropic || echo "WARN: anthropic 安装失败，盘后分析将降级为纯规则报告"

systemctl restart alpha-shadow-webapp

cp server/systemd/alpha-shadow-daily.service server/systemd/alpha-shadow-daily.timer /etc/systemd/system/
cp server/systemd/alpha-shadow-opening.service server/systemd/alpha-shadow-opening.timer /etc/systemd/system/
cp server/systemd/alpha-shadow-postclose.service server/systemd/alpha-shadow-postclose.timer /etc/systemd/system/
cp server/systemd/alpha-shadow-intraday.service server/systemd/alpha-shadow-intraday.timer /etc/systemd/system/
cp server/systemd/alpha-shadow-sentinel-analyst.service server/systemd/alpha-shadow-sentinel-analyst.timer /etc/systemd/system/
cp server/systemd/alpha-shadow-sentinel-reconcile.service server/systemd/alpha-shadow-sentinel-reconcile.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now alpha-shadow-daily.timer alpha-shadow-opening.timer alpha-shadow-postclose.timer alpha-shadow-intraday.timer alpha-shadow-sentinel-analyst.timer alpha-shadow-sentinel-reconcile.timer
systemctl restart alpha-shadow-daily.timer alpha-shadow-opening.timer alpha-shadow-postclose.timer alpha-shadow-intraday.timer alpha-shadow-sentinel-analyst.timer alpha-shadow-sentinel-reconcile.timer

echo "$(date -Is) deployed $(git rev-parse --short HEAD)" >> /var/log/alpha-shadow-deploy.log

# Keep the untracked outer wrapper (the SSH forced-command target,
# /opt/alpha-shadow/deploy.sh) mirroring this file, so it never needs a
# manual edit on the server again regardless of how it got bootstrapped.
cp /opt/alpha-shadow/server/deploy.sh /opt/alpha-shadow/deploy.sh
