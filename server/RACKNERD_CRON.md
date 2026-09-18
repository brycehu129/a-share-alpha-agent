# 定时任务迁移到 RackNerd（替代 GitHub Actions schedule）

背景：原来 09:30–09:35 开盘观察窗口经常被 GitHub Actions 的排队延迟错过（所有
定时工作流共用同一个 `market-report-history` 并发组，前面排一个慢任务就会顶
掉后面的），详见开发过程中的分析。程序已经用 [RACKNERD_DEPLOY.md](RACKNERD_DEPLOY.md)
部署到 `64.188.22.227`，这次把原来 `daily-agent.yml`、`opening-observer.yml`
的 `schedule:` 触发直接搬到这台服务器上用 systemd timer 跑，不再依赖 GitHub
Actions 的调度队列。

账本数据（`market-data` 分支）继续用 git 存，历史和现有网页/报告链接不变。
只是"谁来跑、谁来 push"从 GitHub 的 runner 换成这台服务器。

## 改动了什么

- 新增 `server/cron_common.sh`（被下面两个脚本 source，不单独运行）、
  `server/cron_daily_agent.sh`、`server/cron_opening_observer.sh`：把原来
  两个工作流 YAML 里的步骤顺序、`continue-on-error`/必需步骤的区分、
  `collect`/`REPORT_SLOT` 门控逻辑搬成 bash，行为上是一一对应的移植。
- 新增 `server/systemd/*.service` / `*.timer`：四个 systemd 单元文件本体，
  跟着仓库一起 `git pull`，服务器上只需要 `cp` 过去，不用在终端里手打/粘贴
  大段带大括号的配置（VNC 网页控制台粘贴长文本容易被键盘布局搞乱）。
- `.github/workflows/daily-agent.yml`、`.github/workflows/opening-observer.yml`
  去掉了 `schedule:` 触发，保留 `workflow_dispatch`（手动跑）和 push 触发
  （改对应文件时仍会在 GitHub 上跑测试），不会再自动定时执行，避免和服务器
  重复跑、重复 push 打架。
- 其余没有 `schedule:` 的工作流（`tushare-sync.yml`、`research-report.yml`、
  `alpha-engine.yml`、`tushare-extra-sync.yml` 等）本来就不是定时触发的，
  不受影响，继续留在 GitHub Actions 上。

## 服务器上执行一次（下面这一整段脚本）

前提：已经按 RACKNERD_DEPLOY.md 部署过，`/opt/alpha-shadow` 已存在。在已登录
的 `root@64.188.22.227` shell 里粘贴执行，幂等，重复跑也安全。

```bash
set -e
cd /opt/alpha-shadow
git pull --ff-only origin master

if [ ! -f /root/.ssh/market_data_push ]; then
  ssh-keygen -t ed25519 -f /root/.ssh/market_data_push -N "" -C "alpha-shadow-market-data-push" -q
fi

if ! grep -q '^TUSHARE_TOKEN=' /etc/alpha-shadow.env 2>/dev/null; then
  echo 'TUSHARE_TOKEN=REPLACE_ME' >> /etc/alpha-shadow.env
fi
if ! grep -q '^XIAODEFA_TOKEN=' /etc/alpha-shadow.env 2>/dev/null; then
  echo 'XIAODEFA_TOKEN=REPLACE_ME' >> /etc/alpha-shadow.env
fi
chmod 600 /etc/alpha-shadow.env

cp server/systemd/alpha-shadow-daily.service server/systemd/alpha-shadow-daily.timer /etc/systemd/system/
cp server/systemd/alpha-shadow-opening.service server/systemd/alpha-shadow-opening.timer /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now alpha-shadow-daily.timer alpha-shadow-opening.timer
systemctl list-timers 'alpha-shadow-*'

echo ""
echo "=================================================================="
echo "把下面这行公钥完整复制，去 GitHub 仓库网页 Settings -> Deploy keys ->"
echo "Add deploy key，名字随意填（例如 market-data-push），勾选"
echo "\"Allow write access\"，粘贴进去保存。不勾这个勾就push不上去。"
echo "=================================================================="
cat /root/.ssh/market_data_push.pub
echo "=================================================================="
echo "然后编辑 /etc/alpha-shadow.env，把 TUSHARE_TOKEN / XIAODEFA_TOKEN 的"
echo "REPLACE_ME 换成真实 token（跟现有 GitHub Secrets 里的值一样）。"
echo "改完不用重启任何服务，下次定时器触发时会读取最新的 env 文件。"
echo "=================================================================="
```

那四个 `.service`/`.timer` 文件本体在仓库的 `server/systemd/` 目录下，
跟着 `git pull` 一起拿到本地，上面这段脚本只是把它们 `cp` 到 systemd 认的
目录，不用在 VNC 终端里粘贴大段带 `{`/`}` 的配置文本（网页 VNC 的粘贴经常
把这类内容打乱）。

## 脚本跑完之后，你需要做的事

1. 按打印出来的提示，把公钥加到 GitHub 仓库的 Deploy keys（**必须勾选
   "Allow write access"**，否则定时任务能读不能写，push 会失败）。
2. `sudo nano /etc/alpha-shadow.env`（或你习惯的编辑器），把两个 `REPLACE_ME`
   换成真实的 Tushare token 和小得法 token。
3. 想手动跑一次验证配置对不对，直接跑一次 systemd service（不用等定时器）：
   ```bash
   systemctl start alpha-shadow-daily.service
   journalctl -u alpha-shadow-daily.service -f
   ```
   开盘观察同理：`systemctl start alpha-shadow-opening.service`。注意
   `opening_observer.py` 只在北京时间09:30–09:35之间才会真正尝试成交，
   在这个窗口之外手动跑只会验证脚本本身能否跑通、能否 push，不会产生交易。
4. 查看下次触发时间：`systemctl list-timers 'alpha-shadow-*'`。
5. 日常查日志：`journalctl -u alpha-shadow-daily -u alpha-shadow-opening --since today`。

## 以后改定时任务的时间点或脚本逻辑

改 `server/cron_daily_agent.sh` / `cron_opening_observer.sh` / `cron_common.sh`
这几个脚本：直接改完 push 就行，脚本每次被 timer 触发时会自己先 `git pull`
最新代码，不用登服务器。

改 `server/systemd/*.timer`（比如调整触发时间点）：改完 push 后，
`deploy-racknerd.yml` 现在也会因为这个路径变化自动触发部署（见
[server/deploy.sh](deploy.sh)，它会把最新的 `.service`/`.timer` 文件
`cp` 到 `/etc/systemd/system/`、`daemon-reload`、重启两个 timer），同样
不用手动登服务器。`/opt/alpha-shadow/deploy.sh` 本身是个一次性装好、以后
不用再改的薄包装，真正的部署逻辑都在仓库的 `server/deploy.sh` 里。

## 没有做的事 / 已知限制

- 并发保护用的是本机 `flock /var/lock/alpha-shadow-pipeline.lock`，逻辑上
  对应原来 GitHub 的 `market-report-history` 并发组。如果你之后在 GitHub
  网页上手动点了 `workflow_dispatch` 跑 daily-agent/opening-observer，它会
  和服务器各自独立 push，市场数据分支存在极小概率的推送冲突（两边都是先
  fetch再push，冲突时哪边先push成功哪边赢，另一边这次运行会失败，不会
  破坏账本，但需要手动重跑）——平时不手动触发就不会遇到。
- `operations.json` 里 `latest_run.url` 字段原本拼的是 GitHub Actions 运行
  链接，现在 `REPORT_ID` 不再是真的 run id，这个字段会变成一个打不开的
  死链接，纯粹是展示上的小瑕疵，不影响数据和判断逻辑。
- 系统级没有额外发通知/告警；一次运行失败只会体现在 `journalctl` 和
  `operations.json` 里的 `status: failed`，网页上的状态标记会正常反映
  出来，但不会主动推送提醒。
