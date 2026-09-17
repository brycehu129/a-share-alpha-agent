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

# 5. 部署脚本（GitHub Actions 会通过SSH触发这个，push一次跑一次）
cat > /opt/alpha-shadow/deploy.sh <<'DEPLOYEOF'
#!/bin/bash
set -euo pipefail
cd /opt/alpha-shadow
git fetch origin master --quiet
git reset --hard origin/master
systemctl restart alpha-shadow-webapp
echo "$(date -Is) deployed $(git rev-parse --short HEAD)" >> /var/log/alpha-shadow-deploy.log
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
