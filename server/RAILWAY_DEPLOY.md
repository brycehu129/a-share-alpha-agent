# 部署到 Railway：企业微信推送配置后台

## 这一步做了什么，没做什么
- 新增了本项目第一个**常驻进程**：`server/webapp.py`，一个只用标准库、没有任何
  第三方依赖的极简 HTTP 服务，提供一个后台页面用来配置企业微信群机器人 webhook，
  并可以发一条测试消息验证连通性。
- **没有**改动现有的 GitHub Actions 日级批处理流程（`alpha-engine.yml` /
  `opening-observer.yml` / `dashboard-export.yml` 等）——那些继续按原来的方式跑，
  和这个新服务完全独立，谁也不依赖谁。
- 分钟级信号监控（真正会调用这个 webhook 推送开仓/止损信号的那部分逻辑）**还没写**，
  等信号规则定下来后再加；这一步只是先把"能推送消息的骨架"立起来。

## 部署前必须做的两件事
1. **在 Railway 项目上挂一个 Volume**，挂载路径比如 `/data`。
   webhook 配置是落盘在一个 JSON 文件里的（`CONFIG_PATH` 指向的路径），Railway
   的容器文件系统在每次重新部署/重启时会被重置——如果不挂 Volume，配置会在你
   保存之后的下一次部署时消失，得重新填一遍。
2. **在 Railway 的环境变量里设置**：
   - `ADMIN_PASSWORD`：访问这个后台页面的密码（必须设置，没设置的话进程会直接
     拒绝启动，不允许无密码暴露在公网）。
   - `CONFIG_PATH`：设成 Volume 挂载路径下的一个文件，比如 `/data/webapp_config.json`。
     不设的话默认写在容器本地的 `server/data/webapp_config.json`，重启就丢。

## 部署方式
仓库根目录已经有 `Procfile`（`web: python3 server/webapp.py`）和一个占位的
`requirements.txt`（内容是空的，只是为了让 Railway 认出这是个 Python 项目）。
- 如果 Railway 项目已经通过 GitHub 集成关联了这个仓库：`git push` 到 `master`
  之后会自动触发构建和部署，不需要额外操作。
- 如果是用 Railway CLI：在仓库根目录跑 `railway up` 即可。

## 部署完之后怎么验证
1. 打开 Railway 分配的公网 URL，浏览器会弹出 Basic Auth 登录框——用户名随便填，
   密码填 `ADMIN_PASSWORD`。
2. 页面顶部会显示 webhook 状态（尚未配置 / 已配置，配置好的地址只显示末6位，
   完整地址不会出现在页面上）。
3. 在企业微信里建一个群机器人，拿到形如
   `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx` 的地址，粘贴到
   页面表单里保存。
4. 点"发送测试消息"，检查对应企业微信群是否收到 `[Alpha Shadow] 测试消息...`。
5. `GET /health` 不需要登录，返回 `ok`——如果 Railway 要配置健康检查，指到这个路径。

## 下一步（等你想清楚分钟级信号规则再做）
盘中监控进程会读同一个 `CONFIG_PATH` 里的配置，调用 `wecom_push.send_wecom_message()`
推送信号——推送这一层已经现成，到时候只需要接信号判断逻辑，不需要再改推送/配置这部分。
