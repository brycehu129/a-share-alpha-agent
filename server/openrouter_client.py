"""OpenRouter 客户端（OpenAI 兼容的 chat/completions 接口）。Python 3.9+，只用标准库。

用 OpenRouter 时**不需要装 anthropic 包**：整个调用就是一次 HTTPS POST。接口形状按 OpenRouter
官方文档核对过（2026-09-19），并用真实端点验证了错误响应格式 `{"error": {"message", "code"}}`。

和直连 Anthropic SDK 的几处本质区别，都在这里处理：

1. **推理 token 和可见输出共用同一个 max_tokens 预算。** 推理把预算吃光时，OpenRouter 返回
   `finish_reason: "length"` 且**内容为空，但推理 token 照样计费**。这里专门识别这种情况，
   报出"推理耗尽了预算"而不是含糊的"输出为空"，方便你调大 max_tokens 或调低 effort。
2. **结构化输出的 strict 模式各家实现不同**：有的服务商保证合规，有的只当参考。所以返回的
   JSON 不能盲信——这里容忍 ```json 围栏和前后多余文字（并在元信息里标记 json_repaired），
   上层的数值校验（scenario_analyst.validate_entry 等）是真正的兜底。
3. `provider.require_parameters = true`：只路由到真正支持 json_schema 的服务商，
   否则 OpenRouter 可能把请求悄悄转给不支持结构化输出的端点。
4. 错误分类：402 = 余额不足（要单独提示，不是"服务挂了"）；HTTP 200 但 body 里带 `error`
   也是失败，不能当成功处理。

不依赖响应修复插件（文档页面无法核实其确切参数，没验证过的参数不发）。

凭证只从环境变量 OPENROUTER_API_KEY 读，错误信息返回前统一脱敏。
"""
import json
import os
import random
import re
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from collect_quotes import CST
from llm_errors import ClaudeError

DEFAULT_BASE_URL = 'https://openrouter.ai/api/v1'
DEFAULT_MODEL = 'anthropic/claude-opus-5'
MAX_RESPONSE_BYTES = 8_000_000
DEFAULT_RETRIES = 2

STATUS_BY_HTTP = {400: 'bad_request', 401: 'authentication_error', 402: 'payment_required',
                  403: 'permission_denied', 404: 'model_not_found', 408: 'connection_error',
                  429: 'rate_limited'}
RETRYABLE = {408, 429, 500, 502, 503, 504}


def base_url():
    return os.environ.get('OPENROUTER_BASE_URL', DEFAULT_BASE_URL).rstrip('/')


def default_model():
    return os.environ.get('OPENROUTER_MODEL', DEFAULT_MODEL)


def available():
    if not os.environ.get('OPENROUTER_API_KEY'):
        return False, '未配置 OPENROUTER_API_KEY'
    return True, ''


def redact(text):
    text = re.sub(r'sk-or-[A-Za-z0-9_\-]{8,}', '[REDACTED]', str(text))
    key = os.environ.get('OPENROUTER_API_KEY', '')
    if key and len(key) > 8:
        text = text.replace(key, '[REDACTED]')
    return text[:800]


def build_body(system, user_content, schema, model, effort, max_tokens):
    return {
        'model': model,
        'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user_content}],
        'max_tokens': max_tokens,
        'response_format': {'type': 'json_schema',
                            'json_schema': {'name': 'result', 'strict': True, 'schema': schema}},
        'provider': {'require_parameters': True},
        # exclude：我们不用推理文本，不要让它占响应体积；推理 token 仍然计入预算与计费。
        'reasoning': {'effort': effort, 'exclude': True},
    }


def _error_message(raw):
    text = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw)
    try:
        err = json.loads(text).get('error')
    except (ValueError, AttributeError):
        return text[:300]
    if isinstance(err, dict):
        return str(err.get('message') or err)
    return str(err) if err else text[:300]


def _post(url, body, timeout):
    """一次 HTTP 请求。返回 (状态码, 响应体bytes)；HTTP 错误状态也返回而不是抛出。"""
    headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + os.environ['OPENROUTER_API_KEY'],
               'X-OpenRouter-Title': 'Alpha Shadow'}
    if os.environ.get('OPENROUTER_SITE_URL'):
        headers['HTTP-Referer'] = os.environ['OPENROUTER_SITE_URL']
    req = Request(url, data=json.dumps(body, ensure_ascii=False).encode('utf-8'), headers=headers, method='POST')
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            status = resp.status
    except HTTPError as exc:
        return exc.code, exc.read(MAX_RESPONSE_BYTES)
    except (URLError, TimeoutError, OSError) as exc:
        raise ClaudeError('connection_error', redact('%s: %s' % (type(exc).__name__, exc))) from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ClaudeError('bad_response', '响应超过大小限制')
    return status, raw


def parse_json_text(text):
    """返回 (数据, 是否经过修复)。容忍 ```json 围栏和前后多余文字；仍解析不出来就抛 ValueError。"""
    try:
        return json.loads(text), False
    except ValueError:
        pass
    fenced = re.search(r'```(?:json)?\s*(.*?)```', text, re.S)
    for candidate in ([fenced.group(1)] if fenced else []) + [text[text.find('{'):text.rfind('}') + 1]]:
        if candidate.strip():
            try:
                return json.loads(candidate), True
            except ValueError:
                continue
    raise ValueError('无法从模型输出里解析出 JSON')


def _content_text(message):
    content = message.get('content')
    if isinstance(content, list):                       # 部分模型返回内容块列表
        content = ''.join(p.get('text', '') for p in content if isinstance(p, dict))
    return content or ''


def complete_json(system, user_content, schema, model=None, effort='high', max_tokens=32000,
                  timeout=600, max_retries=None):
    """返回 (数据, 元信息)，失败抛 ClaudeError（与 claude_client 的 Anthropic 路径同一套 status）。"""
    ok, reason = available()
    if not ok:
        raise ClaudeError('unavailable', reason)
    model = model or default_model()
    retries = DEFAULT_RETRIES if max_retries is None else max_retries
    url = base_url() + '/chat/completions'
    body = build_body(system, user_content, schema, model, effort, max_tokens)
    started = datetime.now(CST)
    attempt = 0
    while True:
        try:
            status, raw = _post(url, body, timeout)
            failure = None
        except ClaudeError as exc:
            status, raw, failure = None, b'', exc
        if failure is None and status == 200:
            try:
                payload = json.loads(raw)
            except ValueError as exc:
                raise ClaudeError('bad_response', '响应不是合法 JSON') from exc
            if payload.get('error'):                    # HTTP 200 但 body 里带 error：服务商中途失败
                code = payload['error'].get('code') if isinstance(payload['error'], dict) else None
                failure = ClaudeError('server_error', redact('上游返回错误(code=%s): %s' % (code, _error_message(raw))))
                status = code if isinstance(code, int) else 502
            else:
                break
        elif failure is None:
            failure = ClaudeError(STATUS_BY_HTTP.get(status, 'server_error' if status >= 500 else 'bad_request'),
                                  redact('HTTP %s: %s' % (status, _error_message(raw))))
        if attempt >= retries or (status is not None and status not in RETRYABLE) or (
                status is None and failure.status != 'connection_error'):
            raise failure
        time.sleep(min(2 ** attempt, 8) + random.random() * 0.3)
        attempt += 1

    choice = (payload.get('choices') or [{}])[0]
    message = choice.get('message') or {}
    usage = payload.get('usage') or {}
    details = usage.get('completion_tokens_details') or {}
    finish = choice.get('finish_reason')
    text = _content_text(message)
    if finish == 'length' or (not text.strip() and finish in (None, 'length')):
        reasoning = details.get('reasoning_tokens')
        visible = (usage.get('completion_tokens') or 0) - (reasoning or 0)
        hint = ('推理消耗了几乎全部 max_tokens（推理 %s，可见输出 %s），可见内容为空但推理 token 照样计费——'
                '请调大 max_tokens 或调低 effort' % (reasoning, visible)) if not text.strip() else \
               '输出被 max_tokens 截断，结果不完整，不采用'
        raise ClaudeError('truncated', hint)
    if finish == 'content_filter':
        raise ClaudeError('refusal', '内容被服务商过滤')
    if not text.strip():
        raise ClaudeError('invalid_json', '模型没有返回内容（finish_reason=%s）' % finish)
    try:
        data, repaired = parse_json_text(text)
    except ValueError as exc:
        raise ClaudeError('invalid_json', '结构化输出不是合法JSON：' + redact(text[:300])) from exc

    prompt_details = usage.get('prompt_tokens_details') or {}
    return data, {
        'provider': 'openrouter', 'model': payload.get('model') or model, 'effort': effort,
        'requested_at': started.isoformat(), 'completed_at': datetime.now(CST).isoformat(),
        'stop_reason': finish, 'json_repaired': repaired, 'attempts': attempt + 1,
        'input_tokens': usage.get('prompt_tokens'), 'output_tokens': usage.get('completion_tokens'),
        'reasoning_tokens': details.get('reasoning_tokens'),
        'cache_read_input_tokens': prompt_details.get('cached_tokens'),
        'cost': usage.get('cost'), 'upstream_provider': payload.get('provider'), 'generation_id': payload.get('id'),
    }
