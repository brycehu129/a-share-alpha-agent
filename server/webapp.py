#!/usr/bin/env python3
"""后台服务：单页应用（web/，Vue 3 + Element Plus）的静态文件 + /api JSON 接口。

设计目标：
- 后端只用标准库（http.server），不引入任何第三方依赖，和本仓库其余脚本一致。
  前端是 web/ 里的 Vue 工程，`cd web && npm run build` 构建到 server/static/，构建产物提交进 git，
  服务器不需要 Node。
- 所有页面（看板/哨兵/提议/盘后分析/持仓与自选/设置）是同一个单页应用，共用一个顶栏：
  这些客户端路由（SPA_ROUTES）都返回同一个 index.html，由前端路由决定显示哪一页。
- 数据接口在 api.py（设置页）和 api_pages.py（其余页面），Handler 只负责鉴权、CSRF 和 IO。
- 这是本项目的常驻进程，和 GitHub Actions 的日级批处理流程完全独立、互不影响。
- 配置（webhook 地址）落盘到 CONFIG_PATH。部署在容器上时必须把这个路径挂载到持久卷，
  否则每次重新部署文件系统会被重置，配置就丢了。

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
import json
import mimetypes
import os
import ssl
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import api
import api_pages  # noqa: F401  导入即注册看板/持仓/提议/盘后/哨兵的 /api 路由
import backup
import health_check
from api import DEFAULT_CONFIG_PATH, DEFAULT_HISTORY_DIR, config_path, history_dir  # noqa: F401  兼容旧的引用路径

CST = timezone(timedelta(hours=8))
# 前端（web/，Vue）的构建产物；`cd web && npm run build` 生成，提交进 git，服务器不需要 Node。
STATIC_DIR = Path(__file__).resolve().parent / "static"
# 单页应用的客户端路由：这些路径都返回同一个 index.html，由前端路由决定显示哪一页。
SPA_ROUTES = ("/dashboard", "/sentinel", "/proposals", "/postclose", "/book", "/settings")


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


class Handler(BaseHTTPRequestHandler):
    server_version = "AlphaShadowWebApp/2"

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

    def _send_bytes(self, status, body, content_type, cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status, body):
        self._send_bytes(status, body.encode("utf-8"), "text/html; charset=utf-8")

    def _send_json(self, status, payload):
        self._send_bytes(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                         "application/json; charset=utf-8")

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_index(self):
        index = STATIC_DIR / "index.html"
        if not index.is_file():
            self._send_html(503, "前端尚未构建：在仓库里执行 cd web && npm install && npm run build，再提交 server/static/。")
            return
        self._send_bytes(200, index.read_bytes(), "text/html; charset=utf-8")

    def _serve_static(self, rel):
        """只提供 STATIC_DIR 之内的文件；resolve() 之后再校验，挡住 ../ 和各种编码花样的路径穿越。"""
        root = STATIC_DIR.resolve()
        try:
            target = (root / unquote(rel)).resolve()
        except (OSError, ValueError):
            self._send_html(404, "not found")
            return
        if root not in target.parents or not target.is_file():
            self._send_html(404, "not found")
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        # 带内容 hash 的 assets/* 永不变，可以长缓存；其余每次校验。
        cache = "public, max-age=31536000, immutable" if rel.startswith("assets/") else "no-cache"
        self._send_bytes(200, target.read_bytes(), ctype, cache)

    def _handle_api_get(self, parts):
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        status, payload = api.dispatch("GET", parts.path, query)
        self._send_json(status, payload)

    def _handle_api_post(self, path):
        if path == "/api/backup/download":
            self._handle_backup_download()
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            # 跨站的纯表单提交发不出 application/json；多这一层要求，CSRF 校验之外再加一道。
            self._send_json(415, {"ok": False, "message": "请求必须是 application/json"})
            return
        try:
            body = json.loads(self._body.decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"ok": False, "message": "请求体不是合法的 JSON 对象"})
            return
        status, payload = api.dispatch("POST", path, None, body)
        self._send_json(status, payload)

    def _handle_backup_download(self):
        import io
        root = backup.root_dir()
        try:
            size = backup.total_size(root)
            if size > backup.MAX_DOWNLOAD_BYTES:
                self._send_json(413, {"ok": False, "message": "私有数据 %.0f MB，超过网页下载上限 %d MB；请在服务器上用 backup.py snapshot 后自行拷走。" % (
                    size / 1e6, backup.MAX_DOWNLOAD_BYTES // 1_000_000)})
                return
            buf = io.BytesIO()
            manifest = backup.build_archive(root, buf)
        except OSError as exc:
            self._send_json(500, {"ok": False, "message": "打包失败：%s" % type(exc).__name__})
            return
        if not manifest["files"]:
            self._send_json(404, {"ok": False, "message": "私有目录里没有任何文件可备份（目录配错了？）。"})
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

    def do_GET(self):
        parts = urlsplit(self.path)
        route = parts.path.rstrip("/") or "/"
        if route == "/health":
            self._send_html(200, "ok")
            return
        if route == "/health/deep":
            # 不需要登录，只给外部心跳用：只返回 ok / stale / critical，不含任何细节。
            code, text = health_check.deep_status()
            self._send_html(code, text)
            return
        served = route == "/" or route in SPA_ROUTES or route.startswith("/static/") or route.startswith("/api/")
        if not served:
            self._send_html(404, "not found")
            return
        if not self._require_auth():
            return
        if route == "/":
            self._redirect("/dashboard")
        elif route in SPA_ROUTES:
            self._serve_index()
        elif route.startswith("/static/"):
            self._serve_static(parts.path[len("/static/"):])
        else:
            self._handle_api_get(parts)

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
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            self._handle_api_post(path)
            return
        self._send_html(404, "not found")


def build_tls_context(cert_path, key_path):
    """校验证书/私钥文件存在并可加载，返回一个 server-side SSLContext。"""
    from wecom_push import ConfigError
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
