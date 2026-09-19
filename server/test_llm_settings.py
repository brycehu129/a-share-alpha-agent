import base64
import http.client
import json
import os
import stat
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import urlencode

import claude_client
import llm_settings
import postclose_report
import sentinel
import webapp
from test_openrouter_client import FakeOpenRouter

KEY = 'sk-or-v1-pagekey1234567890abcdef'
OTHER = 'sk-or-v1-envkey1234567890abcdefgh'
PASSWORD = 'pw-for-tests'
AUTH = 'Basic ' + base64.b64encode(('x:' + PASSWORD).encode()).decode()


class Hermetic(unittest.TestCase):
    """环境和进程内状态都隔离：apply() 会改 os.environ 和 _ORIGINALS，测完必须还原。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        env = patch.dict(os.environ, {'PRIVATE_DATA_DIR': self.dir, 'ADMIN_PASSWORD': PASSWORD,
                                      'CONFIG_PATH': os.path.join(self.dir, 'config.json')})
        env.start()
        self.addCleanup(env.stop)
        for name in ('OPENROUTER_API_KEY', 'OPENROUTER_MODEL', 'SENTINEL_MODEL', 'LLM_PROVIDER',
                     'ANTHROPIC_API_KEY', 'OPENROUTER_BASE_URL'):
            os.environ.pop(name, None)
        saved = dict(llm_settings._ORIGINALS)
        llm_settings._ORIGINALS.clear()
        self.addCleanup(lambda: (llm_settings._ORIGINALS.clear(), llm_settings._ORIGINALS.update(saved)))


class ValidationTests(Hermetic):
    def test_rejects_wrong_prefix_short_and_whitespace(self):
        for bad in ('', '   ', 'sk-ant-abcdefghijklmnopqrstuvwx', 'sk-or-short',
                    KEY[:12] + ' ' + KEY[12:], KEY + '\nX-Evil: 1', KEY[:20] + '\r\n' + KEY[20:], KEY + '中文'):
            with self.assertRaises(llm_settings.SettingsError, msg=repr(bad)):
                llm_settings.validate_key(bad)

    def test_surrounding_whitespace_from_copy_paste_is_trimmed(self):
        self.assertEqual(llm_settings.validate_key('  %s\n' % KEY), KEY)

    def test_error_message_never_echoes_the_submitted_value(self):
        bad = 'sk-or-v1-secret-but-has space-in-it'
        with self.assertRaises(llm_settings.SettingsError) as ctx:
            llm_settings.validate_key(bad)
        self.assertNotIn('secret', str(ctx.exception))

    def test_model_names(self):
        self.assertEqual(llm_settings.validate_model(' anthropic/claude-sonnet-5 '), 'anthropic/claude-sonnet-5')
        self.assertEqual(llm_settings.validate_model(''), '')
        for bad in ('a b', 'x;rm -rf', 'model\nnext', '中文', '/leading', 'a' * 200):
            with self.assertRaises(llm_settings.SettingsError, msg=bad):
                llm_settings.validate_model(bad)


class StorageTests(Hermetic):
    def test_roundtrip_and_permissions(self):
        llm_settings.save_key(KEY)
        self.assertEqual(llm_settings.load()[0], {'api_key': KEY})
        mode = stat.S_IMODE(os.stat(llm_settings.path()).st_mode)
        self.assertEqual(mode, 0o600)
        # 不留临时文件
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith('.tmp')], [])

    def test_saving_models_keeps_key_and_saving_key_keeps_models(self):
        llm_settings.save_key(KEY)
        llm_settings.save_models('anthropic/claude-opus-5', 'anthropic/claude-sonnet-5')
        llm_settings.save_key(OTHER)
        self.assertEqual(llm_settings.load()[0], {'api_key': OTHER, 'model': 'anthropic/claude-opus-5',
                                                  'sentinel_model': 'anthropic/claude-sonnet-5'})

    def test_blank_model_clears_it_and_leaves_key(self):
        llm_settings.save_key(KEY)
        llm_settings.save_models('anthropic/claude-opus-5', '')
        llm_settings.save_models('', '')
        self.assertEqual(llm_settings.load()[0], {'api_key': KEY})

    def test_clear_key_keeps_models(self):
        llm_settings.save_key(KEY)
        llm_settings.save_models('m/one', '')
        llm_settings.clear_key()
        self.assertEqual(llm_settings.load()[0], {'model': 'm/one'})

    def test_corrupt_file_is_reported_not_silently_treated_as_absent(self):
        with open(llm_settings.path(), 'w') as f:
            f.write('{not json')
        settings, problem = llm_settings.load()
        self.assertEqual(settings, {})
        self.assertIn('无法读取', problem)

    def test_hand_edited_garbage_fields_are_dropped_not_exported_to_env(self):
        with open(llm_settings.path(), 'w') as f:
            json.dump({'api_key': 'sk-or-x\nInjected: 1', 'model': 'bad model', 'sentinel_model': 5}, f)
        self.assertEqual(llm_settings.load()[0], {})

    def test_mask_shows_only_the_tail(self):
        masked = llm_settings.mask_key(KEY)
        self.assertTrue(masked.endswith(KEY[-4:]))
        self.assertNotIn(KEY[:-4], masked)


class ApplyTests(Hermetic):
    def apply(self, env, memo):
        return llm_settings.apply(self.dir, environ=env, memo=memo)

    def test_page_value_overrides_environment(self):
        llm_settings.save_key(KEY)
        env = {'OPENROUTER_API_KEY': OTHER}
        self.assertEqual(self.apply(env, {}), ['OPENROUTER_API_KEY'])
        self.assertEqual(env['OPENROUTER_API_KEY'], KEY)

    def test_clearing_restores_the_original_environment_value(self):
        llm_settings.save_key(KEY)
        env, memo = {'OPENROUTER_API_KEY': OTHER}, {}
        self.apply(env, memo)
        llm_settings.clear_key(self.dir)
        self.apply(env, memo)
        self.assertEqual(env['OPENROUTER_API_KEY'], OTHER)

    def test_clearing_removes_a_key_that_was_never_in_the_environment(self):
        llm_settings.save_key(KEY)
        env, memo = {}, {}
        self.apply(env, memo)
        llm_settings.clear_key(self.dir)
        self.apply(env, memo)
        self.assertNotIn('OPENROUTER_API_KEY', env)

    def test_never_touches_variables_it_did_not_override(self):
        """没有页面保存值时，进程里别处设置的同名环境变量不能被 apply() 删掉或改掉。"""
        env, memo = {'OPENROUTER_API_KEY': OTHER}, {}
        self.assertEqual(self.apply(env, memo), [])
        self.assertEqual(env, {'OPENROUTER_API_KEY': OTHER})
        memo2 = {}                                  # 另一个进程/调用：环境在两次 apply 之间被别人改了
        self.apply(env, memo2)
        env['OPENROUTER_API_KEY'] = 'sk-or-v1-changed-by-someone-else-123456'
        self.apply(env, memo2)
        self.assertEqual(env['OPENROUTER_API_KEY'], 'sk-or-v1-changed-by-someone-else-123456')

    def test_a_value_someone_else_set_after_our_override_is_not_restored_over(self):
        """我们覆盖过之后，环境变量被别处改成了别的值：清除页面值时不能拿"原值"把它盖回去。"""
        llm_settings.save_key(KEY)
        env, memo = {'OPENROUTER_API_KEY': OTHER}, {}
        self.apply(env, memo)
        env['OPENROUTER_API_KEY'] = 'sk-or-v1-set-later-by-someone-123456'
        llm_settings.clear_key(self.dir)
        self.apply(env, memo)
        self.assertEqual(env['OPENROUTER_API_KEY'], 'sk-or-v1-set-later-by-someone-123456')

    def test_idempotent_and_leaves_unrelated_env_alone(self):
        llm_settings.save_key(KEY)
        env, memo = {'PATH': '/bin', 'OPENROUTER_MODEL': 'env/model'}, {}
        self.apply(env, memo)
        self.apply(env, memo)
        self.assertEqual(env, {'PATH': '/bin', 'OPENROUTER_MODEL': 'env/model', 'OPENROUTER_API_KEY': KEY})

    def test_describe_reports_source_and_shadowing_without_the_full_key(self):
        llm_settings.save_key(KEY)
        info = llm_settings.describe(self.dir, environ={'OPENROUTER_API_KEY': OTHER})
        self.assertEqual(info['api_key']['source'], 'page')
        self.assertTrue(info['api_key']['env_shadowed'])
        self.assertNotIn(KEY, json.dumps(info))
        self.assertNotIn(OTHER, json.dumps(info))
        self.assertEqual(llm_settings.describe(self.dir, environ={})['model']['source'], 'none')

    def test_describe_env_only(self):
        info = llm_settings.describe(self.dir, environ={'OPENROUTER_API_KEY': OTHER})
        self.assertEqual((info['api_key']['source'], info['api_key']['env_shadowed']), ('env', False))


class LibraryStaysHermeticTests(Hermetic):
    def test_library_code_never_reads_the_stored_file(self):
        """cron 每次先跑全量测试。库若自己读文件，服务器一存 key，'没配 key' 的测试全会变样。"""
        llm_settings.save_key(KEY)
        ok, why = claude_client.available()
        self.assertFalse(ok)
        self.assertNotIn('OPENROUTER_API_KEY', os.environ)

    def test_stored_key_is_redacted_from_error_text_once_applied(self):
        llm_settings.save_key(KEY)
        llm_settings.apply()
        self.assertNotIn(KEY, claude_client._redact('boom ' + KEY))


class EntryPointTests(Hermetic):
    def setUp(self):
        super().setUp()
        llm_settings.save_key(KEY)
        llm_settings.save_models('m/main', 'm/sentinel')

    def test_claude_client_check_uses_the_stored_key(self):
        fake = FakeOpenRouter()
        self.addCleanup(fake.close)
        os.environ['OPENROUTER_BASE_URL'] = fake.url
        with patch('claude_client.print', create=True):
            claude_client.main(['check'])
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(fake.requests[0]['headers']['Authorization'], 'Bearer ' + KEY)
        self.assertEqual(fake.requests[0]['body']['model'], 'm/main')

    def test_sentinel_analyze_sees_the_stored_settings(self):
        seen = {}

        def fake_analyze(*a, **k):
            seen.update(key=os.environ.get('OPENROUTER_API_KEY'), sm=os.environ.get('SENTINEL_MODEL'))
            return {}
        with patch.object(sentinel, 'analyze_pending', fake_analyze), \
                patch('sys.argv', ['sentinel.py', 'analyze']), patch('builtins.print'):
            sentinel.main()
        self.assertEqual(seen, {'key': KEY, 'sm': 'm/sentinel'})

    def test_postclose_sees_the_stored_key(self):
        seen = {}

        def fake_build(history, run_id):
            seen['key'] = os.environ.get('OPENROUTER_API_KEY')
            return {'status': 'empty', 'issues': []}
        with patch.object(postclose_report, 'build', fake_build), \
                patch('sys.argv', ['postclose_report.py', '--history', self.dir, '--dry-run']):
            postclose_report.main()
        self.assertEqual(seen['key'], KEY)

    def test_no_ai_really_disables_openrouter_even_with_a_stored_key(self):
        """--no-ai 以前只去掉 Anthropic 的 key；配了 OpenRouter 就会照样调用并花钱。"""
        seen = {}

        def fake_build(history, run_id):
            seen['ok'] = claude_client.available()[0]
            seen['key'] = os.environ.get('OPENROUTER_API_KEY')
            return {'status': 'empty', 'issues': []}
        os.environ['OPENROUTER_API_KEY'] = OTHER
        with patch.object(postclose_report, 'build', fake_build), \
                patch('sys.argv', ['postclose_report.py', '--history', self.dir, '--no-ai', '--dry-run']):
            postclose_report.main()
        self.assertEqual(seen, {'ok': False, 'key': None})


class WebServer(Hermetic):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), webapp.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def request(self, method, path, form=None, headers=None, auth=True):
        h = {'Host': '127.0.0.1:%d' % self.port}
        if auth:
            h['Authorization'] = AUTH
        h.update(headers or {})
        body = urlencode(form or {}).encode() if method == 'POST' else None
        if body is not None:
            h['Content-Type'] = 'application/x-www-form-urlencoded'
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        text = resp.read().decode('utf-8')
        conn.close()
        return resp.status, text, resp


class LlmRouteTests(WebServer):
    def test_all_llm_routes_require_login(self):
        for path in ('/llm/key', '/llm/models', '/llm/check', '/llm/clear'):
            status, _, _ = self.request('POST', path, {'api_key': KEY}, auth=False)
            self.assertEqual(status, 401, path)
        self.assertEqual(llm_settings.load()[0], {})

    def test_save_key_then_page_shows_only_the_mask(self):
        status, page, _ = self.request('POST', '/llm/key', {'api_key': KEY})
        self.assertEqual(status, 200)
        self.assertIn('已保存', page)
        self.assertIn(llm_settings.mask_key(KEY), page)
        self.assertNotIn(KEY, page)
        self.assertNotIn(KEY[8:], page)
        _, index, resp = self.request('GET', '/')
        self.assertNotIn(KEY, index)
        self.assertIn(llm_settings.mask_key(KEY), index)
        self.assertEqual(resp.getheader('Cache-Control'), 'no-store')
        self.assertEqual(llm_settings.load()[0]['api_key'], KEY)

    def test_the_key_input_is_a_password_field_and_never_prefilled(self):
        llm_settings.save_key(KEY)
        _, page, _ = self.request('GET', '/')
        self.assertIn('type="password" name="api_key"', page)
        self.assertNotIn('name="api_key" value', page)

    def test_invalid_key_is_rejected_without_echo_and_without_saving(self):
        bad = 'sk-or-v1-leaky value with spaces'
        status, page, _ = self.request('POST', '/llm/key', {'api_key': bad})
        self.assertEqual(status, 200)
        self.assertIn('格式不对', page)
        self.assertNotIn('leaky', page)
        self.assertEqual(llm_settings.load()[0], {})

    def test_models_form_and_clear(self):
        self.request('POST', '/llm/key', {'api_key': KEY})
        _, page, _ = self.request('POST', '/llm/models', {'model': 'a/b', 'sentinel_model': 'c/d'})
        self.assertIn('模型已保存', page)
        self.assertEqual(llm_settings.load()[0], {'api_key': KEY, 'model': 'a/b', 'sentinel_model': 'c/d'})
        _, page, _ = self.request('POST', '/llm/clear')
        self.assertIn('已清除', page)
        self.assertEqual(llm_settings.load()[0], {'model': 'a/b', 'sentinel_model': 'c/d'})
        self.assertNotIn('OPENROUTER_API_KEY', os.environ)     # 清除后立即生效，不是等重启

    def test_page_key_takes_effect_in_the_running_server_and_clearing_falls_back_to_env(self):
        os.environ['OPENROUTER_API_KEY'] = OTHER
        _, page, _ = self.request('POST', '/llm/key', {'api_key': KEY})
        self.assertIn('已被页面保存的覆盖', page)
        self.assertEqual(os.environ['OPENROUTER_API_KEY'], KEY)
        self.request('POST', '/llm/clear')
        self.assertEqual(os.environ['OPENROUTER_API_KEY'], OTHER)

    def test_anthropic_provider_override_is_called_out(self):
        os.environ['LLM_PROVIDER'] = 'anthropic'
        _, page, _ = self.request('GET', '/')
        self.assertIn('不会被用到', page)

    def test_check_button_sends_one_request_with_the_stored_key(self):
        fake = FakeOpenRouter()
        self.addCleanup(fake.close)
        os.environ['OPENROUTER_BASE_URL'] = fake.url
        self.request('POST', '/llm/key', {'api_key': KEY})
        status, page, _ = self.request('POST', '/llm/check')
        self.assertEqual(status, 200)
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(fake.requests[0]['headers']['Authorization'], 'Bearer ' + KEY)
        self.assertIn('成功', page)
        self.assertNotIn(KEY, page)

    def test_check_failure_is_shown_and_redacted(self):
        fake = FakeOpenRouter()
        self.addCleanup(fake.close)
        fake.script = [(401, {'error': {'message': 'bad key ' + KEY, 'code': 401}})]
        os.environ['OPENROUTER_BASE_URL'] = fake.url
        self.request('POST', '/llm/key', {'api_key': KEY})
        _, page, _ = self.request('POST', '/llm/check')
        self.assertIn('authentication_error', page)
        self.assertNotIn(KEY, page)

    def test_check_without_a_key_makes_no_request(self):
        fake = FakeOpenRouter()
        self.addCleanup(fake.close)
        os.environ['OPENROUTER_BASE_URL'] = fake.url
        _, page, _ = self.request('POST', '/llm/check')
        self.assertEqual(fake.requests, [])
        self.assertIn('不可用', page)

    def test_only_one_check_runs_at_a_time(self):
        self.assertTrue(webapp._llm_check_lock.acquire(blocking=False))
        try:
            ok, lines = webapp.run_llm_check()
        finally:
            webapp._llm_check_lock.release()
        self.assertFalse(ok)
        self.assertIn('正在进行', lines[0])

    def test_unknown_llm_route_is_404(self):
        self.assertEqual(self.request('POST', '/llm/nope')[0], 404)


class CsrfTests(WebServer):
    """有了能改 key 的页面，跨站伪造就从“烦人”变成“持仓数据外流”。"""

    def host(self):
        return '127.0.0.1:%d' % self.port

    def test_cross_site_post_is_blocked_and_changes_nothing(self):
        for headers in ({'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'},
                        {'Origin': 'https://evil.example'}, {'Origin': 'null'},
                        {'Origin': 'http://127.0.0.1:1'}):
            status, page, _ = self.request('POST', '/llm/key', {'api_key': KEY}, headers=headers)
            self.assertEqual(status, 403, headers)
        self.assertEqual(llm_settings.load()[0], {})

    def test_other_state_changing_routes_are_protected_too(self):
        evil = {'Origin': 'https://evil.example'}
        self.assertEqual(self.request('POST', '/config', {'webhook_url': 'x'}, headers=evil)[0], 403)
        self.assertEqual(self.request('POST', '/book/holding', {'symbol': '600000'}, headers=evil)[0], 403)
        self.assertEqual(self.request('POST', '/postclose/run', {}, headers=evil)[0], 403)
        self.assertEqual(self.request('POST', '/test-push', {}, headers=evil)[0], 403)

    def test_same_origin_browser_and_plain_clients_are_allowed(self):
        for headers in ({'Sec-Fetch-Site': 'same-origin'}, {'Origin': 'http://' + self.host()}, {}):
            self.assertEqual(self.request('POST', '/llm/key', {'api_key': KEY}, headers=headers)[0], 200, headers)

    def test_csrf_check_comes_after_login(self):
        status, _, _ = self.request('POST', '/llm/key', {'api_key': KEY}, auth=False,
                                    headers={'Origin': 'https://evil.example'})
        self.assertEqual(status, 401)

    def test_oversized_or_malformed_body_is_refused(self):
        status, _, _ = self.request('POST', '/llm/key', {'api_key': 'x' * 200_000})
        self.assertEqual(status, 413)
        self.assertEqual(llm_settings.load()[0], {})


if __name__ == '__main__':
    unittest.main()
