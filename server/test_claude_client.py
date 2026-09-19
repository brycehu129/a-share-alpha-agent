import os
import unittest
from unittest.mock import patch

import claude_client


class AvailabilityTests(unittest.TestCase):
    def test_missing_key_is_reported_not_raised(self):
        with patch.dict(os.environ, {}, clear=True):
            ok, reason = claude_client.available()
            self.assertFalse(ok)
            self.assertIn('ANTHROPIC_API_KEY', reason)

    def test_missing_package_is_reported_not_raised(self):
        """anthropic 没装时必须是可处理的返回值，不能是导入期异常——
        cron 每次跑前都要执行整个测试套件，导入炸了会连带把日线流程弄挂。"""
        real_import = __builtins__['__import__'] if isinstance(__builtins__, dict) else __import__

        def fake_import(name, *args, **kwargs):
            if name == 'anthropic':
                raise ImportError('No module named anthropic')
            return real_import(name, *args, **kwargs)

        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'sk-ant-test'}):
            with patch('builtins.__import__', side_effect=fake_import):
                ok, reason = claude_client.available()
        self.assertFalse(ok)
        self.assertIn('anthropic', reason)

    def test_complete_json_raises_typed_error_when_unavailable(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(claude_client.ClaudeError) as ctx:
                claude_client.complete_json('sys', 'user', {})
        self.assertEqual(ctx.exception.status, 'unavailable')


class RedactionTests(unittest.TestCase):
    def test_key_shaped_strings_are_scrubbed(self):
        self.assertNotIn('sk-ant-abcdefgh12345',
                         claude_client._redact('bad key sk-ant-abcdefgh12345 rejected'))

    def test_configured_key_is_scrubbed_even_if_oddly_shaped(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'totally-custom-token-value'}):
            scrubbed = claude_client._redact('server echoed totally-custom-token-value back')
        self.assertNotIn('totally-custom-token-value', scrubbed)
        self.assertIn('[REDACTED]', scrubbed)

    def test_output_is_bounded(self):
        self.assertLessEqual(len(claude_client._redact('x' * 5000)), 800)



class FakeUsage:
    input_tokens = 1234
    output_tokens = 567
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 100


class FakeBlock:
    def __init__(self, text):
        self.type = 'text'
        self.text = text


class FakeMessage:
    def __init__(self, text, stop_reason='end_turn'):
        self.content = [FakeBlock(text)]
        self.stop_reason = stop_reason
        self.stop_details = None
        self.model = 'claude-opus-5'
        self.usage = FakeUsage()


class FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self._message


class FakeAnthropicModule:
    """替身 SDK：只为验证本模块自己的调用与解析逻辑，不触网。

    异常类必须是真的类层次，否则 complete_json 里的 except 链匹配不上。
    """
    class APIError(Exception):
        pass

    class APIStatusError(APIError):
        def __init__(self, message='', status_code=500):
            super().__init__(message)
            self.status_code = status_code

    class NotFoundError(APIStatusError):
        pass

    class AuthenticationError(APIStatusError):
        pass

    class PermissionDeniedError(APIStatusError):
        pass

    class RateLimitError(APIStatusError):
        pass

    class APIConnectionError(APIError):
        pass

    def __init__(self, message=None, raises=None):
        self.captured = {}
        self._message = message
        self._raises = raises
        module = self

        class Messages:
            def stream(self, **kwargs):
                module.captured = kwargs
                if module._raises:
                    raise module._raises
                return FakeStream(module._message)

        class Anthropic:
            def __init__(self, **kwargs):
                self.init_kwargs = kwargs
                self.messages = Messages()

        self.Anthropic = Anthropic


def with_fake(module):
    import sys
    return patch.dict(sys.modules, {'anthropic': module})


class CompleteJsonTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'sk-ant-test'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_request_shape(self):
        fake = FakeAnthropicModule(FakeMessage('{"ok": true}'))
        with with_fake(fake):
            data, meta = claude_client.complete_json('规则口径', '今日数据', {'type': 'object'})
        self.assertEqual(data, {'ok': True})
        self.assertEqual(meta['input_tokens'], 1234)
        self.assertEqual(meta['model'], 'claude-opus-5')

        sent = fake.captured
        self.assertEqual(sent['model'], claude_client.MODEL)
        self.assertEqual(sent['thinking'], {'type': 'adaptive'})
        # 结构化输出：schema 必须真的传下去，否则拿回来的是自由文本。
        self.assertEqual(sent['output_config']['format'],
                         {'type': 'json_schema', 'schema': {'type': 'object'}})
        self.assertEqual(sent['output_config']['effort'], claude_client.EFFORT)
        # 稳定的策略口径打上缓存标记，易变的当日数据留在 user 消息里。
        self.assertEqual(sent['system'][0]['cache_control'], {'type': 'ephemeral'})
        self.assertEqual(sent['messages'], [{'role': 'user', 'content': '今日数据'}])

    def test_bounded_callers_can_disable_sdk_retries(self):
        """SDK 默认 2 次重试：超时 600 秒 × 3 次，远超任何有 systemd 时限的定时任务。"""
        fake = FakeAnthropicModule(FakeMessage('{"ok": true}'))
        seen = {}
        orig = fake.Anthropic

        class Spy(orig):
            def __init__(self, **kw):
                seen.update(kw)
                super().__init__(**kw)
        fake.Anthropic = Spy
        with with_fake(fake):
            claude_client.complete_json('s', 'u', {}, timeout=100, max_retries=0)
        self.assertEqual((seen['timeout'], seen['max_retries']), (100, 0))
        seen.clear()
        with with_fake(fake):
            claude_client.complete_json('s', 'u', {})
        self.assertNotIn('max_retries', seen)                 # 不指定就沿用 SDK 默认

    def test_truncated_output_is_rejected_not_half_parsed(self):
        fake = FakeAnthropicModule(FakeMessage('{"partial": ', stop_reason='max_tokens'))
        with with_fake(fake):
            with self.assertRaises(claude_client.ClaudeError) as ctx:
                claude_client.complete_json('s', 'u', {})
        self.assertEqual(ctx.exception.status, 'truncated')

    def test_refusal_is_surfaced(self):
        fake = FakeAnthropicModule(FakeMessage('', stop_reason='refusal'))
        with with_fake(fake):
            with self.assertRaises(claude_client.ClaudeError) as ctx:
                claude_client.complete_json('s', 'u', {})
        self.assertEqual(ctx.exception.status, 'refusal')

    def test_invalid_json_is_rejected(self):
        fake = FakeAnthropicModule(FakeMessage('这不是JSON'))
        with with_fake(fake):
            with self.assertRaises(claude_client.ClaudeError) as ctx:
                claude_client.complete_json('s', 'u', {})
        self.assertEqual(ctx.exception.status, 'invalid_json')

    def test_api_errors_map_to_distinct_statuses(self):
        cases = [
            (FakeAnthropicModule.RateLimitError('slow down', 429), 'rate_limited'),
            (FakeAnthropicModule.AuthenticationError('bad key', 401), 'authentication_error'),
            (FakeAnthropicModule.NotFoundError('no model', 404), 'model_not_found'),
            (FakeAnthropicModule.APIStatusError('boom', 500), 'server_error'),
            (FakeAnthropicModule.APIStatusError('bad', 400), 'bad_request'),
            (FakeAnthropicModule.APIConnectionError('down'), 'connection_error'),
        ]
        for exc, expected in cases:
            fake = FakeAnthropicModule(raises=exc)
            with with_fake(fake):
                with self.assertRaises(claude_client.ClaudeError) as ctx:
                    claude_client.complete_json('s', 'u', {})
            self.assertEqual(ctx.exception.status, expected, msg=repr(exc))


if __name__ == '__main__':
    unittest.main()
