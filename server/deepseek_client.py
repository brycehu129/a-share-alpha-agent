"""DeepSeek official API with JSON output and local schema validation."""
import json
import os
import re
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from collect_quotes import CST
from llm_errors import ClaudeError

BASE_URL = 'https://api.deepseek.com'
DEFAULT_MODEL = 'deepseek-flash'
MAX_RESPONSE_BYTES = 8_000_000
STATUS_BY_HTTP = {400: 'bad_request', 401: 'authentication_error', 402: 'payment_required',
                  403: 'permission_denied', 404: 'model_not_found', 429: 'rate_limited'}


def default_model():
    return os.environ.get('DEEPSEEK_MODEL') or DEFAULT_MODEL


def available():
    if not os.environ.get('DEEPSEEK_API_KEY'):
        return False, '未配置 DEEPSEEK_API_KEY（DeepSeek 官方开放平台）'
    try:
        import jsonschema
    except ImportError:
        return False, '未安装 jsonschema 包（pip install jsonschema）'
    return True, ''


def redact(text):
    text = str(text)
    for name in ('DEEPSEEK_API_KEY', 'OPENROUTER_API_KEY', 'ANTHROPIC_API_KEY'):
        key = os.environ.get(name)
        if key:
            text = text.replace(key, '[REDACTED]')
    return re.sub(r'sk-[A-Za-z0-9_\-]{8,}', '[REDACTED]', text)[:800]


def complete_json(system, user_content, schema, model=None, effort='high', max_tokens=32000,
                  timeout=600, max_retries=None):
    ok, reason = available()
    if not ok:
        raise ClaudeError('unavailable', reason)
    import jsonschema
    model = model or default_model()
    body = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system + '\nReturn only a JSON object matching this JSON Schema:\n'
             + json.dumps(schema, ensure_ascii=False)},
            {'role': 'user', 'content': user_content}],
        'response_format': {'type': 'json_object'},
        'max_tokens': max_tokens,
        'stream': False,
    }
    if model not in ('deepseek-chat', 'deepseek-reasoner'):
        body['reasoning_effort'] = {'medium': 'high', 'xhigh': 'max'}.get(effort, effort)
    request = Request(BASE_URL + '/chat/completions',
                      data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                      headers={'Content-Type': 'application/json',
                               'Authorization': 'Bearer ' + os.environ['DEEPSEEK_API_KEY']}, method='POST')
    started = datetime.now(CST)
    retries = 2 if max_retries is None else max_retries
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            break
        except HTTPError as exc:
            status = exc.code
            with exc:
                detail = redact(exc.read(MAX_RESPONSE_BYTES).decode('utf-8', 'replace'))
            failure = ClaudeError(STATUS_BY_HTTP.get(status, 'server_error' if status >= 500 else 'bad_request'),
                                  'HTTP %s: %s' % (status, detail))
            if attempt >= retries or status not in (408, 429, 500, 502, 503, 504):
                raise failure from exc
        except (URLError, TimeoutError, OSError) as exc:
            if attempt >= retries:
                raise ClaudeError('connection_error', redact(exc)) from exc
        time.sleep(min(2 ** attempt, 8))
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ClaudeError('bad_response', '响应超过大小限制')
    try:
        payload = json.loads(raw)
        if payload.get('error'):
            raise ClaudeError('server_error', redact(payload['error']))
        choice = payload['choices'][0]
        finish = choice.get('finish_reason')
        content = choice['message'].get('content')
        usage = payload.get('usage') or {}
        if not isinstance(usage, dict):
            raise TypeError('usage')
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ClaudeError('bad_response', 'DeepSeek 返回的响应结构无效') from exc
    if finish == 'length':
        raise ClaudeError('truncated', 'DeepSeek 输出被截断，不采用')
    if finish == 'content_filter':
        raise ClaudeError('refusal', 'DeepSeek 拒绝生成')
    if not isinstance(content, str) or not content.strip():
        raise ClaudeError('invalid_json', 'DeepSeek 未返回 JSON 内容')
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise ClaudeError('invalid_json', 'DeepSeek 返回的内容不是合法 JSON') from exc
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as exc:
        raise ClaudeError('invalid_schema', 'DeepSeek JSON 不符合所需结构，未采用') from exc
    return data, {
        'provider': 'deepseek', 'model': payload.get('model') or model, 'effort': effort,
        'requested_at': started.isoformat(), 'completed_at': datetime.now(CST).isoformat(),
        'stop_reason': finish, 'attempts': attempt + 1,
        'input_tokens': usage.get('prompt_tokens'), 'output_tokens': usage.get('completion_tokens'),
        'cache_read_input_tokens': usage.get('prompt_cache_hit_tokens'),
    }