#!/bin/bash
# RackNerd：拉最新 master、重启 webapp、同步全部 systemd 单元（由 deploy-racknerd.yml 经 SSH 触发）。
# 流程包在 main 里、以 `main; exit` 收尾，最后用 install+mv 换掉正在运行的 $R/deploy.sh。
# 否则 bash 会从旧偏移读到新文件的半截文字当命令(127)。本文件别比服务器上的旧版本长。
set -euo pipefail
R=${DEPLOY_ROOT:-/opt/alpha-shadow} U=${UNIT_DIR:-/etc/systemd/system} E=${ENV_FILE:-/etc/alpha-shadow.env}
main() {
  cd "$R"
  git fetch origin master --quiet
  git reset --hard origin/master
  pip3 install --quiet 'jsonschema>=4.18,<5' || echo "WARN: jsonschema 安装失败，DeepSeek 官方研判不可用"
  if ! { grep -q '^OPENROUTER_API_KEY=.' "$E" && ! grep -q '^LLM_PROVIDER=anthropic' "$E"; } 2>/dev/null; then
    pip3 install --quiet --upgrade anthropic || echo "WARN: anthropic 安装失败"
  fi
  systemctl restart alpha-shadow-webapp
  cp server/systemd/*.service server/systemd/*.timer "$U/"
  systemctl daemon-reload
  timers=$(cd server/systemd && ls *.timer)
  systemctl enable --now $timers
  systemctl restart $timers
  echo "$(date -Is) deployed $(git rev-parse --short HEAD)" >> "${DEPLOY_LOG:-/var/log/alpha-shadow-deploy.log}"
  install -m 755 server/deploy.sh "$R/deploy.sh.new" && mv -f "$R/deploy.sh.new" "$R/deploy.sh"
}
main
exit
