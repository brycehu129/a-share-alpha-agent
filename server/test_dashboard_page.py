import json
import time
import unittest
from unittest.mock import patch

from urllib.error import URLError

import dashboard_page as dp

SAMPLE = {"exported_at": "2026-09-17T15:58:00+08:00", "agent": {"version": "x"}}


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self, n=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _reset_cache():
    with dp._cache_lock:
        dp._cache["data"] = None
        dp._cache["fetched_at"] = None


class GetDashboardDataTests(unittest.TestCase):
    def setUp(self):
        _reset_cache()

    def test_successful_fetch_populates_cache(self):
        body = json.dumps(SAMPLE).encode("utf-8")
        with patch("dashboard_page.urlopen", return_value=FakeResponse(body)) as mock_open:
            data, fetched_at, is_stale, error = dp.get_dashboard_data()
        self.assertEqual(data, SAMPLE)
        self.assertIsNotNone(fetched_at)
        self.assertFalse(is_stale)
        self.assertIsNone(error)
        mock_open.assert_called_once()

    def test_second_call_within_ttl_uses_cache_not_network(self):
        body = json.dumps(SAMPLE).encode("utf-8")
        with patch("dashboard_page.urlopen", return_value=FakeResponse(body)) as mock_open:
            dp.get_dashboard_data()
            dp.get_dashboard_data()
        mock_open.assert_called_once()

    def test_force_refresh_bypasses_cache(self):
        body = json.dumps(SAMPLE).encode("utf-8")
        with patch("dashboard_page.urlopen", return_value=FakeResponse(body)) as mock_open:
            dp.get_dashboard_data()
            dp.get_dashboard_data(force_refresh=True)
        self.assertEqual(mock_open.call_count, 2)

    def test_fetch_failure_with_no_cache_returns_error(self):
        with patch("dashboard_page.urlopen", side_effect=URLError("boom")):
            data, fetched_at, is_stale, error = dp.get_dashboard_data()
        self.assertIsNone(data)
        self.assertIsNone(fetched_at)
        self.assertFalse(is_stale)
        self.assertIsNotNone(error)

    def test_fetch_failure_after_previous_success_falls_back_to_stale_cache(self):
        body = json.dumps(SAMPLE).encode("utf-8")
        with patch("dashboard_page.urlopen", return_value=FakeResponse(body)):
            dp.get_dashboard_data()
        with patch("dashboard_page.urlopen", side_effect=URLError("boom")):
            data, fetched_at, is_stale, error = dp.get_dashboard_data(force_refresh=True)
        self.assertEqual(data, SAMPLE)
        self.assertTrue(is_stale)
        self.assertIsNotNone(error)

    def test_malformed_json_raises_dashboard_fetch_error(self):
        with patch("dashboard_page.urlopen", return_value=FakeResponse(b"not json")):
            data, fetched_at, is_stale, error = dp.get_dashboard_data()
        self.assertIsNone(data)
        self.assertIn("JSON", error)


class RenderDashboardPageTests(unittest.TestCase):
    def setUp(self):
        _reset_cache()

    def test_successful_render_embeds_data_and_no_raw_placeholders_left(self):
        body = json.dumps(SAMPLE).encode("utf-8")
        with patch("dashboard_page.urlopen", return_value=FakeResponse(body)):
            page = dp.render_dashboard_page()
        self.assertIn('"version": "x"', page)
        self.assertNotIn("__DASHBOARD_DATA_JSON__", page)
        self.assertNotIn("__FETCH_OK__", page)
        self.assertIn("const FETCH_OK = true;", page)

    def test_failed_render_with_no_cache_sets_fetch_ok_false(self):
        with patch("dashboard_page.urlopen", side_effect=URLError("boom")):
            page = dp.render_dashboard_page()
        self.assertIn("const FETCH_OK = false;", page)
        self.assertIn("const DASHBOARD_DATA = null;", page)


if __name__ == "__main__":
    unittest.main()
