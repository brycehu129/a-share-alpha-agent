"""Claude API 封装。本仓库唯一一处调用大模型的地方。

**这是本项目第一个第三方依赖。** 其余代码只用标准库（见 requirements.txt），
所以 `import anthropic` 写在函数内部做惰性导入：cron 脚本每次跑之前都会执行
全量测试套件，如果这个包没装就在导入期报错，会连带把 15:35 的日线流程和 09:31
的开盘观察任务一起弄挂——那两条是纯规则的，不该被 AI 层的依赖问题影响。
同样的处理方式在 alpha_data.py 里对 baostock 已经用过。

凭证只通过环境变量 ANTHROPIC_API_KEY 读取，永远不写进日志、报告或归档；
错误信息返回前统一做脱敏（沿用 tushare_probe.classify 的 [REDACTED] 做法）。
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


class ClaudeError(RuntimeError):
    """调用失败。带 `status` 字段区分可重试与不可重试，便于上层决定怎么降级。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _redact(text):
    """把任何看起来像 API key 的串抹掉，再把可能被回显的真实 key 也抹掉。"""
    text = re.sub(r'sk-ant-[A-Za-z0-9_\-]{8,}', '[REDACTED]', str(text))
    key = os.environ.get('ANTHROPIC_API_KEY', '')
    if key and len(key) > 8:
        text = text.replace(key, '[REDACTED]')
    return text[:800]


def available():
    """(能不能调用, 原因)。上层据此决定是只出规则报告还是带 AI 研判。"""
    if not os.environ.get('ANTHROPIC_API_KEY'):
        return False, '未配置 ANTHROPIC_API_KEY'
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
    ok, reason = available()
    if not ok:
        raise ClaudeError('unavailable', reason)
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
