# 部署到 RackNerd（裸 VPS，不是 PaaS）

服务器 `64.188.22.227` 是一台普通的 Ubuntu 22.04 VPS，上面已经跑着一个跟本项目无关
的 `s-ui`（代理/VPN 面板，对外开着 2095/2096/40917），**这次部署不动它、不碰全局
防火墙规则，只新增东西**。

跟 Railway 不同，这里没有平台帮你自动构建/部署，所有东西要自己搭：

- 常驻进程用 `systemd` 管理（开机自启、崩了自动重启）。
- 没有域名，用户要求直接 `IP:端口` 访问，所以给 `server/webapp.py` 加了可选的
  自签名 HTTPS 支持（`TLS_CERT_PATH`/`TLS_KEY_PATH` 环境变量）——不加密的话
  HTTP Basic Auth 密码是明文过网络的，公网IP直接访问必须加密。浏览器会提示
  证书不受信任（因为是自签名，不是CA签发的），点"继续访问"就行，这是预期的，
  不是配置错了。**证书要用 ECDSA（prime256v1）或 RSA，不要用 Ed25519**——
  实测 Python `ssl` 模块配 Ed25519 证书时和 Chrome 协商 TLS 会报
  `ERR_SSL_VERSION_OR_CIPHER_MISMATCH`（curl 因为协商逻辑更宽松能连上，
  掩盖了这个问题，浏览器才会暴露出来），下面脚本已经改成 ECDSA。
- "push自动重新部署"是一个新增的 GitHub Actions workflow
  (`.github/workflows/deploy-racknerd.yml`)：代码改动push后，Actions通过SSH
  连到服务器触发部署，SSH私钥存在GitHub Secrets里，且这个key在服务器上被限制
  成**只能执行一条固定的部署命令**（`command="..."`前缀），就算Secrets哪天泄露，
  拿到这个key也做不了别的事。

## 在服务器上执行一次（下面这一整段脚本）

在已经登录的 `root@64.188.22.227` shell 里粘贴执行。全程幂等，重复跑也安全。

```bash
set -e

# 1. 克隆仓库（公开仓库，只读clone不需要token）
if [ ! -d /opt/alpha-shadow ]; then
  git clone https://github.com/brycehu129/a-share-alpha-agent.git /opt/alpha-shadow
fi
cd /opt/alpha-shadow

# 2. 自签名 TLS 证书（10年有效期，纯粹是为了加密传输，不是为了证明身份）
mkdir -p /opt/alpha-shadow/tls
if [ ! -f /opt/alpha-shadow/tls/cert.pem ]; then
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
    -keyout /opt/alpha-shadow/tls/key.pem \
    -out /opt/alpha-shadow/tls/cert.pem \
    -days 3650 -nodes -subj "/CN=64.188.22.227"
  chmod 600 /opt/alpha-shadow/tls/key.pem
fi

# 3. 配置文件（含随机生成的后台密码，只在第一次生成）
mkdir -p /opt/alpha-shadow/server/data
if [ ! -f /etc/alpha-shadow.env ]; then
  ADMIN_PW=$(openssl rand -base64 24)
  cat > /etc/alpha-shadow.env <<ENVEOF
ADMIN_PASSWORD=$ADMIN_PW
PORT=8080
CONFIG_PATH=/opt/alpha-shadow/server/data/webapp_config.json
TLS_CERT_PATH=/opt/alpha-shadow/tls/cert.pem
TLS_KEY_PATH=/opt/alpha-shadow/tls/key.pem
ENVEOF
  chmod 600 /etc/alpha-shadow.env
  echo ">>> 生成的后台登录密码（只显示这一次，自己先存好）: $ADMIN_PW"
fi

# 4. systemd 服务
cat > /etc/systemd/system/alpha-shadow-webapp.service <<'UNITEOF'
[Unit]
Description=Alpha Shadow WeCom 配置后台
After=network.target

[Service]
Type=simple
EnvironmentFile=/etc/alpha-shadow.env
WorkingDirectory=/opt/alpha-shadow/server
ExecStart=/usr/bin/python3 /opt/alpha-shadow/server/webapp.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNITEOF
systemctl daemon-reload
systemctl enable --now alpha-shadow-webapp
systemctl --no-pager status alpha-shadow-webapp | head -5

# 5. 部署脚本（GitHub Actions 会通过SSH触发这个，push一次跑一次）。真正的
#    逻辑在仓库里的 server/deploy.sh（跟着 git pull 一起更新），这里装的
#    只是一个不用再改的薄包装，避免每次改部署逻辑都要重新登服务器。
cat > /opt/alpha-shadow/deploy.sh <<'DEPLOYEOF'
#!/bin/bash
set -euo pipefail
cd /opt/alpha-shadow
git fetch origin master --quiet
git reset --hard origin/master
exec bash /opt/alpha-shadow/server/deploy.sh
DEPLOYEOF
chmod +x /opt/alpha-shadow/deploy.sh

# 6. 给 GitHub Actions 用的专属部署密钥（和现在这次会话用的是两把不同的key）
#    限制成只能跑 deploy.sh，不能拿这把key执行别的命令。
if [ ! -f /root/.ssh/github_actions_deploy ]; then
  ssh-keygen -t ed25519 -f /root/.ssh/github_actions_deploy -N "" -C "github-actions-deploy" -q
fi
FORCED_LINE="command=\"/opt/alpha-shadow/deploy.sh\",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty $(cat /root/.ssh/github_actions_deploy.pub)"
if ! grep -qF "github-actions-deploy" /root/.ssh/authorized_keys 2>/dev/null; then
  echo "$FORCED_LINE" >> /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
fi

# 7. 开放新端口（如果 ufw 在用，只放行8080，不碰其他规则；ufw没启用就跳过）
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 8080/tcp
fi

echo ""
echo "=================================================================="
echo "把下面这一整行 base64 复制（不是上面PEM格式的私钥本身——多行PEM在网页"
echo "终端里复制粘贴很容易丢换行/截断，导致GitHub Actions报 error in libcrypto，"
echo "单行base64能从根本上避开这个问题），粘到 GitHub 仓库 Settings -> Secrets"
echo "and variables -> Actions -> New repository secret，名字填 RACKNERD_DEPLOY_KEY："
echo "=================================================================="
base64 -w0 /root/.ssh/github_actions_deploy; echo
echo "=================================================================="
echo "再新建一个 secret，名字 RACKNERD_HOST，值填：64.188.22.227"
echo "=================================================================="
```

## 脚本跑完之后，你需要做的事
1. 把脚本打印出来的**后台登录密码**存好（只在第一次运行时打印一次，密码本身
   存在服务器上的 `/etc/alpha-shadow.env`，之后可以用 `cat /etc/alpha-shadow.env`
   随时再看）。
2. 把打印出来的**私钥**完整复制，去 GitHub 仓库网页上添加两个 Secrets：
   `RACKNERD_DEPLOY_KEY`（私钥全文）和 `RACKNERD_HOST`（填 `64.188.22.227`）。
3. 浏览器打开 `https://64.188.22.227:8080/`，会提示"不安全/证书不受信任"，
   这是预期的（自签名证书），点"继续访问"。用户名随便填，密码填第1步存的那个。
4. 以后改 `server/webapp.py`/`server/wecom_push.py` 这几个文件、push到 master，
   GitHub Actions 会自动SSH进服务器跑 `deploy.sh`（`git reset --hard` 到最新
   commit + 重启服务）。想手动触发一次，去 GitHub 仓库的 Actions 页面找到
   "部署到RackNerd" workflow，点 Run workflow 就行。
5. 服务器上的部署日志：`cat /var/log/alpha-shadow-deploy.log`；
   查看服务运行状态/日志：`systemctl status alpha-shadow-webapp` /
   `journalctl -u alpha-shadow-webapp -f`。

## 没有做的事（下一阶段再说）
- 现有的10个 GitHub Actions 日级批处理 workflow（`alpha-engine.yml` /
  `opening-observer.yml` 等）**完全没动**，继续照常跑。要不要把它们也搬到这台
  RackNerd 上、以及分钟级监控要怎么接，等分钟级信号规则定下来后再规划——这次
  只是先把"能常驻、能自动部署"的骨架在真实服务器上跑通。

## 私有数据备份（`backup.py`）

`server/data/private/` 里的东西**只此一份、不进 git**：持仓与自选、exec-0.2 虚拟账本、情景账本与对账、提议队列与修订、盘后报告。

| 层 | 做法 | 防什么 | 不防什么 |
|---|---|---|---|
| 本机每日快照 | `alpha-shadow-backup.timer`，每天 17:30（`Persistent=true`，停机后补跑），`server/data/backups/` 滚动保留 14 份 tar.gz，做完立刻回读校验，校验不过不清理旧的 | 损坏、误删、写坏的账本 | **这台机器本身丢失** |
| 手动下载 | 后台"推送配置"页 →"下载备份到我的电脑" | 机器丢失（这才是异地备份） | 需要你隔一段时间点一下——我没有别处的凭据可以替你自动异地存 |

- **不含凭据**：OpenRouter key（`llm_settings.json`）和企业微信 webhook 都不进归档，恢复后需要重新填。
- 备份入口**故意不先跑全量测试**（别的定时入口都会先跑）：代码有 bug 的那天恰恰最需要一份能回退的快照。
- 失败会：记下原因并在配置页标"需要留意"、推一条企业微信；超过 36 小时没有新的成功快照也会标警告。
- 命令：`python3 server/backup.py snapshot | list | verify [归档] | restore 归档 --to server/data/private [--force]`。恢复默认**拒绝覆盖**内容不同的文件，解包时逐个成员检查路径（不允许绝对路径、`..`、链接）。
- 部署后第一次定时器触发在当天 17:30；想立刻确认：`systemctl start alpha-shadow-backup && journalctl -u alpha-shadow-backup -n 20`。

## 系统健康告警（`health_check.py`）

`alpha-shadow-health.timer` 每 5 分钟检查一遍，出问题推企业微信。**看产出，不看退出码**：盘中引擎最后一轮是不是 4 分钟内、今天的日线报告有没有生成、盘后报告有没有 AI 研判、情景对账、备份、systemd 失败单元、磁盘、HTTPS 证书有效期、行情源质量、评估器报错、研判队列积压、AI key/余额……

- **克制**：`warn` 要连续两次才推、`crit` 立即推；同一问题 crit 每 2 小时最多提醒一次、warn 每 12 小时一次；夜间（22:30–07:00）只发首次 crit；问题消失推"已恢复"；多个问题合并成一条；非交易日、午休、开盘前不会因为"引擎没在跑"报警。
- **`skip` 不算恢复**：报警窗口结束时检查变成"未检查"，不会误报"已恢复"。
- **入口不先跑测试、不取锁**（同备份）：检查器要在别的东西坏掉时照常工作。
- 后台"推送配置"页有"系统健康"面板：每项检查的当前状态、最近一次告警是否送达、检查器自己的心跳（超过 20 分钟没运行会明说"没人在盯着系统了"）。
- 命令：`python3 server/health_check.py status`（最近结果）、`python3 server/health_check.py run --no-send`（现场检查一遍，不推送、不写状态）。

**局限**：
1. 检查器和被检查的系统在同一台机器上，**机器整个挂了它发不出告警**。补救是**外部心跳**：`/health/deep`（不需要登录，只返回 `ok` / `stale` / `critical`，不含任何细节），仓库里的 `.github/workflows/health-ping.yml` 每 30 分钟从 GitHub 一侧访问它，非 200 就让这次运行失败，GitHub 会给你发失败邮件。它只是"活着且没有严重问题"的粗探测；GitHub 定时任务可能延迟几十分钟，仓库 60 天无活动会被自动停用。
2. 没配企业微信 webhook 时告警**发不出去**（页面和检查里都会明说）。
3. 阈值（4 分钟、两次去抖、2/12 小时）是经验值，没在真实盘中校准过，第一周很可能要调。
- 部署后确认：`systemctl list-timers | grep alpha-shadow-health`，`python3 server/health_check.py run --no-send`。
