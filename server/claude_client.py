"""大模型调用的统一入口。本仓库唯一一处调用大模型的地方。

**三个后端，同一套接口**（`available()` / `complete_json()` / `ClaudeError`）：

- `openrouter`：经 OpenRouter 调用（`openrouter_client.py`，只用标准库，**不需要装任何包**）。
- `deepseek`：直连 DeepSeek 官方 API（`deepseek_client.py`），JSON mode 输出用 jsonschema 本地校验。
- `anthropic`：直连 Anthropic SDK（本项目第一个第三方依赖，`import anthropic` 写在函数内部
  惰性导入——cron 脚本每次跑之前都会执行全量测试套件，包没装就在导入期报错会连带把日线
  流程和开盘观察一起弄挂，那两条是纯规则的，不该被 AI 层的依赖问题影响）。

选择规则：显式模型名决定渠道；默认后端由共享模型、LLM_PROVIDER 和可用 key 决定。
LLM_PROVIDER=anthropic 保留旧的默认模型行为。上层只认这里的接口，切换后端
不用改任何一行。模块名保留 claude_client 是为了不牵动大量引用，它现在是"LLM 入口"。

凭证只通过环境变量读取，永远不写进日志、报告或归档；错误信息返回前统一做脱敏。
"""
import json
import os
import re
from datetime import datetime

from collect_quotes import CST

MODEL = os.environ.get('CLAUDE_MODEL', 'claude-opus-5')
EFFORT = os.environ.get('CLAUDE_EFFORT', 'high')
MAX_TOKENS = 32000
TIMEOUT_SECONDS = 600


from llm_errors import ClaudeError  # noqa: E402,F401  重新导出，保持 claude_client.ClaudeError 可用


def _redact(text):
    """把任何看起来像 API key 的串抹掉，再把可能被回显的真实 key 也抹掉。"""
    text = re.sub(r'sk-[A-Za-z0-9_\-]{8,}', '[REDACTED]', str(text))
    for name in ('ANTHROPIC_API_KEY', 'OPENROUTER_API_KEY', 'DEEPSEEK_API_KEY'):
        key = os.environ.get(name, '')
        if key and len(key) > 8:
            text = text.replace(key, '[REDACTED]')
    return text[:800]


def provider(model=None):
    """显式模型先按名称选渠道；未指定时使用默认配置，不跨渠道回退凭据。"""
    if model:
        if model.startswith('deepseek-'):
            return 'deepseek'
        if '/' in model:
            return 'openrouter'
        if model.startswith('claude-'):
            return 'anthropic'
    chosen = os.environ.get('LLM_PROVIDER', '').strip().lower()
    shared = os.environ.get('OPENROUTER_MODEL', '')
    if chosen != 'anthropic' and shared and ('/' in shared or shared.startswith(('deepseek-', 'claude-'))):
        return provider(shared)
    if chosen in ('openrouter', 'anthropic', 'deepseek'):
        return chosen
    if not os.environ.get('OPENROUTER_API_KEY') and os.environ.get('DEEPSEEK_API_KEY'):
        return 'deepseek'
    return 'openrouter' if os.environ.get('OPENROUTER_API_KEY') else 'anthropic'


def default_model():
    shared = os.environ.get('OPENROUTER_MODEL', '')
    if shared and os.environ.get('LLM_PROVIDER', '').strip().lower() != 'anthropic':
        return shared
    backend = provider()
    if backend == 'deepseek':
        import deepseek_client
        return deepseek_client.default_model()
    if backend == 'openrouter':
        import openrouter_client
        return openrouter_client.default_model()
    return MODEL


def available(model=None):
    """(能不能调用, 原因)。上层据此决定是只出规则报告还是带 AI 研判。"""
    if provider(model) == 'deepseek':
        import deepseek_client
        return deepseek_client.available()
    if provider(model) == 'openrouter':
        import openrouter_client
        return openrouter_client.available()
    if not os.environ.get('ANTHROPIC_API_KEY'):
        return False, '未配置 ANTHROPIC_API_KEY（或改用 OpenRouter：配置 OPENROUTER_API_KEY）'
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, '未安装 anthropic 包（pip install anthropic）'
    return True, ''


def complete_json(system, user_content, schema, model=None, effort=None,
                  max_tokens=MAX_TOKENS, timeout=TIMEOUT_SECONDS, max_retries=None):
    """要求模型按 json_schema 返回结构化结果，返回 (数据, 元信息)。

    用结构化输出而不是"请你输出JSON"的提示词，是因为后面几期要按这些字段自动
    执行（持仓合约的卖出条件要被代码逐条检查）——自由文本解析不了，一次格式
    漂移就会让监控静默失效。

    system 用列表形式并在最后一块打 cache_control：稳定的策略口径在前、当日
    易变数据在 user 消息里。一天只调一次的话缓存（默认5分钟TTL）命中不了，
    真正省钱要等盘中哨兵那种高频调用，但现在就摆对位置，之后不用再改结构。
    """
    model = model or default_model()
    ok, reason = available(model)
    if not ok:
        raise ClaudeError('unavailable', reason)
    if provider(model) == 'deepseek':
        import deepseek_client
        return deepseek_client.complete_json(system, user_content, schema, model=model, effort=effort or EFFORT,
                                             max_tokens=max_tokens, timeout=timeout, max_retries=max_retries)
    if provider(model) == 'openrouter':
        import openrouter_client
        return openrouter_client.complete_json(system, user_content, schema, model=model, effort=effort or EFFORT,
                                               max_tokens=max_tokens, timeout=timeout, max_retries=max_retries)
    import anthropic

    model = model or MODEL
    started = datetime.now(CST)
    # SDK 默认带 2 次重试：超时 600 秒 × 3 次，远超任何一个有 systemd 时限的定时任务。
    # 有时限的调用方显式传 max_retries=0，让"总耗时 ≤ timeout"成立。
    client = anthropic.Anthropic(timeout=timeout, **({} if max_retries is None else {'max_retries': max_retries}))
    try:
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=[{'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}}],
            thinking={'type': 'adaptive'},
            output_config={
                'effort': effort or EFFORT,
                'format': {'type': 'json_schema', 'schema': schema},
            },
            messages=[{'role': 'user', 'content': user_content}],
        ) as stream:
            response = stream.get_final_message()
    except anthropic.NotFoundError as exc:
        raise ClaudeError('model_not_found', _redact(exc)) from exc
    except anthropic.AuthenticationError as exc:
        raise ClaudeError('authentication_error', _redact(exc)) from exc
    except anthropic.PermissionDeniedError as exc:
        raise ClaudeError('permission_denied', _redact(exc)) from exc
    except anthropic.RateLimitError as exc:
        raise ClaudeError('rate_limited', _redact(exc)) from exc
    except anthropic.APIStatusError as exc:
        status = 'server_error' if exc.status_code >= 500 else 'bad_request'
        raise ClaudeError(status, _redact(exc)) from exc
    except anthropic.APIConnectionError as exc:
        raise ClaudeError('connection_error', _redact(exc)) from exc

    if response.stop_reason == 'refusal':
        detail = getattr(response, 'stop_details', None)
        raise ClaudeError('refusal', '模型拒绝生成：' + str(getattr(detail, 'category', '未知')))
    if response.stop_reason == 'max_tokens':
        raise ClaudeError('truncated', '输出被 max_tokens 截断，结果不完整，不采用')

    text = ''.join(b.text for b in response.content if b.type == 'text')
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ClaudeError('invalid_json', '结构化输出不是合法JSON：' + _redact(text[:300])) from exc

    usage = response.usage
    meta = {
        'model': response.model, 'effort': effort or EFFORT,
        'requested_at': started.isoformat(), 'completed_at': datetime.now(CST).isoformat(),
        'stop_reason': response.stop_reason,
        'input_tokens': getattr(usage, 'input_tokens', None),
        'output_tokens': getattr(usage, 'output_tokens', None),
        'cache_read_input_tokens': getattr(usage, 'cache_read_input_tokens', None),
        'cache_creation_input_tokens': getattr(usage, 'cache_creation_input_tokens', None),
    }
    return data, meta


CHECK_SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}, 'echo': {'type': 'string'}},
                'required': ['ok', 'echo'], 'additionalProperties': False}


def check(model=None, effort='low'):
    """用最小的一次结构化调用验证：key 有效、余额够、模型存在、结构化输出可用。返回 (成功?, 报告行列表)。

    花费约几分钱。没有 key 时不发请求。这是给你上线前自检用的——没配好就会在这里明确报错，
    而不是等到周一盘中哨兵触发时才发现研判一直静默失败。"""
    import time
    model = model or default_model()
    backend = provider(model)
    lines = ['后端：%s' % backend]
    ok, why = available(model)
    if not ok:
        return False, lines + ['不可用：' + why]
    if backend == 'deepseek':
        lines.append('模型：%s（DeepSeek 官方 API）' % model)
    elif backend == 'openrouter':
        import openrouter_client
        lines.append('模型：%s（可用 OPENROUTER_MODEL 修改）' % (model or openrouter_client.default_model()))
    else:
        lines.append('模型：%s（可用 CLAUDE_MODEL 修改）' % (model or MODEL))
    started = time.monotonic()
    try:
        data, meta = complete_json('你是连通性自检程序，只按 schema 回答。', '请返回 ok=true，echo 填 "pong"。',
                                   CHECK_SCHEMA, model=model, effort=effort, max_tokens=2000, timeout=60, max_retries=0)
    except ClaudeError as exc:
        return False, lines + ['失败 [%s]：%s' % (exc.status, exc.message)]
    lines.append('成功：%.1f 秒，实际模型 %s' % (time.monotonic() - started, meta.get('model')))
    lines.append('token：输入 %s / 输出 %s（其中推理 %s）' % (meta.get('input_tokens'), meta.get('output_tokens'),
                                                        meta.get('reasoning_tokens')))
    if meta.get('cost') is not None:
        lines.append('本次花费：%s（OpenRouter 积分）' % meta['cost'])
    if meta.get('json_repaired'):
        lines.append('注意：该模型/服务商返回的 JSON 需要修复才能解析（strict 模式只当参考）——'
                     '结构化输出不够可靠，建议换一个模型')
    if data != {'ok': True, 'echo': 'pong'}:
        lines.append('注意：返回内容和预期不完全一致：%r' % (data,))
    return True, lines


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description='验证大模型配置是否可用（会发一次极小的真实请求）')
    p.add_argument('command', choices=['check'])
    p.add_argument('--model', help='覆盖默认模型')
    p.add_argument('--effort', default='low')
    a = p.parse_args(argv)
    import llm_settings
    llm_settings.apply()      # 页面保存的 key/模型优先于环境变量；库代码本身只看环境变量
    ok, lines = check(a.model, a.effort)
    print('\n'.join(lines))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
