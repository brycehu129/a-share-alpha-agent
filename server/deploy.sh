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

systemctl restart alpha-shadow-webapp

cp server/systemd/alpha-shadow-daily.service server/systemd/alpha-shadow-daily.timer /etc/systemd/system/
cp server/systemd/alpha-shadow-opening.service server/systemd/alpha-shadow-opening.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now alpha-shadow-daily.timer alpha-shadow-opening.timer
systemctl restart alpha-shadow-daily.timer alpha-shadow-opening.timer

echo "$(date -Is) deployed $(git rev-parse --short HEAD)" >> /var/log/alpha-shadow-deploy.log
