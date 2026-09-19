#!/usr/bin/env python3
"""极简后台页面：配置企业微信群机器人 webhook，并可以发测试消息。

设计目标：
- 只用标准库（http.server），不引入任何第三方依赖，和本仓库其余脚本一致。
- 这是本项目第一个"常驻进程"（部署在 Railway 上），和现有的 GitHub Actions
  日级批处理流程完全独立、互不影响——那边继续照常跑，谁也不依赖谁。
- 配置（webhook 地址）落盘到 CONFIG_PATH。Railway 上必须把这个路径挂载到一个
  Volume 上，否则每次重新部署容器文件系统会被重置，配置就丢了。

启动：python3 server/webapp.py
环境变量：
  PORT           监听端口，默认 8080。
  ADMIN_PASSWORD 访问后台页面所需的密码（必须设置，否则拒绝启动）。
  CONFIG_PATH    配置文件路径，默认 server/data/webapp_config.json。
  TLS_CERT_PATH  TLS证书文件路径；和 TLS_KEY_PATH 一起设置时，用 HTTPS 监听。
  TLS_KEY_PATH   TLS私钥文件路径。

  没设置 TLS_CERT_PATH/TLS_KEY_PATH 时用纯 HTTP 监听——这时 Basic Auth 密码是
  明文在网络上传输的，只应该在只有本机/SSH隧道能访问的情况下这么用；一旦要
  用公网IP直接访问，必须配好这两个环境变量（自签名证书就够，见
  server/RACKNERD_DEPLOY.md），否则密码可能被同网络的人截获。
"""
import base64
import hmac
import html
import os
import ssl
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import backup
import exec_spec
import health_check
import llm_settings
import portfolio_book
import webapp_views
from dashboard_page import render_dashboard_page
from wecom_push import (
    ConfigError,
    PushError,
    load_config,
    mask_webhook_url,
    save_config,
    send_wecom_message,
)

CST = timezone(timedelta(hours=8))
DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "data", "webapp_config.json")
# 盘后分析要读归档（候选池、日线缓存）。服务器上 cron_common.sh 把 market-data 分支
# clone 到仓库根目录的 .history/；本机开发可以用 HISTORY_DIR 指到别处。
DEFAULT_HISTORY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".history")


def history_dir():
    return Path(os.environ.get("HISTORY_DIR", DEFAULT_HISTORY_DIR))


def config_path():
    return os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH)


def check_auth(headers, password):
    """标准库手写 HTTP Basic Auth 校验，headers 是类字典对象。"""
    header = headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[len("Basic "):]).decode("utf-8")
    except Exception:
        return False
    _, _, supplied = decoded.partition(":")
    return hmac.compare_digest(supplied, password)


MAX_FORM_BYTES = 100_000


def same_origin(headers):
    """防跨站请求伪造（CSRF）。后台用 Basic Auth，浏览器会给这个站点的一切请求自动带上凭证，
    所以任何一个你顺手打开的网页都能让你的浏览器向这里发 POST——比如把 OpenRouter key 换成
    对方自己的，之后你的持仓、成本价、止损位就全流向对方账户的调用日志。

    浏览器对跨站 POST 一定会带 Sec-Fetch-Site 或 Origin；两个都没有的只可能是 curl 这类
    非浏览器客户端（它们没法被"借用"你的登录态），放行。"""
    site = headers.get("Sec-Fetch-Site")
    if site is not None:
        return site in ("same-origin", "none")
    origin = headers.get("Origin")
    if origin is not None:
        return origin != "null" and urlsplit(origin).netloc == headers.get("Host", "")
    return True


_llm_check_lock = threading.Lock()
SOURCE_LABEL = {"page": "页面保存", "env": "环境变量（/etc/alpha-shadow.env）", "none": "未设置"}


def run_llm_check(timeout=90):
    """测试连接：发一次极小的真实请求（约几分钱）。返回 (成功?, 说明行列表)。
    同一时刻只放一个进去——连点两下没有意义，还会花两份钱。"""
    if not _llm_check_lock.acquire(blocking=False):
        return False, ["已有一次测试正在进行，请稍候。"]
    try:
        import claude_client
        llm_settings.apply()
        return claude_client.check()
    except Exception as exc:  # 页面必须能显示失败原因，而不是 500
        import claude_client
        return False, ["测试异常：" + claude_client._redact("%s: %s" % (type(exc).__name__, exc))]
    finally:
        _llm_check_lock.release()


def render_llm_panel(message="", error=False, check_lines=None, check_ok=None):
    import claude_client
    llm_settings.apply()
    info = llm_settings.describe()
    key = info["api_key"]
    if key["source"] == "none":
        key_status = '<span class="pill status-wait">未配置</span>'
    else:
        key_status = '<span class="pill status-ready">已配置 %s</span> 来源：%s' % (
            html.escape(key["value"]), SOURCE_LABEL[key["source"]])
    notes = []
    if key["env_shadowed"]:
        notes.append("环境变量里也配了一个 key，已被页面保存的覆盖；点“清除”后会回落到环境变量的那个。")
    if os.environ.get("LLM_PROVIDER", "").strip().lower() == "anthropic":
        notes.append("服务器环境变量 LLM_PROVIDER=anthropic，当前后端是直连 Anthropic，"
                     "这里填的 OpenRouter key 不会被用到。")
    if info["problem"]:
        notes.append(info["problem"])
    notes_html = "".join('<p class="hint warn">%s</p>' % html.escape(n) for n in notes)
    msg_html = ('<p class="notice%s">%s</p>' % (" error" if error else "", html.escape(message))
                if message else "")
    check_html = ""
    if check_lines:
        check_html = '<pre class="check %s">%s</pre>' % (
            "ok" if check_ok else "bad", html.escape("\n".join(check_lines)))

    def model_row(field, label, hint):
        cur = info[field]
        shown = "%s（%s）" % (cur["value"], SOURCE_LABEL[cur["source"]]) if cur["value"] else "使用默认值"
        return ('<label>%s</label><input type="text" name="%s" value="%s" placeholder="%s" autocomplete="off">'
                '<p class="hint">当前：%s。%s</p>' % (
                    label, field, html.escape(cur["value"] if cur["source"] == "page" else ""),
                    html.escape(hint), html.escape(shown), "留空 = 使用默认。"))

    return f"""<div class="panel">
  <h2>大模型（OpenRouter）</h2>
  <p>API key：{key_status} · 当前后端：{html.escape(claude_client.provider())}</p>
  {msg_html}{notes_html}
  <form method="post" action="/llm/key" autocomplete="off">
    <label>OpenRouter API key（只写不读：保存后页面只显示末 4 位）</label>
    <input type="password" name="api_key" placeholder="sk-or-v1-..." autocomplete="new-password" spellcheck="false">
    <button type="submit">保存 key</button>
  </form>
  <form method="post" action="/llm/models">
    {model_row("model", "主模型（盘后报告）", "anthropic/claude-opus-5")}
    {model_row("sentinel_model", "哨兵模型（盘中情景，每天最多约 15 次，可选更便宜的）", "anthropic/claude-sonnet-5")}
    <button type="submit">保存模型</button>
  </form>
  <form method="post" action="/llm/check">
    <button type="submit">测试连接（会发一次真实请求，约几分钱）</button>
  </form>
  <form method="post" action="/llm/clear">
    <button type="submit" class="danger">清除页面保存的 key</button>
  </form>
  {check_html}
  <p class="hint">key 保存在服务器 server/data/private/（权限 0600，不进 git）。
  网页是自签名 HTTPS，浏览器会有证书警告，属正常；不要在不信任的网络下使用。</p>
</div>"""


def render_backup_panel(message="", error=False):
    level, text = backup.health()
    cls = {"ok": "status-ready", "warn": "status-wait", "none": "status-wait"}[level]
    msg_html = ('<p class="notice%s">%s</p>' % (" error" if error else "", html.escape(message)) if message else "")
    warn = '<p class="hint warn">%s</p>' % html.escape(text) if level == "warn" else ""
    ok_line = '<p class="hint">%s</p>' % html.escape(text) if level != "warn" else ""
    return f"""<div class="panel">
  <h2>私有数据备份</h2>
  <p>本机每日快照：<span class="pill {cls}">{ {"ok": "正常", "warn": "需要留意", "none": "尚未运行"}[level] }</span></p>
  {msg_html}{warn}{ok_line}
  <form method="post" action="/backup/download">
    <button type="submit">下载备份到我的电脑</button>
  </form>
  <p class="hint">持仓与自选、exec-0.2 账本、情景账本、提议与修订、盘后报告都只存在这台服务器上、不进 git。
  本机快照（每天 17:30，保留 14 份）防的是损坏和误操作，<b>防不了这台机器本身丢失</b>——
  真正的异地备份是上面这个按钮：隔一段时间点一下，把文件存在你自己的电脑上。
  归档<b>不含凭据</b>（OpenRouter key、企业微信 webhook），恢复后需要重新填。</p>
</div>"""


LEVEL_TEXT = {"ok": "正常", "warn": "注意", "crit": "严重", "skip": "未检查"}


def render_health_panel():
    s = health_check.summary()
    if s["heartbeat_age_min"] is None:
        head = '<span class="pill status-wait">尚未运行</span> 健康检查还没有运行过（部署后 5 分钟内第一次触发）。'
    elif s["stale"]:
        head = ('<span class="pill status-wait">已停止</span> 健康检查已 %d 分钟没有运行——<b>没人在盯着系统了</b>，'
                "请在服务器上看 systemctl status alpha-shadow-health.timer。" % s["heartbeat_age_min"])
    else:
        head = '<span class="pill %s">%s</span> 最近一次检查 %d 分钟前。' % (
            "status-wait" if s["critical"] else "status-ready", "有严重问题" if s["critical"] else "运行中", s["heartbeat_age_min"])
    rows = "".join(
        '<tr><td>%s</td><td>%s</td><td class="hint" style="margin:0">%s</td></tr>' % (
            html.escape(c["title"]), LEVEL_TEXT.get(c["level"], html.escape(c["level"])), html.escape(c["message"]))
        for c in sorted(s["checks"], key=lambda c: {"crit": 0, "warn": 1, "ok": 2, "skip": 3}.get(c["level"], 4)))
    delivery = s.get("last_delivery")
    dline = ""
    if delivery:
        dline = '<p class="hint">%s最近一次告警推送（%s）：%s</p>' % (
            "" if delivery["ok"] else "<b>未送达！</b>", html.escape(delivery["at"][:16].replace("T", " ")),
            "已送达" if delivery["ok"] else html.escape(delivery["reason"]))
    return f"""<div class="panel">
  <h2>系统健康</h2>
  <p>{head}</p>{dline}
  <table style="width:100%;border-collapse:collapse;font-size:12.5px"><tbody>{rows or '<tr><td class="hint">暂无检查结果</td></tr>'}</tbody></table>
  <p class="hint">每 5 分钟检查一遍：看的是"该出现的产出有没有出现"（盘中引擎最后一轮、日线报告、盘后 AI 研判、备份、证书、磁盘……），
  不只是进程有没有退出。问题会推企业微信（warn 要连续两次才推，crit 立即推）。<b>这台机器整个挂了它发不出告警</b>——
  仓库里的 GitHub Actions 会定期访问 /health/deep，异常时 GitHub 给你发邮件。</p>
</div>"""


def render_page(message="", llm=None, backup_message=None):
    config = load_config(config_path())
    masked = mask_webhook_url(config.get("webhook_url"))
    configured = bool(masked)
    status = f"已配置（{html.escape(masked)}）" if masked else "尚未配置"
    status_cls = "status-ready" if configured else "status-wait"
    message_html = (
        f'<p class="notice">{html.escape(message)}</p>' if message else ""
    )
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Alpha Shadow 盘中推送配置</title>
<style>
:root{{
  --bg:#f2f5f4; --surface:#ffffff; --surface-2:#e8edec; --border:#d6dedb;
  --ink:#16221d; --ink-2:#48584f; --ink-3:#7c8b83; --accent:#2f5fae; --accent-ink:#fff;
  --status-ready-bg:#dcebf6; --status-ready-fg:#1f4c78;
  --status-wait-bg:#eceef0; --status-wait-fg:#57626b;
  --shadow: 0 1px 2px rgba(22,34,29,.06), 0 8px 24px -12px rgba(22,34,29,.18);
}}
*{{box-sizing:border-box;}}
body{{font-family:"IBM Plex Sans","PingFang SC","Microsoft YaHei",-apple-system,sans-serif;
  max-width:640px;margin:0 auto;padding:28px 20px 48px;background:var(--bg);color:var(--ink);}}
h1{{font-size:24px;font-weight:600;margin:0 0 14px;}}
.nav-pills{{display:inline-flex;gap:2px;padding:3px;background:var(--surface-2);border-radius:999px;border:1px solid var(--border);margin-bottom:18px;}}
.nav-pills a{{display:inline-flex;align-items:center;padding:5px 14px;border-radius:999px;font-size:12.5px;font-weight:600;color:var(--ink-2);text-decoration:none;}}
.nav-pills a:hover{{color:var(--ink);}}
.nav-pills a.active{{background:var(--accent);color:var(--accent-ink);box-shadow:var(--shadow);}}
.panel{{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:18px 18px 20px;box-shadow:var(--shadow);margin-bottom:16px;}}
.pill{{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;font-size:12.5px;font-weight:600;}}
.pill.status-ready{{background:var(--status-ready-bg);color:var(--status-ready-fg);}}
.pill.status-wait{{background:var(--status-wait-bg);color:var(--status-wait-fg);}}
.notice{{font-size:13.5px;color:#0a7d32;background:#e8f5ec;border-radius:8px;padding:8px 12px;margin:0 0 14px;}}
label{{display:block;font-size:12.5px;font-weight:600;color:var(--ink-2);margin-bottom:6px;}}
input[type=text],input[type=password]{{width:100%;padding:9px 10px;box-sizing:border-box;font-family:"JetBrains Mono",ui-monospace,monospace;
  font-size:13px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--ink);}}
input[type=text]:focus,input[type=password]:focus{{outline:2px solid var(--accent);outline-offset:1px;}}
button{{padding:8px 16px;margin-top:10px;margin-right:8px;cursor:pointer;border:1px solid var(--border);
  border-radius:999px;background:var(--surface);color:var(--ink);font-size:13px;font-weight:600;min-height:36px;}}
button:hover{{color:var(--accent);border-color:var(--accent);}}
h2{{font-size:16px;margin:0 0 10px;}}
.hint{{color:var(--ink-3);font-size:12px;margin:6px 0 12px;}}
.hint.warn{{color:#8a5a00;}}
.notice.error{{color:#a1281f;background:#fbeae8;}}
button.danger{{color:#a1281f;}}
pre.check{{white-space:pre-wrap;word-break:break-word;font-size:12px;padding:10px 12px;border-radius:8px;margin:12px 0 0;}}
pre.check.ok{{background:#e8f5ec;color:#0a5a25;}}
pre.check.bad{{background:#fbeae8;color:#a1281f;}}
.footer-note{{color:var(--ink-3);font-size:11.5px;margin-top:24px;}}
</style></head>
<body>
{webapp_views.nav("/")}
<h1>Alpha Shadow 盘中推送配置</h1>
<div class="panel">
  <p>企业微信群机器人 webhook 状态：<span class="pill {status_cls}">{status}</span></p>
  {message_html}
  <form method="post" action="/config">
    <label>企业微信群机器人 Webhook 地址</label>
    <input type="text" name="webhook_url" placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..." autocomplete="off">
    <button type="submit">保存</button>
  </form>
  <form method="post" action="/test-push">
    <button type="submit">发送测试消息</button>
  </form>
</div>
{render_llm_panel(**(llm or {}))}
{render_backup_panel(**(backup_message or {}))}
{render_health_panel()}
<p class="footer-note">由 GitHub Actions 自动部署（push 到 master 后自动生效）</p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "AlphaShadowWebApp/1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _require_auth(self):
        password = os.environ["ADMIN_PASSWORD"]
        if check_auth(self.headers, password):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="alpha-shadow"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("需要登录".encode("utf-8"))
        return False

    def _send_html(self, status, body):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _read_form(self):
        raw = getattr(self, "_body", b"")
        parsed = parse_qs(raw.decode("utf-8", "replace"))
        return {k: v[0] for k, v in parsed.items()}

    def _book_page(self, message="", error=False):
        """持仓/自选页顺带展示现价。取价失败不影响页面本身——账本是本地数据，
        行情只是锦上添花，不能因为网络问题让人连自己录的持仓都看不到。

        但也不能静默：拿不到价的股票在表格里只会显示“—”，不说一声容易被当成
        行情就是这样。所以没有别的消息要显示时，把取价失败报出来。"""
        holdings = portfolio_book.load("holdings")
        watchlist = portfolio_book.load("watchlist")
        quotes, trouble = {}, ""
        symbols = sorted({r["symbol"] for r in holdings} | {r["symbol"] for r in watchlist})
        if symbols:
            try:
                import live_quote
                snapshot = live_quote.snapshot(symbols)
                quotes = {q["symbol"]: q for q in snapshot["quotes"]}
                if snapshot["failures"]:
                    trouble = "%d 只股票未取得行情，现价显示为“—”：%s" % (
                        len(snapshot["failures"]),
                        ",".join(f["symbol"] for f in snapshot["failures"][:5]))
            except (OSError, ValueError) as exc:
                trouble = "行情获取失败，只显示账本数据：%s" % exc
        # 刚做完增删的提示优先；没有的话才报行情问题。
        if trouble and not message:
            message, error = trouble, True
        return webapp_views.render_book_page(holdings, watchlist, quotes, message, error)

    def do_GET(self):
        if self.path == "/health":
            self._send_html(200, "ok")
            return
        if self.path == "/health/deep":
            # 不需要登录，只给外部心跳用：只返回 ok / stale / critical，不含任何细节。
            code, text = health_check.deep_status()
            self._send_html(code, text)
            return
        if self.path in ("/dashboard", "/dashboard/"):
            if not self._require_auth():
                return
            self._send_html(200, render_dashboard_page())
            return
        if self.path in ("/book", "/book/"):
            if not self._require_auth():
                return
            self._send_html(200, self._book_page())
            return
        if self.path.split("?")[0] in ("/sentinel", "/sentinel/"):
            if not self._require_auth():
                return
            import sentinel_view
            from urllib.parse import parse_qs as _qs
            day = (_qs(self.path.partition("?")[2]).get("day") or [None])[0]
            self._send_html(200, sentinel_view.render_sentinel_page(day))
            return
        if self.path in ("/proposals", "/proposals/"):
            if not self._require_auth():
                return
            self._send_html(200, self._proposals_page())
            return
        if self.path in ("/postclose", "/postclose/"):
            if not self._require_auth():
                return
            import postclose_report
            self._send_html(200, webapp_views.render_postclose_page(
                postclose_report.latest_report(), webapp_views.run_state()))
            return
        if self.path != "/":
            self._send_html(404, "not found")
            return
        if not self._require_auth():
            return
        self._send_html(200, render_page())

    def _handle_book_post(self, form):
        actions = {
            "/book/holding": lambda: (portfolio_book.add_holding(form), "已保存持仓。"),
            "/book/watch": lambda: (portfolio_book.add_watch(form), "已加入自选。"),
            "/book/holding/remove": lambda: (
                portfolio_book.remove("holdings", form.get("symbol", "")), "已从持仓中删除。"),
            "/book/watch/remove": lambda: (
                portfolio_book.remove("watchlist", form.get("symbol", "")), "已从自选中删除。"),
        }
        try:
            _, message = actions[self.path]()
            error = False
        except portfolio_book.BookError as exc:
            message, error = str(exc), True
        except OSError as exc:
            message, error = "写入账本失败：%s" % exc, True
        self._send_html(200, self._book_page(message, error))

    def _proposals_page(self, message="", error=False):
        import proposals
        try:
            store = proposals.load()
        except proposals.ProposalError as exc:
            return webapp_views.page("策略参数提议", "/proposals",
                                     '<h1>策略参数提议</h1><p class="notice error">%s</p>' % html.escape(str(exc)))
        return webapp_views.render_proposals_page(store, message, error)

    def _handle_proposals_post(self, form):
        import proposals
        try:
            if self.path == "/proposals/approve":
                rev = proposals.approve(form.get("id", ""), form.get("note", ""))
                message, error = ("已批准 %s：%s.%s %s → %s。从下一次日线流程起新冻结的计划生效（执行版本 %s）。" % (
                    rev["proposal_id"], rev["track"], rev["parameter"], rev["old"], rev["new"],
                    exec_spec.execution_version(rev["revision"]))), False
            elif self.path == "/proposals/reject":
                proposals.reject(form.get("id", ""), form.get("note", ""))
                message, error = "已驳回。", False
            else:
                self._send_html(404, "not found")
                return
        except proposals.ProposalError as exc:
            message, error = str(exc), True
        except OSError as exc:
            message, error = "写入失败：%s" % type(exc).__name__, True
        self._send_html(200, self._proposals_page(message, error))

    def _handle_backup_download(self):
        import io
        root = backup.root_dir()
        try:
            size = backup.total_size(root)
            if size > backup.MAX_DOWNLOAD_BYTES:
                self._send_html(200, render_page(backup_message={
                    "message": "私有数据 %.0f MB，超过网页下载上限 %d MB；请在服务器上用 backup.py snapshot 后自行拷走。" % (
                        size / 1e6, backup.MAX_DOWNLOAD_BYTES // 1_000_000), "error": True}))
                return
            buf = io.BytesIO()
            manifest = backup.build_archive(root, buf)
        except OSError as exc:
            self._send_html(200, render_page(backup_message={"message": "打包失败：%s" % type(exc).__name__, "error": True}))
            return
        if not manifest["files"]:
            self._send_html(200, render_page(backup_message={"message": "私有目录里没有任何文件可备份（目录配错了？）。", "error": True}))
            return
        data = buf.getvalue()
        name = "alpha-shadow-private-%s.tar.gz" % datetime.now(CST).strftime("%Y%m%d-%H%M%S")
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle_llm_post(self, form):
        llm = {}
        try:
            if self.path == "/llm/key":
                llm_settings.save_key(form.get("api_key", ""))
                llm["message"] = "已保存。点“测试连接”确认 key 可用。"
            elif self.path == "/llm/models":
                llm_settings.save_models(form.get("model", ""), form.get("sentinel_model", ""))
                llm["message"] = "模型已保存。"
            elif self.path == "/llm/clear":
                llm_settings.clear_key()
                llm["message"] = "已清除页面保存的 key。"
            elif self.path == "/llm/check":
                ok, lines = run_llm_check()
                llm.update(check_lines=lines, check_ok=ok)
            else:
                self._send_html(404, "not found")
                return
        except llm_settings.SettingsError as exc:
            llm.update(message=str(exc), error=True)
        except OSError as exc:
            llm.update(message="写入失败：%s" % type(exc).__name__, error=True)
        self._send_html(200, render_page(llm=llm))

    def do_POST(self):
        # 先把（有上限的）请求体读掉再决定放不放行：服务端没读完就回 401/403 并关连接，
        # 客户端会收到 connection reset 而不是那条错误页。
        try:
            declared = int(self.headers.get("Content-Length", 0))
        except ValueError:
            declared = -1
        if not 0 <= declared <= MAX_FORM_BYTES:
            self.close_connection = True
            self._send_html(413, "请求体过大或无效")
            return
        self._body = self.rfile.read(declared) if declared else b""
        if not self._require_auth():
            return
        if not same_origin(self.headers):
            self._send_html(403, "拒绝：请求来自其他网站（跨站请求已被拦截）。请直接在本后台页面内操作。")
            return
        if self.path.startswith("/llm/"):
            self._handle_llm_post(self._read_form())
            return
        if self.path.startswith("/proposals/"):
            self._handle_proposals_post(self._read_form())
            return
        if self.path == "/backup/download":
            self._handle_backup_download()
            return
        if self.path.startswith("/book/"):
            if self.path not in ("/book/holding", "/book/watch",
                                 "/book/holding/remove", "/book/watch/remove"):
                self._send_html(404, "not found")
                return
            self._handle_book_post(self._read_form())
            return
        if self.path == "/postclose/run":
            form = self._read_form()
            llm_settings.apply()      # 后台线程在本进程里调用大模型，要用页面保存的 key
            started, message = webapp_views.start_run(
                history_dir(), push_enabled=not form.get("no_push"))
            import postclose_report
            self._send_html(200, webapp_views.render_postclose_page(
                postclose_report.latest_report(), webapp_views.run_state(),
                message, error=not started))
            return
        if self.path == "/config":
            form = self._read_form()
            try:
                save_config(config_path(), form.get("webhook_url", ""))
                message = "已保存。"
            except ConfigError as exc:
                message = f"保存失败：{exc}"
            self._send_html(200, render_page(message))
            return
        if self.path == "/test-push":
            config = load_config(config_path())
            webhook_url = config.get("webhook_url")
            if not webhook_url:
                self._send_html(200, render_page("还没配置 webhook，无法发送测试消息。"))
                return
            now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
            try:
                send_wecom_message(
                    webhook_url, f"[Alpha Shadow] 测试消息，发送于 {now} (UTC+8)"
                )
                message = "测试消息已发送，请检查企业微信群。"
            except PushError as exc:
                message = f"发送失败：{exc}"
            self._send_html(200, render_page(message))
            return
        self._send_html(404, "not found")


def build_tls_context(cert_path, key_path):
    """校验证书/私钥文件存在并可加载，返回一个 server-side SSLContext。"""
    for label, p in (("TLS_CERT_PATH", cert_path), ("TLS_KEY_PATH", key_path)):
        if not p or not os.path.exists(p):
            raise ConfigError(f"{label} 指向的文件不存在: {p!r}")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    return context


def main():
    if not os.environ.get("ADMIN_PASSWORD"):
        raise SystemExit(
            "必须设置环境变量 ADMIN_PASSWORD 才能启动（不允许无密码暴露后台页面）"
        )
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)

    cert_path = os.environ.get("TLS_CERT_PATH")
    key_path = os.environ.get("TLS_KEY_PATH")
    if cert_path or key_path:
        context = build_tls_context(cert_path, key_path)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        print(f"Alpha Shadow webapp listening on https://0.0.0.0:{port}", file=sys.stderr)
    else:
        print(
            f"Alpha Shadow webapp listening on http://0.0.0.0:{port} "
            "(WARNING: no TLS configured, Basic Auth password travels in cleartext)",
            file=sys.stderr,
        )
    server.serve_forever()


if __name__ == "__main__":
    main()
