import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import claude_client
import openrouter_client as orc

KEY = 'sk-or-v1-testkey1234567890abcdef'
# 在任何 patch 之前留住真正的 sleep：被测代码里的 orc.time 就是全局 time 模块，给它打 sleep 补丁会
# 连假服务器里模拟延迟的 sleep 一起废掉，超时测试就永远不会触发。
REAL_SLEEP = time.sleep
SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False}


def completion(content='{"ok": true}', finish='stop', usage=None, **extra):
    return {'id': 'gen-123', 'model': 'anthropic/claude-opus-5', 'provider': 'Anthropic',
            'choices': [{'index': 0, 'finish_reason': finish, 'message': {'role': 'assistant', 'content': content}}],
            'usage': usage or {'prompt_tokens': 100, 'completion_tokens': 40, 'cost': 0.0012,
                               'completion_tokens_details': {'reasoning_tokens': 25},
                               'prompt_tokens_details': {'cached_tokens': 60}}, **extra}


class FakeOpenRouter:
    """真的 HTTP 服务器：urllib 的请求路径、请求头、请求体都会被实际走一遍。"""

    def __init__(self):
        self.script, self.requests, self.delay = [], [], 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                outer.requests.append({'path': self.path, 'headers': dict(self.headers), 'body': json.loads(body)})
                if outer.delay:
                    REAL_SLEEP(outer.delay)
                status, payload = outer.script.pop(0) if outer.script else (200, completion())
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%d/api/v1' % self.server.server_address[1]
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOpenRouter()
        self.addCleanup(self.fake.close)
        env = patch.dict(os.environ, {'OPENROUTER_API_KEY': KEY, 'OPENROUTER_BASE_URL': self.fake.url,
                                      'LLM_PROVIDER': 'openrouter'}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        # cron 每次先 source /etc/alpha-shadow.env 再跑全量测试：那里按文档会配 OPENROUTER_MODEL / SENTINEL_MODEL，
        # 断言"默认模型"的测试不能被它们影响，否则服务器上一配模型，日线流程就被测试失败连带中止。
        for name in ('OPENROUTER_MODEL', 'SENTINEL_MODEL', 'POSTCLOSE_MODEL', 'NEXTDAY_MODEL',
                 'OPENROUTER_SITE_URL', 'CLAUDE_MODEL'):
            os.environ.pop(name, None)
        sleep = patch.object(orc.time, 'sleep')
        sleep.start()
        self.addCleanup(sleep.stop)

    def call(self, **kw):
        return orc.complete_json('系统提示', '用户内容', SCHEMA, **kw)

    def error(self, **kw):
        with self.assertRaises(claude_client.ClaudeError) as ctx:
            self.call(**kw)
        return ctx.exception


class RequestShapeTests(Base):
    def test_request_matches_the_documented_shape(self):
        self.call(effort='medium', max_tokens=4321)
        req = self.fake.requests[0]
        self.assertEqual(req['path'], '/api/v1/chat/completions')
        self.assertEqual(req['headers']['Authorization'], 'Bearer ' + KEY)
        b = req['body']
        self.assertEqual(b['model'], 'anthropic/claude-opus-5')
        self.assertEqual([m['role'] for m in b['messages']], ['system', 'user'])
        self.assertEqual(b['max_tokens'], 4321)
        self.assertEqual(b['response_format'], {'type': 'json_schema',
                                                'json_schema': {'name': 'result', 'strict': True, 'schema': SCHEMA}})
        self.assertEqual(b['reasoning'], {'effort': 'medium', 'exclude': True})

    def test_only_providers_that_support_structured_output_are_used(self):
        """否则 OpenRouter 可能把请求悄悄转给不支持 json_schema 的端点。"""
        self.call()
        self.assertEqual(self.fake.requests[0]['body']['provider'], {'require_parameters': True})

    def test_model_can_be_overridden_by_argument_and_by_env(self):
        self.call(model='deepseek/deepseek-v4.1-flash')
        self.assertEqual(self.fake.requests[0]['body']['model'], 'deepseek/deepseek-v4.1-flash')
        with patch.dict(os.environ, {'OPENROUTER_MODEL': 'anthropic/claude-sonnet-5'}):
            self.call()
        self.assertEqual(self.fake.requests[1]['body']['model'], 'anthropic/claude-sonnet-5')

    def test_optional_attribution_headers(self):
        with patch.dict(os.environ, {'OPENROUTER_SITE_URL': 'https://example.test'}):
            self.call()
        h = {k.lower(): v for k, v in self.fake.requests[0]['headers'].items()}    # HTTP 头名大小写不敏感
        self.assertEqual(h['http-referer'], 'https://example.test')
        self.assertEqual(h['x-openrouter-title'], 'Alpha Shadow')

    def test_non_ascii_content_survives_the_wire(self):
        orc.complete_json('系统', '贵州茅台 触及止损 ¥1250', SCHEMA)
        self.assertIn('贵州茅台', self.fake.requests[0]['body']['messages'][1]['content'])


class SuccessTests(Base):
    def test_meta_carries_cost_tokens_and_provenance(self):
        data, meta = self.call()
        self.assertEqual(data, {'ok': True})
        self.assertEqual(meta['provider'], 'openrouter')
        self.assertEqual((meta['input_tokens'], meta['output_tokens'], meta['reasoning_tokens']), (100, 40, 25))
        self.assertEqual(meta['cache_read_input_tokens'], 60)
        self.assertEqual(meta['cost'], 0.0012)
        self.assertEqual(meta['upstream_provider'], 'Anthropic')
        self.assertEqual(meta['generation_id'], 'gen-123')
        self.assertFalse(meta['json_repaired'])
        self.assertEqual(meta['attempts'], 1)

    def test_fenced_or_chatty_json_is_tolerated_and_flagged(self):
        """strict 模式各家实现不同：有的只当参考，模型可能包一层 ```json 围栏。"""
        for text in ('```json\n{"ok": true}\n```', '好的，结果如下：\n{"ok": true}\n希望有帮助', '```\n{"ok": true}\n```'):
            self.fake.script = [(200, completion(text))]
            data, meta = self.call()
            self.assertEqual(data, {'ok': True})
            self.assertTrue(meta['json_repaired'], text)

    def test_a_fenced_block_wins_over_stray_braces_in_the_prose_around_it(self):
        """只取"第一个 { 到最后一个 }"会把文字里的花括号也圈进去得到非法 JSON；围栏内的内容才是要的。
        （只测围栏里的简单 JSON 时，围栏分支被删掉测试照样通过——后备分支恰好也能解析。）"""
        text = '结果按 {schema} 给出：\n```json\n{"ok": true}\n```\n注意 {不要} 遗漏。'
        self.fake.script = [(200, completion(text))]
        data, meta = self.call()
        self.assertEqual(data, {'ok': True})
        self.assertTrue(meta['json_repaired'])

    def test_content_blocks_list_is_joined(self):
        self.fake.script = [(200, completion([{'type': 'text', 'text': '{"ok":'}, {'type': 'text', 'text': ' true}'}]))]
        self.assertEqual(self.call()[0], {'ok': True})

    def test_missing_optional_usage_fields_do_not_crash(self):
        self.fake.script = [(200, completion(usage={'prompt_tokens': 5}))]
        _, meta = self.call()
        self.assertIsNone(meta['cost'])
        self.assertIsNone(meta['reasoning_tokens'])


class ErrorClassificationTests(Base):
    def test_http_status_codes_map_to_distinct_statuses(self):
        for code, expected in ((400, 'bad_request'), (401, 'authentication_error'), (402, 'payment_required'),
                               (403, 'permission_denied'), (404, 'model_not_found'), (429, 'rate_limited'),
                               (500, 'server_error'), (503, 'server_error')):
            self.fake.script = [(code, {'error': {'message': '原因%d' % code, 'code': code}})]
            err = self.error(max_retries=0)
            self.assertEqual(err.status, expected, code)
            self.assertIn('原因%d' % code, err.message)

    def test_insufficient_credits_is_distinguishable_from_the_service_being_down(self):
        """402 是"余额不足"，要单独提示——不是服务挂了，重试没用，得去充值。"""
        self.fake.script = [(402, {'error': {'message': 'Insufficient credits', 'code': 402}})]
        self.assertEqual(self.error(max_retries=0).status, 'payment_required')

    def test_http_200_with_an_error_body_is_a_failure_not_a_success(self):
        """OpenRouter 在服务商中途失败时可能返回 200 + error 对象。"""
        self.fake.script = [(200, {'error': {'message': 'Provider returned error', 'code': 502}})]
        err = self.error(max_retries=0)
        self.assertEqual(err.status, 'server_error')
        self.assertIn('Provider returned error', err.message)

    def test_reasoning_that_ate_the_whole_budget_is_called_out(self):
        """推理和可见输出共用 max_tokens。推理吃光预算时返回 length + 空内容，推理 token 仍计费。"""
        self.fake.script = [(200, completion('', finish='length', usage={
            'prompt_tokens': 500, 'completion_tokens': 8000, 'completion_tokens_details': {'reasoning_tokens': 8000}}))]
        err = self.error(max_retries=0)
        self.assertEqual(err.status, 'truncated')
        self.assertIn('推理消耗了几乎全部', err.message)
        self.assertIn('照样计费', err.message)

    def test_plain_truncation_of_visible_output(self):
        self.fake.script = [(200, completion('{"ok": ', finish='length'))]
        err = self.error(max_retries=0)
        self.assertEqual(err.status, 'truncated')
        self.assertNotIn('推理消耗', err.message)

    def test_content_filter_is_reported_as_a_refusal(self):
        self.fake.script = [(200, completion('', finish='content_filter'))]
        self.assertEqual(self.error(max_retries=0).status, 'refusal')

    def test_unparseable_output_and_empty_output(self):
        self.fake.script = [(200, completion('这不是JSON'))]
        self.assertEqual(self.error(max_retries=0).status, 'invalid_json')
        self.fake.script = [(200, completion('   '))]
        self.assertEqual(self.error(max_retries=0).status, 'invalid_json')

    def test_a_non_json_success_body(self):
        self.fake.script = [(200, b'<html>gateway</html>')]
        self.assertEqual(self.error(max_retries=0).status, 'bad_response')

    def test_a_non_json_error_body_still_produces_a_readable_error(self):
        self.fake.script = [(502, b'<html>Bad Gateway</html>')]
        err = self.error(max_retries=0)
        self.assertEqual(err.status, 'server_error')
        self.assertIn('Bad Gateway', err.message)

    def test_timeout_is_a_connection_error(self):
        self.fake.delay = 1.0
        self.assertEqual(self.error(timeout=0.2, max_retries=0).status, 'connection_error')

    def test_unreachable_server_is_a_connection_error(self):
        with patch.dict(os.environ, {'OPENROUTER_BASE_URL': 'http://127.0.0.1:1/api/v1'}):
            self.assertEqual(self.error(max_retries=0).status, 'connection_error')

    def test_missing_key_is_unavailable_not_a_crash(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(orc.available()[0], False)
            with self.assertRaises(claude_client.ClaudeError) as ctx:
                self.call()
            self.assertEqual(ctx.exception.status, 'unavailable')


class RetryTests(Base):
    def test_rate_limit_then_success_is_retried(self):
        self.fake.script = [(429, {'error': {'message': 'slow', 'code': 429}}),
                            (503, {'error': {'message': 'busy', 'code': 503}}), (200, completion())]
        data, meta = self.call(max_retries=2)
        self.assertEqual(data, {'ok': True})
        self.assertEqual((meta['attempts'], len(self.fake.requests)), (3, 3))

    def test_retries_are_bounded(self):
        self.fake.script = [(429, {'error': {'message': 'slow', 'code': 429}})] * 5
        self.assertEqual(self.error(max_retries=1).status, 'rate_limited')
        self.assertEqual(len(self.fake.requests), 2)

    def test_time_limited_callers_can_disable_retries_entirely(self):
        """有 systemd 时限的定时任务（哨兵研判）必须能让"总耗时 ≤ timeout"成立。"""
        self.fake.script = [(429, {'error': {'message': 'slow', 'code': 429}})] * 3
        self.error(max_retries=0)
        self.assertEqual(len(self.fake.requests), 1)

    def test_errors_that_retrying_cannot_fix_are_not_retried(self):
        for code in (400, 401, 402, 404):
            self.fake.requests.clear()
            self.fake.script = [(code, {'error': {'message': 'x', 'code': code}})] * 3
            self.error(max_retries=2)
            self.assertEqual(len(self.fake.requests), 1, code)

    def test_default_retry_count_applies_when_unspecified(self):
        self.fake.script = [(500, {'error': {'message': 'x', 'code': 500}})] * 5
        self.error()
        self.assertEqual(len(self.fake.requests), orc.DEFAULT_RETRIES + 1)


class SecretHygieneTests(Base):
    def test_a_key_echoed_back_in_an_error_is_scrubbed(self):
        self.fake.script = [(401, {'error': {'message': 'Invalid key %s and sk-or-v1-anotherleakedkey99999' % KEY, 'code': 401}})]
        err = self.error(max_retries=0)
        self.assertNotIn(KEY, err.message)
        self.assertNotIn('anotherleakedkey99999', err.message)
        self.assertIn('[REDACTED]', err.message)

    def test_facade_redaction_covers_both_providers(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'sk-ant-abcdefghij123', 'OPENROUTER_API_KEY': KEY}):
            out = claude_client._redact('a sk-ant-abcdefghij123 b %s c sk-or-v1-zzzzzzzzzzzz' % KEY)
        for leak in ('abcdefghij123', KEY, 'zzzzzzzzzzzz'):
            self.assertNotIn(leak, out)


class FacadeSelectionTests(unittest.TestCase):
    def test_explicit_provider_wins(self):
        for chosen in ('openrouter', 'anthropic'):
            with patch.dict(os.environ, {'LLM_PROVIDER': chosen, 'OPENROUTER_API_KEY': KEY, 'ANTHROPIC_API_KEY': 'k'}):
                self.assertEqual(claude_client.provider(), chosen)

    def test_openrouter_is_chosen_automatically_when_only_its_key_is_set(self):
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': KEY}, clear=True):
            self.assertEqual(claude_client.provider(), 'openrouter')

    def test_falls_back_to_anthropic_when_openrouter_is_not_configured(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'k'}, clear=True):
            self.assertEqual(claude_client.provider(), 'anthropic')

    def test_junk_provider_value_is_ignored_not_trusted(self):
        with patch.dict(os.environ, {'LLM_PROVIDER': 'gpt', 'OPENROUTER_API_KEY': KEY}, clear=True):
            self.assertEqual(claude_client.provider(), 'openrouter')

    def test_available_reflects_the_selected_provider(self):
        with patch.dict(os.environ, {'LLM_PROVIDER': 'openrouter'}, clear=True):
            ok, why = claude_client.available()
            self.assertFalse(ok)
            self.assertIn('OPENROUTER_API_KEY', why)
        with patch.dict(os.environ, {'LLM_PROVIDER': 'openrouter', 'OPENROUTER_API_KEY': KEY}, clear=True):
            self.assertTrue(claude_client.available()[0])

    def test_no_key_at_all_mentions_both_options(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIn('OPENROUTER_API_KEY', claude_client.available()[1])

    def test_openrouter_needs_no_third_party_package(self):
        """选 OpenRouter 时不该要求装 anthropic 包。"""
        import builtins
        real = builtins.__import__

        def no_anthropic(name, *a, **k):
            if name == 'anthropic':
                raise ImportError('blocked')
            return real(name, *a, **k)
        with patch.dict(os.environ, {'LLM_PROVIDER': 'openrouter', 'OPENROUTER_API_KEY': KEY}, clear=True):
            with patch('builtins.__import__', side_effect=no_anthropic):
                self.assertTrue(claude_client.available()[0])


class FacadeRoutingTests(Base):
    def test_complete_json_goes_through_the_facade_to_openrouter(self):
        data, meta = claude_client.complete_json('s', 'u', SCHEMA, effort='low', max_tokens=999, timeout=30, max_retries=0)
        self.assertEqual((data, meta['provider']), ({'ok': True}, 'openrouter'))
        self.assertEqual(self.fake.requests[0]['body']['reasoning']['effort'], 'low')
        self.assertEqual(self.fake.requests[0]['body']['max_tokens'], 999)

    def test_the_facades_default_effort_env_is_honoured(self):
        with patch.object(claude_client, 'EFFORT', 'xhigh'):
            claude_client.complete_json('s', 'u', SCHEMA)
        self.assertEqual(self.fake.requests[0]['body']['reasoning']['effort'], 'xhigh')

    def test_the_analysts_work_unchanged_on_top_of_openrouter(self):
        import scenario_analyst
        stock = {'symbol': 'sz000001', 'current_read': '现价9.4', 'scenarios': [], 'watch_metrics': [],
                 'action_hint': 'wait', 'confidence': 3, 'caveats': []}
        self.fake.script = [(200, completion(json.dumps({'stocks': [stock], 'data_caveats': []}, ensure_ascii=False)))]
        data, meta = scenario_analyst.analyze({'stocks': [{'symbol': 'sz000001'}], 'market': {}})
        self.assertEqual(meta['status'], 'ok')
        self.assertEqual(meta['provider'], 'openrouter')
        b = self.fake.requests[0]['body']
        self.assertEqual(b['reasoning']['effort'], 'medium')
        self.assertEqual(b['max_tokens'], scenario_analyst.MAX_TOKENS)

    def test_analyst_failure_still_degrades_gracefully_on_openrouter(self):
        import scenario_analyst
        self.fake.script = [(402, {'error': {'message': 'Insufficient credits', 'code': 402}})]
        data, meta = scenario_analyst.analyze({'stocks': [{'symbol': 'sz000001'}], 'market': {}})
        self.assertIsNone(data)
        self.assertEqual(meta['status'], 'payment_required')


class CheckCommandTests(Base):
    def run_check(self, **kw):
        return claude_client.check(**kw)

    def test_successful_check_reports_model_tokens_and_cost(self):
        self.fake.script = [(200, completion('{"ok": true, "echo": "pong"}'))]
        ok, lines = self.run_check()
        text = '\n'.join(lines)
        self.assertTrue(ok)
        for needle in ('后端：openrouter', 'anthropic/claude-opus-5', '成功', 'token：输入 100 / 输出 40', '推理 25', '0.0012'):
            self.assertIn(needle, text)
        self.assertEqual(self.fake.requests[0]['body']['reasoning']['effort'], 'low')
        self.assertLessEqual(self.fake.requests[0]['body']['max_tokens'], 2000)      # 自检不该烧钱

    def test_check_names_the_reason_when_the_key_has_no_credit(self):
        self.fake.script = [(402, {'error': {'message': 'Insufficient credits', 'code': 402}})]
        ok, lines = self.run_check()
        self.assertFalse(ok)
        self.assertIn('payment_required', '\n'.join(lines))
        self.assertEqual(len(self.fake.requests), 1)                                   # 自检不重试

    def test_check_warns_when_the_model_needs_json_repair(self):
        """strict 模式只当参考的服务商：结构化输出不够可靠，上线前就该知道。"""
        self.fake.script = [(200, completion('```json\n{"ok": true, "echo": "pong"}\n```'))]
        ok, lines = self.run_check()
        self.assertTrue(ok)
        self.assertIn('不够可靠', '\n'.join(lines))

    def test_check_without_a_key_sends_nothing(self):
        with patch.dict(os.environ, {}, clear=True):
            ok, lines = claude_client.check()
        self.assertFalse(ok)
        self.assertEqual(self.fake.requests, [])

    def test_cli_exit_code_reflects_the_result(self):
        self.fake.script = [(200, completion('{"ok": true, "echo": "pong"}'))]
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(claude_client.main(['check']), 0)
            self.fake.script = [(401, {'error': {'message': 'bad key', 'code': 401}})]
            self.assertEqual(claude_client.main(['check']), 1)

    def test_model_override_flag(self):
        self.fake.script = [(200, completion('{"ok": true, "echo": "pong"}'))]
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            claude_client.main(['check', '--model', 'deepseek/deepseek-v4.1-flash'])
        self.assertEqual(self.fake.requests[0]['body']['model'], 'deepseek/deepseek-v4.1-flash')


class SentinelModelTests(Base):
    def test_each_scene_uses_its_model_and_falls_back_to_the_shared_default(self):
        import ai_analyst
        import next_day_watch
        import scenario_analyst
        scene_env = {'OPENROUTER_MODEL': 'm/default', 'POSTCLOSE_MODEL': 'm/report',
                     'SENTINEL_MODEL': 'deepseek/deepseek-v3.2', 'NEXTDAY_MODEL': 'm/watch'}
        with patch.object(ai_analyst, 'build_payload', return_value={'stocks': []}), \
                patch.object(next_day_watch, 'build_payload', return_value={}), \
                patch.object(next_day_watch, 'apply_ai', return_value=[]):
            for overrides, expected in ((scene_env, ['m/report', 'deepseek/deepseek-v3.2', 'm/watch']),
                                        ({name: '' for name in scene_env if name != 'OPENROUTER_MODEL'},
                                         ['m/default'] * 3)):
                with patch.dict(os.environ, {**scene_env, **overrides}):
                    ai_analyst.analyze({'rows': []}, {}, [], [])
                    scenario_analyst.analyze({'stocks': []})
                    next_day_watch.analyze({'items': [{'symbol': 'sz000001'}]})
                self.assertEqual([req['body']['model'] for req in self.fake.requests[-3:]], expected)
                for req in self.fake.requests[-3:]:
                    self.assertEqual(req['body']['response_format']['type'], 'json_schema')
                    self.assertTrue(req['body']['provider']['require_parameters'])

    def test_sentinel_can_use_a_cheaper_model_than_the_postclose_report(self):
        import scenario_analyst
        stock = {'symbol': 'sz000001', 'current_read': 'x', 'scenarios': [], 'watch_metrics': [],
                 'action_hint': 'wait', 'confidence': 3, 'caveats': []}
        body = json.dumps({'stocks': [stock], 'data_caveats': []})
        with patch.dict(os.environ, {'SENTINEL_MODEL': 'anthropic/claude-sonnet-5'}):
            self.fake.script = [(200, completion(body))]
            scenario_analyst.analyze({'stocks': [{'symbol': 'sz000001'}], 'market': {}})
        self.assertEqual(self.fake.requests[0]['body']['model'], 'anthropic/claude-sonnet-5')
        self.fake.script = [(200, completion(body))]
        scenario_analyst.analyze({'stocks': [{'symbol': 'sz000001'}], 'market': {}})
        self.assertEqual(self.fake.requests[1]['body']['model'], 'anthropic/claude-opus-5')     # 未设置时用默认


class RunAsScriptTests(Base):
    """回归：`python3 server/claude_client.py check` 时该文件是 __main__，早先 openrouter_client 里
    `from claude_client import ClaudeError` 会再导入一份同名模块，抛出的异常类和 check() 要捕获的是两个
    不同的类，except 抓不到——用户看到一屏 traceback 而不是"key 无效"。测试全是以 import 方式跑的，测不到。"""

    def run_script(self, script_args, env_extra):
        import subprocess, sys
        env = {**os.environ, **env_extra}
        return subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), 'claude_client.py')] + script_args,
                              capture_output=True, text=True, env=env, timeout=30)

    def test_auth_failure_is_a_readable_message_not_a_traceback(self):
        self.fake.script = [(401, {'error': {'message': 'User not found.', 'code': 401}})]
        r = self.run_script(['check'], {'OPENROUTER_API_KEY': KEY, 'OPENROUTER_BASE_URL': self.fake.url, 'LLM_PROVIDER': 'openrouter'})
        self.assertEqual(r.returncode, 1)
        self.assertNotIn('Traceback', r.stderr)
        self.assertIn('authentication_error', r.stdout)
        self.assertIn('User not found', r.stdout)

    def test_success_when_run_as_a_script(self):
        self.fake.script = [(200, completion('{"ok": true, "echo": "pong"}'))]
        r = self.run_script(['check'], {'OPENROUTER_API_KEY': KEY, 'OPENROUTER_BASE_URL': self.fake.url, 'LLM_PROVIDER': 'openrouter'})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('成功', r.stdout)

    def test_the_error_class_is_a_single_shared_class(self):
        import llm_errors
        self.assertIs(claude_client.ClaudeError, llm_errors.ClaudeError)
        self.assertIs(orc.ClaudeError, llm_errors.ClaudeError)


if __name__ == '__main__':
    unittest.main()
