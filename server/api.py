"""前端（web/，Vue 单页应用）用到的 JSON 接口。只用标准库。

Handler 只负责鉴权、CSRF 和 IO；这里的函数是纯的「请求 → 数据」：
  GET  处理函数签名 fn(query: dict[str,str]) -> dict
  POST 处理函数签名 fn(body: dict)         -> dict
返回的 dict 会被补上 ok=True；出错抛 ApiError(message, status)，前端统一按 {ok:false, message} 展示。
"""
import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

import backup
import health_check
import llm_settings
from collect_quotes import CST
from wecom_push import (
    ConfigError,
    PushError,
    load_config,
    mask_webhook_url,
    save_config,
    send_wecom_message,
)

_GET, _POST = {}, {}
DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "webapp_config.json")
# 盘后分析要读归档（候选池、日线缓存）。服务器上 cron_common.sh 把 market-data 分支
# clone 到仓库根目录的 .history/；本机开发可以用 HISTORY_DIR 指到别处。
DEFAULT_HISTORY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".history")


def config_path():
    return os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH)


def history_dir():
    return Path(os.environ.get("HISTORY_DIR", DEFAULT_HISTORY_DIR))


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def get(path):
    def deco(fn):
        _GET[path] = fn
        return fn
    return deco


def post(path):
    def deco(fn):
        _POST[path] = fn
        return fn
    return deco


def is_api_path(path):
    return path in _GET or path in _POST


def dispatch(method, path, query=None, body=None):
    """返回 (http_status, payload)。任何未预期的异常都变成 500 + 可读文案，绝不让前端拿到 HTML 报错页。"""
    table = _GET if method == "GET" else _POST
    fn = table.get(path)
    if fn is None:
        return 404, {"ok": False, "message": "未知接口：%s" % path}
    try:
        result = fn(query or {}) if method == "GET" else fn(body or {})
    except ApiError as exc:
        return exc.status, {"ok": False, "message": exc.message}
    except Exception as exc:  # 接口层兜底：把异常类型告诉前端，细节留在服务器日志
        traceback.print_exc(file=sys.stderr)
        return 500, {"ok": False, "message": "服务器内部错误：%s" % type(exc).__name__}
    payload = dict(result) if isinstance(result, dict) else {"data": result}
    payload.setdefault("ok", True)
    return 200, payload


def text(body, key, default=""):
    """取表单字段并强制成字符串：JSON 体里可能是数字/null/列表，下游校验都按字符串写的。"""
    value = body.get(key, default)
    return "" if value is None else str(value)


# --- 设置页：推送 / 大模型 / 备份 / 健康 ------------------------------------------------

_llm_check_lock = threading.Lock()
SOURCE_LABEL = {"page": "页面保存", "env": "环境变量", "none": "未设置"}
LEVEL_ORDER = {"crit": 0, "warn": 1, "ok": 2, "skip": 3}
LLM_DEFAULT_MODELS = {"model": "anthropic/claude-opus-5", "sentinel_model": "anthropic/claude-sonnet-5"}


def run_llm_check(timeout=90):
    """测试连接：发一次极小的真实请求（约几分钱）。返回 (成功?, 说明行列表)。
    同一时刻只放一个进去——连点两下没有意义，还会花两份钱。"""
    if not _llm_check_lock.acquire(blocking=False):
        return False, ["已有一次测试正在进行，请稍候。"]
    try:
        import claude_client
        llm_settings.apply()
        return claude_client.check()
    except Exception as exc:  # 页面必须能显示失败原因，而不是 500
        import claude_client
        return False, ["测试异常：" + claude_client._redact("%s: %s" % (type(exc).__name__, exc))]
    finally:
        _llm_check_lock.release()


def push_state():
    masked = mask_webhook_url(load_config(config_path()).get("webhook_url"))
    return {"configured": bool(masked), "masked": masked}


def llm_state():
    import claude_client
    llm_settings.apply()
    info = llm_settings.describe()
    key = info["api_key"]
    notes = []
    if key["env_shadowed"]:
        notes.append("环境变量里也配了一个 key，已被页面保存的覆盖；点“清除”后会回落到环境变量的那个。")
    if os.environ.get("LLM_PROVIDER", "").strip().lower() == "anthropic":
        notes.append("服务器环境变量 LLM_PROVIDER=anthropic，当前后端是直连 Anthropic，这里填的 OpenRouter key 不会被用到。")
    if info["problem"]:
        notes.append(info["problem"])

    def model(field):
        cur = info[field]
        return {"value": cur["value"], "source": cur["source"], "source_label": SOURCE_LABEL[cur["source"]],
                "page_value": cur["value"] if cur["source"] == "page" else "", "default": LLM_DEFAULT_MODELS[field]}

    return {
        "key": {"configured": key["source"] != "none", "masked": key["value"], "source": key["source"],
                "source_label": SOURCE_LABEL[key["source"]]},
        "provider": claude_client.provider(),
        "model": model("model"),
        "sentinel_model": model("sentinel_model"),
        "notes": notes,
    }


def backup_state():
    level, text = backup.health()
    return {"level": level, "text": text}


def health_state():
    s = health_check.summary()
    checks = sorted(s["checks"], key=lambda c: LEVEL_ORDER.get(c["level"], 4))
    return {"heartbeat_age_min": s["heartbeat_age_min"], "stale": s["stale"], "critical": s["critical"],
            "checks": checks, "last_delivery": s.get("last_delivery")}


@get("/api/settings")
def api_settings(query):
    return {"push": push_state(), "llm": llm_state(), "backup": backup_state(), "health": health_state()}


@post("/api/settings/webhook")
def api_save_webhook(body):
    try:
        save_config(config_path(), text(body, "webhook_url"))
    except ConfigError as exc:
        raise ApiError("保存失败：%s" % exc)
    except OSError as exc:
        raise ApiError("写入失败：%s" % type(exc).__name__, 500)
    return {"message": "已保存。", "push": push_state()}


@post("/api/settings/test-push")
def api_test_push(body):
    webhook_url = load_config(config_path()).get("webhook_url")
    if not webhook_url:
        raise ApiError("还没配置 webhook，无法发送测试消息。")
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    try:
        send_wecom_message(webhook_url, "[Alpha Shadow] 测试消息，发送于 %s (UTC+8)" % now)
    except PushError as exc:
        raise ApiError("发送失败：%s" % exc, 502)
    return {"message": "测试消息已发送，请检查企业微信群。"}


def _llm_write(action, message):
    """页面上三个「写设置」的动作共用：校验失败 → 400，磁盘写失败 → 500，成功返回最新状态。"""
    try:
        action()
    except llm_settings.SettingsError as exc:
        raise ApiError(str(exc))
    except OSError as exc:
        raise ApiError("写入失败：%s" % type(exc).__name__, 500)
    return {"message": message, "llm": llm_state()}


@post("/api/llm/key")
def api_llm_key(body):
    return _llm_write(lambda: llm_settings.save_key(text(body, "api_key")), "已保存。点“测试连接”确认 key 可用。")


@post("/api/llm/models")
def api_llm_models(body):
    return _llm_write(lambda: llm_settings.save_models(text(body, "model"), text(body, "sentinel_model")), "模型已保存。")


@post("/api/llm/clear")
def api_llm_clear(body):
    return _llm_write(llm_settings.clear_key, "已清除页面保存的 key。")


@post("/api/llm/check")
def api_llm_check(body):
    ok, lines = run_llm_check()
    return {"check_ok": ok, "lines": lines}
