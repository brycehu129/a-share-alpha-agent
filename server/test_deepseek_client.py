import io
import importlib.util
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import deepseek_client as client
from llm_errors import ClaudeError

KEY = 'sk-deepseek-test1234567890abcdef'
SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
          'required': ['ok'], 'additionalProperties': False}


def response(content='{"ok": true}', finish='stop'):
    return io.BytesIO(json.dumps({'model': 'deepseek-flash', 'choices': [
        {'message': {'content': content}, 'finish_reason': finish}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 20}}).encode())


@unittest.skipUnless(importlib.util.find_spec('jsonschema'), 'optional DeepSeek validator not installed')
class DeepSeekTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'DEEPSEEK_API_KEY': KEY}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_official_endpoint_credentials_and_json_contract(self):
        with patch.object(client, 'urlopen', return_value=response()) as send:
            data, meta = client.complete_json('system', 'facts', SCHEMA, effort='medium', timeout=100,
                                              max_tokens=12000, max_retries=0)
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.deepseek.com/chat/completions')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + KEY)
        body = json.loads(request.data)
        self.assertEqual(body['model'], 'deepseek-flash')
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertEqual(body['reasoning_effort'], 'high')
        self.assertEqual(body['max_tokens'], 12000)
        self.assertNotIn('provider', body)
        self.assertNotIn('reasoning', body)
        self.assertIn(json.dumps(SCHEMA), body['messages'][0]['content'])
        self.assertEqual(send.call_args.kwargs['timeout'], 100)
        self.assertEqual(data, {'ok': True})
        self.assertEqual(meta['provider'], 'deepseek')

    def test_invalid_outputs_are_rejected(self):
        for content, finish, status in [('', 'stop', 'invalid_json'), ('broken', 'stop', 'invalid_json'),
                                        ('{}', 'stop', 'invalid_schema'), ('{"ok": "yes"}', 'stop', 'invalid_schema'),
                                        ('{"ok": true}', 'length', 'truncated')]:
            with self.subTest(status=status, content=content), patch.object(client, 'urlopen', return_value=response(content, finish)):
                with self.assertRaises(ClaudeError) as caught:
                    client.complete_json('s', 'u', SCHEMA, max_retries=0)
                self.assertEqual(caught.exception.status, status)

    def test_no_key_no_network_and_no_openrouter_fallback(self):
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'other'}, clear=True), patch.object(client, 'urlopen') as send:
            with self.assertRaises(ClaudeError) as caught:
                client.complete_json('s', 'u', SCHEMA)
            self.assertEqual(caught.exception.status, 'unavailable')
            send.assert_not_called()

    def test_error_redaction_and_no_retry(self):
        error = HTTPError(client.BASE_URL, 401, 'bad key', {}, io.BytesIO(KEY.encode()))
        with patch.object(client, 'urlopen', side_effect=error) as send:
            with self.assertRaises(ClaudeError) as caught:
                client.complete_json('s', 'u', SCHEMA, max_retries=0)
            self.assertEqual(caught.exception.status, 'authentication_error')
            self.assertNotIn(KEY, caught.exception.message)
            self.assertEqual(send.call_count, 1)

    def test_network_retry_is_bounded(self):
        with patch.object(client, 'urlopen', side_effect=[URLError('offline'), response()]) as send, \
                patch.object(client.time, 'sleep'):
            _, meta = client.complete_json('s', 'u', SCHEMA, max_retries=1)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(meta['attempts'], 2)

    def test_facade_mixes_official_and_openrouter_models_without_crossing_credentials(self):
        import claude_client
        with patch.dict(os.environ, {'LLM_PROVIDER': 'openrouter', 'OPENROUTER_API_KEY': 'or-key'}), \
                patch.object(client, 'urlopen', return_value=response()) as send, \
                patch('openrouter_client.complete_json', return_value=({}, {})) as router:
            claude_client.complete_json('s', 'u', SCHEMA, model='deepseek-flash', max_retries=0)
            self.assertEqual(send.call_args.args[0].get_header('Authorization'), 'Bearer ' + KEY)
            router.assert_not_called()
            claude_client.complete_json('s', 'u', SCHEMA, model='anthropic/claude-sonnet-5')
            self.assertEqual(router.call_args.kwargs['model'], 'anthropic/claude-sonnet-5')
            self.assertEqual(send.call_count, 1)

    def test_default_official_model_and_no_fallback_when_key_missing(self):
        import claude_client
        with patch.dict(os.environ, {'OPENROUTER_MODEL': 'deepseek-flash', 'OPENROUTER_API_KEY': 'or-key'}):
            self.assertEqual(claude_client.default_model(), 'deepseek-flash')
            self.assertEqual(claude_client.provider(), 'deepseek')
            os.environ.pop('DEEPSEEK_API_KEY')
            self.assertFalse(claude_client.available()[0])
            with patch('openrouter_client.complete_json') as router:
                with self.assertRaises(ClaudeError):
                    claude_client.complete_json('s', 'u', SCHEMA)
                router.assert_not_called()

    def test_explicit_shared_model_not_replaced_by_another_available_provider(self):
        import claude_client
        for model, provider in [('anthropic/claude-opus-5', 'openrouter'), ('claude-sonnet-5', 'anthropic'),
                                ('deepseek-v4-pro', 'deepseek')]:
            with self.subTest(model=model), patch.dict(os.environ, {'OPENROUTER_MODEL': model}):
                self.assertEqual(claude_client.default_model(), model)
                self.assertEqual(claude_client.provider(), provider)