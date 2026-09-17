import os
import tempfile
import unittest

import webapp
import wecom_push as wp

VALID_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123"


class CheckAuthTests(unittest.TestCase):
    def test_missing_header_rejected(self):
        self.assertFalse(webapp.check_auth({}, "secret"))

    def test_wrong_scheme_rejected(self):
        self.assertFalse(webapp.check_auth({"Authorization": "Bearer x"}, "secret"))

    def test_wrong_password_rejected(self):
        import base64

        header = "Basic " + base64.b64encode(b"admin:wrong").decode()
        self.assertFalse(webapp.check_auth({"Authorization": header}, "secret"))

    def test_correct_password_accepted_any_username(self):
        import base64

        header = "Basic " + base64.b64encode(b"anyone:secret").decode()
        self.assertTrue(webapp.check_auth({"Authorization": header}, "secret"))

    def test_malformed_base64_rejected(self):
        self.assertFalse(webapp.check_auth({"Authorization": "Basic ###"}, "secret"))


class RenderPageTests(unittest.TestCase):
    def test_unconfigured_state_shown(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["CONFIG_PATH"] = os.path.join(d, "config.json")
            try:
                page = webapp.render_page()
            finally:
                del os.environ["CONFIG_PATH"]
            self.assertIn("尚未配置", page)

    def test_configured_state_never_leaks_full_webhook(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            wp.save_config(path, VALID_URL)
            os.environ["CONFIG_PATH"] = path
            try:
                page = webapp.render_page()
            finally:
                del os.environ["CONFIG_PATH"]
            self.assertIn("已配置", page)
            self.assertNotIn(VALID_URL, page)
            self.assertIn(VALID_URL[-6:], page)

    def test_message_is_escaped(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["CONFIG_PATH"] = os.path.join(d, "config.json")
            try:
                page = webapp.render_page("<script>evil()</script>")
            finally:
                del os.environ["CONFIG_PATH"]
            self.assertNotIn("<script>evil()</script>", page)
            self.assertIn("&lt;script&gt;", page)


if __name__ == "__main__":
    unittest.main()
