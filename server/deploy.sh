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

# 直连 Anthropic 才需要 anthropic 包（本仓库唯一的第三方依赖）。用 OpenRouter 时只有标准库的
# HTTPS 调用，不需要装任何东西。判断依据是 /etc/alpha-shadow.env：配了 OPENROUTER_API_KEY 且没有
# 显式指定 LLM_PROVIDER=anthropic 就跳过。装不上也不中断部署——纯规则的日线流程和开盘观察照常工作。
if grep -q '^OPENROUTER_API_KEY=.' /etc/alpha-shadow.env 2>/dev/null && ! grep -q '^LLM_PROVIDER=anthropic' /etc/alpha-shadow.env 2>/dev/null; then
  echo "使用 OpenRouter，无需安装 anthropic 包"
else
  pip3 install --quiet --upgrade anthropic || echo "WARN: anthropic 安装失败，AI 研判将降级为纯规则报告"
fi

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
