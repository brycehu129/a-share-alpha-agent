"""大模型配置的页面存储：OpenRouter key、模型名。Python 3.9+，只用标准库。

让你在后台"推送配置"页填 key，不用登服务器改 /etc/alpha-shadow.env。

**几条不能破的规矩**

1. key 落在 `server/data/private/llm_settings.json`（0600，原子写），**绝不进 git**、不进日志、
   不进页面：页面只显示掩码（末 4 位），输入框永远是空的。
2. **只有程序入口才读这个文件**（`apply()` 由各个 main() 和 webapp 显式调用），库代码
   （claude_client / openrouter_client）仍然只看环境变量。原因：cron 脚本每次先跑全量测试，
   测试里"没配 key"是常见前提；库代码若自己去读文件，服务器上一存了 key，测试就全变了行为，
   日线流程会被连带中止——和上次 OPENROUTER_API_KEY 那次是同一个坑。
3. **优先级：页面保存的 > 环境变量。** 你刚在页面保存的东西必须立刻生效，不能被一个忘了的
   旧环境变量悄悄盖住；页面会显示当前生效的是哪个来源。清除页面保存的值后自动回落到环境变量。
4. 只管 OpenRouter（`OPENROUTER_API_KEY` / `OPENROUTER_MODEL` / `SENTINEL_MODEL`）。
   直连 Anthropic 的 key 仍只走环境变量。
"""
import json
import os
import re
import tempfile

import portfolio_book

# 存储字段 -> 它覆盖的环境变量
ENV_NAMES = {'api_key': 'OPENROUTER_API_KEY', 'model': 'OPENROUTER_MODEL', 'sentinel_model': 'SENTINEL_MODEL'}

KEY_PATTERN = re.compile(r'sk-or-[A-Za-z0-9_\-]{16,200}')
MODEL_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/\-]{1,99}')

# 进程内"没被页面覆盖之前，环境里原来是什么"。清除页面值时据此恢复，而不是留着旧值。
_ORIGINALS = {}


class SettingsError(ValueError):
    """输入不合法。消息里绝不带用户提交的内容——key 不能经由错误提示回显。"""


def path(directory=None):
    return os.path.join(directory or portfolio_book.default_dir(), 'llm_settings.json')


def validate_key(raw):
    key = (raw or '').strip()
    if not key:
        raise SettingsError('API key 不能为空')
    if not KEY_PATTERN.fullmatch(key):
        raise SettingsError('格式不对：OpenRouter 的 key 以 sk-or- 开头，只含字母、数字、下划线和连字符，'
                            '且中间不能有空格或换行（请重新复制）')
    return key


def validate_model(raw):
    model = (raw or '').strip()
    if not model:
        return ''
    if not MODEL_PATTERN.fullmatch(model):
        raise SettingsError('模型名格式不对：形如 anthropic/claude-opus-5，只含字母、数字和 . _ : / -')
    return model


def load(directory=None):
    """返回 (设置字典, 问题说明)。文件不存在不是问题；文件损坏要说出来，
    而不是当成"没配置"悄悄回落到别的来源。字段不合规的直接丢弃。"""
    p = path(directory)
    if not os.path.exists(p):
        return {}, ''
    try:
        with open(p, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}, '页面保存的配置文件无法读取（可能已损坏），已忽略；请重新保存。'
    if not isinstance(data, dict):
        return {}, '页面保存的配置文件内容异常，已忽略；请重新保存。'
    out = {}
    if isinstance(data.get('api_key'), str) and KEY_PATTERN.fullmatch(data['api_key']):
        out['api_key'] = data['api_key']
    for name in ('model', 'sentinel_model'):
        value = data.get(name)
        if isinstance(value, str) and MODEL_PATTERN.fullmatch(value):
            out[name] = value
    return out, ''


def _write(settings, directory=None):
    p = path(directory)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=os.path.dirname(p), suffix='.tmp')   # mkstemp 本身就是 0600
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    os.chmod(p, 0o600)


def save_key(raw, directory=None):
    key = validate_key(raw)
    settings, _ = load(directory)
    settings['api_key'] = key
    _write(settings, directory)


def save_models(model, sentinel_model, directory=None):
    """空字符串 = 清除该项、用默认值。改模型不需要重新输入 key。"""
    values = {'model': validate_model(model), 'sentinel_model': validate_model(sentinel_model)}
    settings, _ = load(directory)
    for name, value in values.items():
        if value:
            settings[name] = value
        else:
            settings.pop(name, None)
    _write(settings, directory)


def clear_key(directory=None):
    settings, _ = load(directory)
    settings.pop('api_key', None)
    _write(settings, directory)


def mask_key(key):
    """页面展示用：只露前缀和末 4 位。"""
    if not key:
        return None
    return 'sk-or-…' + key[-4:]


def apply(directory=None, environ=None, memo=None):
    """把页面保存的值放进环境变量（覆盖），并把已不再被页面设置的项恢复成原来的环境值。
    返回被页面覆盖的环境变量名列表（只有名字，不含值）。幂等，可反复调用。

    由程序入口调用，见模块说明第 2 条。"""
    if environ is None:
        environ, memo = os.environ, (_ORIGINALS if memo is None else memo)
    memo = {} if memo is None else memo
    settings, _ = load(directory)
    applied = []
    for field, env_name in ENV_NAMES.items():
        if env_name not in memo:
            memo[env_name] = environ.get(env_name)
        if field in settings:
            environ[env_name] = settings[field]
            applied.append(env_name)
        elif memo[env_name] is None:
            environ.pop(env_name, None)
        else:
            environ[env_name] = memo[env_name]
    return applied


def describe(directory=None, environ=None):
    """页面用的状态：每一项当前生效的值来自哪里。key 只给掩码。"""
    memo = _ORIGINALS if environ is None else {}
    environ = os.environ if environ is None else environ
    settings, problem = load(directory)
    out = {'problem': problem}
    for field, env_name in ENV_NAMES.items():
        env_value = memo[env_name] if env_name in memo else environ.get(env_name)
        if field in settings:
            source, value = 'page', settings[field]
        elif env_value:
            source, value = 'env', env_value
        else:
            source, value = 'none', None
        out[field] = {'source': source, 'value': mask_key(value) if field == 'api_key' else value,
                      'env_shadowed': field in settings and bool(env_value)}
    return out
