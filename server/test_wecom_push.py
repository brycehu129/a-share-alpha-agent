import json
import os
import tempfile
import unittest
from unittest.mock import patch

import wecom_push as wp

VALID_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123"


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self, n=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ValidateWebhookUrlTests(unittest.TestCase):
    def test_rejects_empty(self):
        with self.assertRaises(wp.ConfigError):
            wp.validate_webhook_url("")
        with self.assertRaises(wp.ConfigError):
            wp.validate_webhook_url(None)

    def test_rejects_wrong_domain(self):
        with self.assertRaises(wp.ConfigError):
            wp.validate_webhook_url("https://example.com/webhook")

    def test_accepts_official_prefix(self):
        self.assertEqual(wp.validate_webhook_url(VALID_URL), VALID_URL)

    def test_strips_whitespace(self):
        self.assertEqual(wp.validate_webhook_url("  " + VALID_URL + "  "), VALID_URL)


class MaskWebhookUrlTests(unittest.TestCase):
    def test_none_stays_none(self):
        self.assertIsNone(wp.mask_webhook_url(None))

    def test_only_shows_tail(self):
        masked = wp.mask_webhook_url(VALID_URL)
        self.assertTrue(masked.startswith("..."))
        self.assertEqual(masked, "..." + VALID_URL[-6:])
        self.assertNotIn("qyapi.weixin.qq.com", masked)


class ConfigRoundTripTests(unittest.TestCase):
    def test_missing_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "nested", "config.json")
            self.assertEqual(wp.load_config(path), {"webhook_url": None})

    def test_save_then_load_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            wp.save_config(path, VALID_URL)
            self.assertEqual(wp.load_config(path), {"webhook_url": VALID_URL})

    def test_save_rejects_invalid_url_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            with self.assertRaises(wp.ConfigError):
                wp.save_config(path, "not-a-webhook")
            self.assertFalse(os.path.exists(path))
            self.assertEqual(os.listdir(d), [])

    def test_corrupt_config_file_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            with open(path, "w") as f:
                f.write("[1, 2, 3]")
            with self.assertRaises(wp.ConfigError):
                wp.load_config(path)


class SendWecomMessageTests(unittest.TestCase):
    def test_success(self):
        ok_body = json.dumps({"errcode": 0, "errmsg": "ok"}).encode("utf-8")
        with patch("wecom_push.urlopen", return_value=FakeResponse(ok_body)) as mock_open:
            result = wp.send_wecom_message(VALID_URL, "hello")
        self.assertEqual(result["errcode"], 0)
        request = mock_open.call_args[0][0]
        self.assertEqual(request.full_url, VALID_URL)
        sent = json.loads(request.data.decode("utf-8"))
        self.assertEqual(sent, {"msgtype": "text", "text": {"content": "hello"}})

    def test_nonzero_errcode_raises(self):
        body = json.dumps({"errcode": 93000, "errmsg": "invalid webhook url"}).encode("utf-8")
        with patch("wecom_push.urlopen", return_value=FakeResponse(body)):
            with self.assertRaises(wp.PushError):
                wp.send_wecom_message(VALID_URL, "hello")

    def test_invalid_url_never_makes_a_request(self):
        with patch("wecom_push.urlopen") as mock_open:
            with self.assertRaises(wp.ConfigError):
                wp.send_wecom_message("https://example.com", "hello")
        mock_open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
