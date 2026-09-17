#!/usr/bin/env python3
"""发送企业微信群机器人 webhook 消息，并管理该 webhook 的本地持久化配置。

Python 3.9+，仅使用标准库，和本仓库其余脚本的约定一致（参见 collect_quotes.py）。
"""
import json
import os
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

WEBHOOK_PREFIX = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
MAX_RESPONSE_BYTES = 100_000


class ConfigError(ValueError):
    """配置本身有问题（比如 webhook 地址格式不对、配置文件损坏）。"""


class PushError(RuntimeError):
    """向企业微信发送消息失败（网络错误，或企业微信返回了非0 errcode）。"""


def validate_webhook_url(url):
    url = (url or "").strip()
    if not url:
        raise ConfigError("Webhook 地址不能为空")
    if not url.startswith(WEBHOOK_PREFIX):
        raise ConfigError(
            "看起来不是企业微信群机器人的 webhook 地址（应以 " + WEBHOOK_PREFIX + " 开头）"
        )
    return url


def load_config(path):
    """读取配置文件；文件不存在时返回空配置，这不是错误（还没配置过）。"""
    if not os.path.exists(path):
        return {"webhook_url": None}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ConfigError("配置文件内容损坏（不是一个 JSON 对象）")
    return {"webhook_url": data.get("webhook_url")}


def save_config(path, webhook_url):
    """校验后原子写入配置文件：先写临时文件再 rename，避免读到写一半的文件。"""
    webhook_url = validate_webhook_url(webhook_url)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".webapp_config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"webhook_url": webhook_url}, f, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    return webhook_url


def mask_webhook_url(url):
    """页面展示用：只留最后6位，其余打码，绝不把完整 webhook 显示在页面上。"""
    if not url:
        return None
    return "..." + url[-6:]


def send_wecom_message(webhook_url, content, timeout=10):
    """给企业微信群机器人 webhook 发一条文本消息。失败抛 PushError。"""
    webhook_url = validate_webhook_url(webhook_url)
    payload = json.dumps(
        {"msgtype": "text", "text": {"content": content}}, ensure_ascii=False
    ).encode("utf-8")
    request = Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES)
    except HTTPError as exc:
        raise PushError(f"企业微信返回HTTP错误: {exc.code} {exc.reason}") from exc
    except URLError as exc:
        raise PushError(f"无法连接企业微信 webhook: {exc.reason}") from exc
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PushError(
            "企业微信响应不是合法JSON: " + raw[:200].decode("utf-8", "replace")
        ) from exc
    if body.get("errcode") != 0:
        raise PushError(
            f"企业微信返回错误: errcode={body.get('errcode')} errmsg={body.get('errmsg')}"
        )
    return body
