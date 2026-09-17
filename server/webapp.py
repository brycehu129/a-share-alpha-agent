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
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

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


def render_page(message=""):
    config = load_config(config_path())
    masked = mask_webhook_url(config.get("webhook_url"))
    status = f"已配置（{html.escape(masked)}）" if masked else "尚未配置"
    message_html = (
        f'<p style="color:#0a7d32">{html.escape(message)}</p>' if message else ""
    )
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>Alpha Shadow 盘中推送配置</title>
<style>
body{{font-family:-apple-system,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;color:#1a1a1a}}
input[type=text]{{width:100%;padding:8px;box-sizing:border-box;font-family:monospace}}
button{{padding:8px 16px;margin-top:8px;margin-right:8px;cursor:pointer}}
.status{{padding:8px 12px;background:#f2f2f2;border-radius:6px;display:inline-block}}
</style></head>
<body>
<h1>Alpha Shadow 盘中推送配置</h1>
<p><a href="/dashboard">→ 看板</a></p>
<p>企业微信群机器人 webhook 状态：<span class="status">{status}</span></p>
{message_html}
<form method="post" action="/config">
  <label>企业微信群机器人 Webhook 地址</label>
  <input type="text" name="webhook_url" placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..." autocomplete="off">
  <button type="submit">保存</button>
</form>
<form method="post" action="/test-push">
  <button type="submit">发送测试消息</button>
</form>
<p style="color:#999;font-size:12px;margin-top:32px">由 GitHub Actions 自动部署（push 到 master 后自动生效）</p>
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
        self.end_headers()
        self.wfile.write(encoded)

    def _read_form(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        parsed = parse_qs(raw.decode("utf-8"))
        return {k: v[0] for k, v in parsed.items()}

    def do_GET(self):
        if self.path == "/health":
            self._send_html(200, "ok")
            return
        if self.path in ("/dashboard", "/dashboard/"):
            if not self._require_auth():
                return
            self._send_html(200, render_dashboard_page())
            return
        if self.path != "/":
            self._send_html(404, "not found")
            return
        if not self._require_auth():
            return
        self._send_html(200, render_page())

    def do_POST(self):
        if not self._require_auth():
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
