import base64
import http.client
import json
import os
import re
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import webapp
import wecom_push as wp

VALID_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123"
PASSWORD = "pw-for-tests"
AUTH = "Basic " + base64.b64encode(("admin:%s" % PASSWORD).encode()).decode()


class CheckAuthTests(unittest.TestCase):
    def test_missing_header_rejected(self):
        self.assertFalse(webapp.check_auth({}, "secret"))

    def test_wrong_scheme_rejected(self):
        self.assertFalse(webapp.check_auth({"Authorization": "Bearer x"}, "secret"))

    def test_wrong_password_rejected(self):
        header = "Basic " + base64.b64encode(b"admin:wrong").decode()
        self.assertFalse(webapp.check_auth({"Authorization": header}, "secret"))

    def test_correct_password_accepted_any_username(self):
        header = "Basic " + base64.b64encode(b"anyone:secret").decode()
        self.assertTrue(webapp.check_auth({"Authorization": header}, "secret"))

    def test_malformed_base64_rejected(self):
        self.assertFalse(webapp.check_auth({"Authorization": "Basic ###"}, "secret"))


class BuildTlsContextTests(unittest.TestCase):
    def test_missing_cert_file_raises_clear_error(self):
        with self.assertRaises(wp.ConfigError):
            webapp.build_tls_context("/no/such/cert.pem", "/no/such/key.pem")

    def test_missing_key_file_raises_even_if_cert_exists(self):
        with tempfile.TemporaryDirectory() as d:
            cert = os.path.join(d, "cert.pem")
            open(cert, "w").close()
            with self.assertRaises(wp.ConfigError):
                webapp.build_tls_context(cert, "/no/such/key.pem")


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        env = patch.dict(os.environ, {"ADMIN_PASSWORD": PASSWORD, "CONFIG_PATH": str(self.tmp / "config.json"),
                                      "PRIVATE_DATA_DIR": str(self.tmp / "private"),
                                      # 不指定的话 api_pages 会去读这台机器上真实的 .history（日线缓存、策略评分）。
                                      "HISTORY_DIR": str(self.tmp / "history")})
        env.start()
        self.addCleanup(env.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def request(self, method, path, body=None, headers=None, auth=True):
        h = {"Host": "127.0.0.1:%d" % self.port}
        if auth:
            h["Authorization"] = AUTH
        raw = None
        if body is not None:
            raw = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request(method, path, body=raw, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data, resp

    def json(self, method, path, body=None, **kw):
        status, data, _ = self.request(method, path, body, **kw)
        return status, json.loads(data.decode("utf-8"))


class SinglePageAppTests(ServerCase):
    def test_root_redirects_to_the_dashboard(self):
        status, _, resp = self.request("GET", "/")
        self.assertEqual(status, 302)
        self.assertEqual(resp.getheader("Location"), "/dashboard")

    def test_every_page_route_serves_the_same_app_shell(self):
        """所有页面（含两个旧链接）共用同一个 index.html（同一个顶栏）——导航不会因为切页而变样或跳位置。"""
        bodies = set()
        for route in webapp.SPA_ROUTES:
            status, data, resp = self.request("GET", route)
            self.assertEqual(status, 200, route)
            self.assertIn("text/html", resp.getheader("Content-Type"))
            self.assertEqual(resp.getheader("Cache-Control"), "no-store")
            self.assertIn(b'<div id="app">', data)
            bodies.add(data)
        self.assertEqual(len(bodies), 1)
        self.assertEqual(self.request("GET", "/dashboard/")[0], 200)          # 末尾斜杠也行
        for route in ("/candidates", "/sentinel", "/postclose"):               # 新页面，以及必须继续可打开的旧链接
            self.assertIn(route, webapp.SPA_ROUTES)

    def test_pages_and_api_and_static_require_login(self):
        for path in ("/", "/dashboard", "/settings", "/api/settings", "/static/index.html"):
            self.assertEqual(self.request("GET", path, auth=False)[0], 401, path)

    def test_health_endpoints_need_no_login(self):
        self.assertEqual(self.request("GET", "/health", auth=False)[0], 200)

    def test_unknown_paths_are_404(self):
        for path in ("/nope", "/config", "/llm/key", "/static2/x"):
            self.assertEqual(self.request("GET", path)[0], 404, path)

    def test_missing_build_is_a_clear_503_not_a_blank_page(self):
        with patch.object(webapp, "STATIC_DIR", self.tmp / "no-static"):
            status, data, _ = self.request("GET", "/dashboard")
        self.assertEqual(status, 503)
        self.assertIn("npm run build", data.decode())

    def test_the_committed_build_exists_and_its_assets_are_all_served(self):
        """忘了 `npm run build` 或忘了提交 server/static/ 就让测试红：服务器不装 Node，不会替你构建。"""
        index = (webapp.STATIC_DIR / "index.html")
        self.assertTrue(index.is_file(), "server/static/index.html 不存在：cd web && npm run build，并提交 server/static/")
        refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', index.read_text(encoding="utf-8"))
        self.assertTrue(refs)
        for ref in refs:
            status, _, resp = self.request("GET", ref)
            self.assertEqual(status, 200, ref)
            if ref.startswith("/static/assets/"):
                self.assertIn("immutable", resp.getheader("Cache-Control"))       # 带 hash 的资源长缓存

    def test_static_content_types(self):
        assets = sorted((webapp.STATIC_DIR / "assets").glob("*.js"))
        self.assertTrue(assets)
        _, _, resp = self.request("GET", "/static/assets/" + assets[0].name)
        self.assertIn("javascript", resp.getheader("Content-Type"))

    def test_static_path_traversal_is_blocked(self):
        for path in ("/static/../webapp.py", "/static/%2e%2e/webapp.py", "/static/..%2fwebapp.py",
                     "/static/assets/../../webapp.py", "/static/%2e%2e%2f%2e%2e%2fapi.py", "/static/"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
            conn.request("GET", path, headers={"Host": "127.0.0.1:%d" % self.port, "Authorization": AUTH})
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            self.assertEqual(resp.status, 404, path)
            self.assertNotIn(b"ThreadingHTTPServer", body, path)


class ApiProtocolTests(ServerCase):
    def test_unknown_api_is_a_json_404(self):
        status, payload = self.json("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])

    def test_post_needs_json_and_a_same_origin_caller(self):
        self.assertEqual(self.request("POST", "/api/settings/webhook", {"webhook_url": VALID_URL}, auth=False)[0], 401)
        self.assertEqual(self.request("POST", "/api/settings/webhook", {"webhook_url": VALID_URL},
                                      headers={"Origin": "https://evil.example"})[0], 403)
        self.assertFalse(os.path.exists(os.environ["CONFIG_PATH"]))

    def test_get_only_and_post_only_paths_do_not_cross_over(self):
        self.assertEqual(self.request("POST", "/api/settings", {})[0], 404)
        self.assertEqual(self.request("GET", "/api/settings/webhook")[0], 404)

    def test_internal_errors_become_a_json_500_not_an_html_traceback(self):
        import api
        with patch.dict(api._GET, {"/api/boom": lambda q: 1 / 0}):
            status, data, resp = self.request("GET", "/api/boom")
        self.assertEqual(status, 500)
        self.assertIn("application/json", resp.getheader("Content-Type"))
        self.assertIn("ZeroDivisionError", json.loads(data)["message"])


class SettingsApiTests(ServerCase):
    def test_unconfigured_state(self):
        status, payload = self.json("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(payload["push"], {"configured": False, "masked": None})

    def test_saved_webhook_is_only_ever_returned_masked(self):
        status, payload = self.json("POST", "/api/settings/webhook", {"webhook_url": VALID_URL})
        self.assertEqual(status, 200)
        self.assertTrue(payload["push"]["configured"])
        _, settings = self.request("GET", "/api/settings")[:2]
        self.assertNotIn(VALID_URL.encode(), settings)
        self.assertIn(VALID_URL[-6:].encode(), settings)
        self.assertNotIn(VALID_URL.encode(), json.dumps(payload).encode())

    def test_a_failed_save_is_a_400_with_ok_false(self):
        """旧页面曾把「保存失败」显示成绿色成功样式；现在失败一定是 HTTP 错误 + ok:false，前端据此显示红色。"""
        status, payload = self.json("POST", "/api/settings/webhook", {"webhook_url": "http://evil.example/x"})
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("保存失败", payload["message"])
        self.assertFalse(os.path.exists(os.environ["CONFIG_PATH"]))

    def test_non_string_fields_do_not_crash_validation(self):
        for value in (None, 123, ["x"], {"a": 1}):
            status, payload = self.json("POST", "/api/settings/webhook", {"webhook_url": value})
            self.assertEqual(status, 400, value)

    def test_test_push_without_a_webhook_is_refused_and_sends_nothing(self):
        with patch("api.send_wecom_message") as send:
            status, payload = self.json("POST", "/api/settings/test-push", {})
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        send.assert_not_called()

    def test_test_push_success_and_failure(self):
        wp.save_config(os.environ["CONFIG_PATH"], VALID_URL)
        with patch("api.send_wecom_message") as send:
            status, payload = self.json("POST", "/api/settings/test-push", {})
        self.assertEqual(status, 200)
        send.assert_called_once()
        with patch("api.send_wecom_message", side_effect=wp.PushError("boom")):
            status, payload = self.json("POST", "/api/settings/test-push", {})
        self.assertEqual(status, 502)
        self.assertIn("发送失败", payload["message"])


def flat_bars(n=40, close=10.0):
    """n 根平的日线（一字板，ATR 无效）：够算 MA20/MA60 以外的均线类事实，够触发"按典型波动估算"。"""
    from datetime import date, timedelta
    out, d = [], date(2026, 9, 18)
    while len(out) < n:
        if d.weekday() < 5:
            out.append({"date": d.isoformat(), "close": str(close), "open": str(close), "high": str(close),
                        "low": str(close)})
        d -= timedelta(days=1)
    return list(reversed(out))


def live(symbol="sh600519", last="1300", prev="1300", name="贵州茅台", **kw):
    q = {"symbol": symbol, "market": "cn", "name": name, "last": last, "previous_close": prev, "open": prev,
         "high": last, "low": last, "change_pct": "0.00", "volume_ratio": "1.0", "quote_date": "2026-09-21",
         "quote_at": "2026-09-21T10:00:00+08:00", "age_seconds": 3, "is_index": False,
         "limit_up": None, "limit_down": None}
    q.update(kw)
    return q


def snapshot(*quotes, failures=()):
    return {"quotes": list(quotes), "failures": list(failures), "session": "morning"}


class BookApiTests(ServerCase):
    def watch(self, symbol="600519", name="贵州茅台"):
        return self.json("POST", "/api/book/watch", {"symbol": symbol, "name": name})

    def buy(self, symbol="600519", shares=100, price="1300", **kw):
        return self.json("POST", "/api/book/buy", {"symbol": symbol, "shares": shares, "price": price, **kw})

    def book(self, *quotes, bars=None):
        with patch("live_quote.snapshot", return_value=snapshot(*quotes)), \
                patch("live_check.load_series", return_value=(bars or [], None, None)):
            return self.json("GET", "/api/book")

    def test_watch_buy_sell_remove_flow(self):
        self.assertEqual(self.watch()[0], 200)
        self.assertEqual(self.buy(shares=300, price="1300", date="2026-09-01")[0], 200)
        _, book = self.book(live())
        self.assertEqual([h["symbol"] for h in book["holdings"]], ["sh600519"])
        h = book["holdings"][0]
        self.assertEqual((h["shares"], h["sellable"], h["cost_price"]), (300, 300, 1300.0))
        self.assertEqual([w["symbol"] for w in book["watchlist"]], ["sh600519"])
        self.assertEqual(book["watchlist"][0]["held_shares"], 300)
        status, payload = self.json("POST", "/api/book/sell", {"symbol": "600519", "shares": 100, "price": "1400"})
        self.assertEqual(status, 200)
        _, book = self.book(live())
        self.assertEqual(book["holdings"][0]["shares"], 200)
        self.assertEqual([t["side"] for t in book["trades"]], ["sell", "buy"])
        self.assertEqual(book["trades"][0]["realized_pnl"], 10000.0)
        self.assertEqual(self.json("POST", "/api/book/watch/remove", {"symbol": "sh600519"})[0], 200)

    def test_the_old_direct_holding_entry_is_gone(self):
        """持仓不能凭空录入：只能从自选买入、从持仓卖出。"""
        status, _ = self.json("POST", "/api/book/holding", {"symbol": "600519", "shares": 100, "cost_price": "1300"})
        self.assertEqual(status, 404)

    def test_buying_something_not_on_the_watchlist_is_refused(self):
        status, payload = self.buy()
        self.assertEqual(status, 400)
        self.assertIn("自选", payload["message"])

    def test_same_day_buy_cannot_be_sold_the_same_day(self):
        self.watch()
        self.assertEqual(self.buy()[0], 200)                                   # 日期默认今天
        status, payload = self.json("POST", "/api/book/sell", {"symbol": "600519", "shares": 100, "price": "1400"})
        self.assertEqual(status, 400)
        self.assertIn("T+1", payload["message"])
        _, book = self.book(live())
        self.assertEqual(book["holdings"][0]["sellable"], 0)

    def test_invalid_entries_are_rejected_with_a_readable_message(self):
        status, payload = self.json("POST", "/api/book/buy", {"symbol": "zzz", "shares": "100", "price": "1"})
        self.assertEqual(status, 400)
        self.assertIn("无法识别", payload["message"])
        self.watch()
        status, payload = self.buy(shares=150)
        self.assertEqual(status, 400)
        self.assertIn("100 股的整数倍", payload["message"])

    def test_adding_a_watch_fills_the_name_from_search_when_the_client_did_not_send_one(self):
        with patch("stock_search.resolve", return_value={"symbol": "sz000001", "name": "平安银行"}):
            self.assertEqual(self.json("POST", "/api/book/watch", {"symbol": "000001"})[0], 200)
        _, book = self.book(live("sz000001", "10", "10", "平安银行"))
        self.assertEqual(book["watchlist"][0]["name"], "平安银行")

    def test_adding_an_unknown_code_is_refused_but_a_search_outage_does_not_block_it(self):
        import stock_search
        with patch("stock_search.resolve", return_value=None):
            status, payload = self.json("POST", "/api/book/watch", {"symbol": "000009"})
        self.assertEqual(status, 400)
        self.assertIn("没有找到", payload["message"])
        with patch("stock_search.resolve", side_effect=stock_search.SearchError("down")):
            self.assertEqual(self.json("POST", "/api/book/watch", {"symbol": "000009"})[0], 200)

    def test_rules_endpoint_serves_the_rule_text_for_the_page(self):
        status, p = self.json("GET", "/api/book/rules")
        self.assertEqual(status, 200)
        self.assertEqual(p["holding"]["groups"][0]["label"], "建议彻底卖出")
        self.assertEqual(p["watch"]["groups"][0]["label"], "具备买入信号")
        self.assertIn("前瞻验证", p["disclaimer"])

    def test_search_endpoint(self):
        import stock_search
        hit = {"symbol": "sh600519", "code": "600519", "name": "贵州茅台", "pinyin": "GZMT", "board": ""}
        with patch("stock_search.search", return_value=[hit]):
            status, payload = self.json("GET", "/api/book/search?q=maotai")
        self.assertEqual((status, payload["results"]), (200, [hit]))
        with patch("stock_search.search", side_effect=stock_search.SearchError("搜索接口请求失败")):
            status, payload = self.json("GET", "/api/book/search?q=x")
        self.assertEqual(status, 502)

    def test_lookup_returns_name_price_and_whether_it_is_already_watched_or_held(self):
        self.watch()
        with patch("stock_search.resolve", return_value={"symbol": "sh600519", "name": "贵州茅台"}), \
                patch("live_quote.snapshot", return_value=snapshot(live(last="1350", prev="1300", change_pct="3.85",
                                                                        limit_up="1430", limit_down="1170"))):
            status, p = self.json("GET", "/api/book/lookup?symbol=600519")
        self.assertEqual(status, 200)
        self.assertEqual((p["symbol"], p["name"], p["last"], p["limit_up"], p["in_watch"], p["held_shares"], p["lot"]),
                         ("sh600519", "贵州茅台", 1350.0, "1430", True, 0, 100))

    def test_lookup_of_an_unknown_stock_and_of_garbage(self):
        with patch("stock_search.resolve", return_value=None), patch("live_quote.snapshot", return_value=snapshot()):
            status, p = self.json("GET", "/api/book/lookup?symbol=000009")
        self.assertEqual(status, 400)
        self.assertIn("没有找到", p["message"])
        self.assertEqual(self.json("GET", "/api/book/lookup?symbol=abc")[0], 400)

    def test_lookup_survives_a_quote_outage_using_the_search_name(self):
        with patch("stock_search.resolve", return_value={"symbol": "sh600519", "name": "贵州茅台"}), \
                patch("live_quote.snapshot", side_effect=OSError("down")):
            status, p = self.json("GET", "/api/book/lookup?symbol=600519")
        self.assertEqual((status, p["name"], p["last"]), (200, "贵州茅台", None))
        self.assertIn("取不到行情", p["warning"])

    def test_quote_failure_does_not_hide_the_book_and_is_reported(self):
        self.watch()
        self.buy(date="2026-09-01")
        with patch("live_quote.snapshot", side_effect=OSError("network down")):
            status, book = self.json("GET", "/api/book")
        self.assertEqual(status, 200)
        self.assertEqual(len(book["holdings"]), 1)
        self.assertIn("network down", book["trouble"])
        self.assertIsNone(book["holdings"][0]["last"])
        self.assertIsNone(book["holdings"][0]["verdict"])

    def test_rows_carry_the_systems_verdict_not_a_user_declaration(self):
        # 持仓：现价跌破系统止损位（成本 1300 的 5.25% 以下 = 1231.75）→ 建议彻底卖出
        self.watch()
        self.buy(date="2026-09-01")
        _, book = self.book(live(last="1200", prev="1300", change_pct="-7.69"), bars=flat_bars(close=1300.0))
        v = book["holdings"][0]["verdict"]
        self.assertEqual(v["action"], "exit")
        self.assertIn("系统止损位", "".join(v["reasons"]))
        self.assertAlmostEqual(v["stop_price"], 1231.75, places=2)
        # 现价在成本附近：继续持有，并给出到系统止损/止盈位的距离
        _, book = self.book(live(last="1310", prev="1300", change_pct="0.77"), bars=flat_bars(close=1300.0))
        v = book["holdings"][0]["verdict"]
        self.assertEqual(v["action"], "hold")
        self.assertLess(v["to_stop_pct"], 10)
        self.assertTrue(any("典型波动" in c for c in v["caveats"]))                # 日线是一字板，波动率算不出来
        # 自选：没有买入形态
        w = book["watchlist"][0]["verdict"]
        self.assertEqual(w["action"], "wait")

    def test_a_verdict_that_cannot_be_computed_only_affects_its_own_row(self):
        self.watch()
        self.buy(date="2026-09-01")
        with patch("live_quote.snapshot", return_value=snapshot(live())), \
                patch("live_check.load_series", side_effect=RuntimeError("boom")):
            status, book = self.json("GET", "/api/book")
        self.assertEqual(status, 200)
        self.assertEqual(book["holdings"][0]["verdict"]["action"], "nodata")
        self.assertIn("RuntimeError", book["holdings"][0]["verdict"]["reasons"][0])

    def test_cross_site_write_is_blocked(self):
        status, _, _ = self.request("POST", "/api/book/buy", {"symbol": "600519", "shares": 100, "price": "1"},
                                    headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)


class SentinelInBookApiTests(ServerCase):
    """哨兵不再是独立页面：告警详情挂在持仓/自选每一行的按钮里。"""

    def write_alerts(self, day, *records):
        d = self.tmp / "private" / "sentinel"
        d.mkdir(parents=True, exist_ok=True)
        (d / ("alerts-%s.jsonl" % day)).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))

    def rec(self, blocks, at="10:00:00", sent=True):
        import sentinel_view
        day = sentinel_view.safe_day(None)
        return {"at": "%sT%s+08:00" % (day, at), "text": "x", "push": {"sent": sent},
                "symbols": [b["symbol"] for b in blocks], "blocks": blocks}

    def block(self, symbol, urgent=False):
        return {"symbol": symbol, "name": "测试", "urgent": urgent, "text": "【哨兵】测试 %s" % symbol, "events": [],
                "flow": {"main": -1e8}, "book": {"outer_pct": 40.0}}

    def test_book_rows_carry_todays_alert_counts_and_zero_when_none(self):
        import sentinel_view
        self.write_alerts(sentinel_view.safe_day(None), self.rec([self.block("sh600519", True), self.block("sz000001")]),
                          self.rec([self.block("sh600519")], "10:05:00"))
        self.json("POST", "/api/book/watch", {"symbol": "600519", "name": "茅台"})
        self.json("POST", "/api/book/buy", {"symbol": "600519", "shares": 100, "price": "1300", "date": "2026-09-01"})
        self.json("POST", "/api/book/watch", {"symbol": "000001", "name": "甲"})
        self.json("POST", "/api/book/watch", {"symbol": "000002", "name": "乙"})
        with patch("live_quote.snapshot", return_value={"quotes": [], "failures": []}):
            _, book = self.json("GET", "/api/book")
        self.assertEqual(book["holdings"][0]["alerts"], {"count": 2, "urgent": 1})
        self.assertEqual({w["symbol"]: w["alerts"] for w in book["watchlist"]},
                         {"sh600519": {"count": 2, "urgent": 1}, "sz000001": {"count": 1, "urgent": 0},
                          "sz000002": {"count": 0, "urgent": 0}})

    def test_a_broken_alert_file_never_breaks_the_book_page(self):
        self.json("POST", "/api/book/watch", {"symbol": "600519", "name": "茅台"})
        self.json("POST", "/api/book/buy", {"symbol": "600519", "shares": 100, "price": "1300", "date": "2026-09-01"})
        with patch("live_quote.snapshot", return_value={"quotes": [], "failures": []}):
            with patch("sentinel_view.alert_counts", side_effect=RuntimeError("boom")):
                status, book = self.json("GET", "/api/book")
        self.assertEqual(status, 200)
        self.assertEqual(book["holdings"][0]["alerts"], {"count": 0, "urgent": 0})

    def test_symbol_endpoint_rejects_anything_that_is_not_a_sh_sz_stock_code(self):
        for bad in ("", "../x", "hkHSI", "sz00000", "sz000001;x"):
            status, payload = self.json("GET", "/api/sentinel/symbol?symbol=%s" % bad)
            self.assertEqual(status, 400, bad)
            self.assertIn("股票代码", payload["message"])

    def test_symbol_endpoint_returns_alerts_flow_now_and_the_source_note(self):
        import sentinel_view
        self.write_alerts(sentinel_view.safe_day(None), self.rec([self.block("sz000001", True)]))
        flow = {"rows": [{"t": "0931", "main": -1.0, "small": 0.0, "mid": 0.0, "large": -1.0, "xlarge": 0.0}],
                "as_of": "0931", "main": -1.0, "xlarge": 0.0, "large": -1.0, "mid": 0.0, "small": 0.0,
                "main_5m": -1.0, "main_30m": -1.0, "peak_main": 0.0, "peak_time": "0931", "from_peak": -1.0, "flip": None}
        quote = {"outer_vol": "60", "inner_vol": "40", "bid_ask_ratio": "5.0"}
        with patch("money_flow.fetch_flow", return_value=flow), \
                patch("live_quote.snapshot", return_value={"quotes": [quote], "failures": []}):
            status, p = self.json("GET", "/api/sentinel/symbol?symbol=sz000001")
        self.assertEqual(status, 200)
        self.assertEqual(len(p["alerts"]), 1)
        self.assertTrue(p["alerts"][0]["urgent"])
        self.assertEqual(p["alerts"][0]["flow"], {"main": -1e8})              # 告警当时的快照
        self.assertEqual(p["flow_now"]["main"], -1.0)                          # 此刻的
        self.assertEqual(p["book_now"], {"outer_pct": 60.0, "bid_ask_ratio": 5.0})
        self.assertEqual(p["flow_table"][0]["t"], "09:31")
        self.assertIn("非交易所披露", p["flow_source_note"])
        self.assertIsNone(p["flow_error"])

    def test_flow_failure_degrades_to_a_reason_and_the_rest_still_loads(self):
        import money_flow
        import sentinel_view
        self.write_alerts(sentinel_view.safe_day(None), self.rec([self.block("sz000001")]))
        with patch("money_flow.fetch_flow", side_effect=money_flow.FlowError("资金流请求失败（URLError）")), \
                patch("live_quote.snapshot", side_effect=OSError("down")):
            status, p = self.json("GET", "/api/sentinel/symbol?symbol=sz000001")
        self.assertEqual(status, 200)
        self.assertEqual(len(p["alerts"]), 1)
        self.assertIsNone(p["flow_now"])
        self.assertIn("URLError", p["flow_error"])
        self.assertIsNone(p["book_now"])
        self.assertEqual(p["flow_table"], [])                                  # 没有留存也没有联网：空表，不是报错

    def test_a_past_day_never_goes_to_the_network(self):
        with patch("money_flow.fetch_flow") as fetch, patch("live_quote.snapshot") as snap:
            status, p = self.json("GET", "/api/sentinel/symbol?symbol=sz000001&day=2026-09-18")
        self.assertEqual(status, 200)
        fetch.assert_not_called()
        snap.assert_not_called()
        self.assertEqual(p["day"], "2026-09-18")

    def test_day_summary_has_no_charts_and_reports_collection_health(self):
        _, p = self.json("GET", "/api/sentinel?day=2026-09-18")
        self.assertNotIn("charts", p)
        self.assertEqual((p["collection"]["ticks"], p["collection"]["expected"]), (0, 243))


class DashboardApiTests(ServerCase):
    def test_returns_data_with_stale_and_error_flags(self):
        import dashboard_page
        sample = {"agent": {"version": "x"}}
        no_extra = {"hotmoney_board": None, "limit_counts": None}      # 不读真实的 .history：服务器上那里有龙虎榜数据
        with patch("dashboard_page.get_dashboard_data", return_value=(sample, 1_700_000_000.0, False, None)), \
                patch("market_board.build", return_value=no_extra), patch("market_board.approx_limits", return_value=None):
            status, payload = self.json("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"], sample)
        self.assertFalse(payload["stale"])
        self.assertTrue(payload["fetched_at"].endswith("+08:00"))
        with patch("dashboard_page.get_dashboard_data", return_value=(None, None, False, "无法连接GitHub")):
            status, payload = self.json("GET", "/api/dashboard")
        self.assertEqual(status, 200)                      # 拉取失败是「数据状态」，不是接口错误：前端要显示失败原因
        self.assertIsNone(payload["data"])
        self.assertEqual(payload["error"], "无法连接GitHub")

    def test_market_board_is_added_from_the_server_side_history_and_the_cached_dict_is_untouched(self):
        sample = {"agent": {"version": "x"}}
        extra = {"hotmoney_board": {"hm_date": "2026-09-15", "limit_date": "2026-09-15", "hm_rows": [], "hm_total_rows": 0,
                                    "limit_rows": [], "limit_total_rows": 0},
                 "limit_counts": {"date": "2026-09-15", "up": 3, "down": 1, "broken": 2}}
        with patch("dashboard_page.get_dashboard_data", return_value=(sample, 1_700_000_000.0, False, None)), \
                patch("market_board.build", return_value=extra):
            _, payload = self.json("GET", "/api/dashboard")
        self.assertEqual(payload["data"]["limit_counts"]["up"], 3)
        self.assertEqual(payload["data"]["hotmoney_board"]["hm_date"], "2026-09-15")
        self.assertEqual(sample, {"agent": {"version": "x"}})                   # 缓存里的原对象没被改

    def test_without_exact_limit_data_the_approximate_counts_are_offered_and_labelled(self):
        sample = {"agent": {}}
        none = {"hotmoney_board": None, "limit_counts": None}
        approx = {"up": 12, "down": 3, "source_generated_at": "t", "note": "近似"}
        with patch("dashboard_page.get_dashboard_data", return_value=(sample, 1.0, False, None)), \
                patch("market_board.build", return_value=none), patch("market_board.approx_limits", return_value=approx):
            _, payload = self.json("GET", "/api/dashboard")
        self.assertNotIn("limit_counts", payload["data"])
        self.assertEqual(payload["data"]["limit_approx"]["up"], 12)

    def test_a_failure_in_the_supplement_never_fails_the_dashboard(self):
        sample = {"agent": {"version": "x"}}
        with patch("dashboard_page.get_dashboard_data", return_value=(sample, 1.0, False, None)), \
                patch("market_board.build", side_effect=RuntimeError("boom")):
            status, payload = self.json("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"], sample)

    def test_an_existing_hotmoney_board_from_the_export_is_not_overwritten(self):
        sample = {"hotmoney_board": {"hm_date": "from-export"}}
        extra = {"hotmoney_board": {"hm_date": "from-server"}, "limit_counts": None}
        with patch("dashboard_page.get_dashboard_data", return_value=(sample, 1.0, False, None)), \
                patch("market_board.build", return_value=extra), patch("market_board.approx_limits", return_value=None):
            _, payload = self.json("GET", "/api/dashboard")
        self.assertEqual(payload["data"]["hotmoney_board"]["hm_date"], "from-export")

    def test_refresh_flag_bypasses_the_cache(self):
        with patch("dashboard_page.get_dashboard_data", return_value=(None, None, False, "x")) as get:
            self.json("GET", "/api/dashboard?refresh=1")
            self.json("GET", "/api/dashboard")
        self.assertEqual([c.kwargs["force_refresh"] for c in get.call_args_list], [True, False])


class PostcloseApiTests(ServerCase):
    def test_state_and_empty_report(self):
        with patch("postclose_report.latest_report", return_value=None):
            status, payload = self.json("GET", "/api/postclose")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["report"])
        self.assertFalse(payload["state"]["running"])

    def test_report_html_is_escaped_markdown(self):
        report = {"id": "r1", "ai_meta": {}}
        with patch("postclose_report.latest_report", return_value=report), \
                patch("postclose_report.render", return_value="# <script>alert(1)</script>"):
            _, payload = self.json("GET", "/api/postclose")
        self.assertNotIn("<script>", payload["report"]["html"])
        self.assertIn("&lt;script&gt;", payload["report"]["html"])

    def test_run_is_refused_with_409_when_one_is_already_running(self):
        with patch("webapp_views.start_run", return_value=(False, "已有一次分析正在生成中，请稍候。")):
            status, payload = self.json("POST", "/api/postclose/run", {})
        self.assertEqual(status, 409)
        self.assertIn("正在生成", payload["message"])

    def test_run_passes_the_no_push_choice_through(self):
        with patch("webapp_views.start_run", return_value=(True, "已开始生成")) as start:
            self.json("POST", "/api/postclose/run", {"no_push": True})
            self.json("POST", "/api/postclose/run", {})
        self.assertEqual([c.kwargs["push_enabled"] for c in start.call_args_list], [False, True])


if __name__ == "__main__":
    unittest.main()
