#!/usr/bin/env python3
"""看板数据：直接从GitHub market-data分支拉取dashboard/latest.json（带30秒缓存），
由 /api/dashboard 交给前端（web/）渲染。

这是Artifact看板（claude.ai/artifact/...）的服务器版：Artifact的CSP不让页面自己
fetch raw.githubusercontent.com，只能靠Claude这边的定时任务把数据同步进Artifact
的db能力——而这个定时任务一直没有真的建起来，所以Artifact页面一直停在静态快照。
这个模块跑在服务器上没有CSP限制，每次收到请求时直接拉一次最新数据，没有这个中间
同步环节。

Python 3.9+，只用标准库。
"""
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CST = timezone(timedelta(hours=8))
MARKET_DATA_JSON_URL = (
    "https://raw.githubusercontent.com/brycehu129/a-share-alpha-agent"
    "/market-data/dashboard/latest.json"
)
CACHE_TTL_SECONDS = 30
MAX_RESPONSE_BYTES = 5_000_000


class DashboardFetchError(RuntimeError):
    """拉取或解析 dashboard/latest.json 失败。"""


_cache_lock = threading.Lock()
_cache = {"data": None, "fetched_at": None}


def _fetch_latest_json(url=None, timeout=15):
    url = url or MARKET_DATA_JSON_URL
    request = Request(url, headers={"Cache-Control": "no-cache"})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES)
    except HTTPError as exc:
        raise DashboardFetchError(f"GitHub返回HTTP错误: {exc.code} {exc.reason}") from exc
    except URLError as exc:
        raise DashboardFetchError(f"无法连接GitHub: {exc.reason}") from exc
    except OSError as exc:
        # 超时/连接被重置等，urllib不总是包装成URLError。
        raise DashboardFetchError(f"网络错误: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise DashboardFetchError("dashboard/latest.json 不是合法JSON") from exc


def get_dashboard_data(force_refresh=False, url=None):
    """带30秒缓存地拉取最新看板数据。

    返回 (data, fetched_at_epoch, is_stale, error_message)：
    - 缓存命中：(cached_data, cached_fetched_at, False, None)
    - 拉取成功：(new_data, now, False, None)
    - 拉取失败但有旧缓存：(cached_data, cached_fetched_at, True, 本次的错误信息)
    - 拉取失败且没有任何缓存：(None, None, False, 错误信息)
    """
    with _cache_lock:
        fresh_enough = (
            not force_refresh
            and _cache["data"] is not None
            and _cache["fetched_at"] is not None
            and (time.time() - _cache["fetched_at"]) < CACHE_TTL_SECONDS
        )
        if fresh_enough:
            return _cache["data"], _cache["fetched_at"], False, None

    try:
        data = _fetch_latest_json(url=url)
    except DashboardFetchError as exc:
        with _cache_lock:
            if _cache["data"] is not None:
                return _cache["data"], _cache["fetched_at"], True, str(exc)
        return None, None, False, str(exc)

    fetched_at = time.time()
    with _cache_lock:
        _cache["data"] = data
        _cache["fetched_at"] = fetched_at
    return data, fetched_at, False, None


def iso_cst(epoch_seconds):
    return datetime.fromtimestamp(epoch_seconds, CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")


_iso = iso_cst
