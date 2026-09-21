"""按代码 / 名称 / 拼音首字母查沪深A股（腾讯 smartbox，免 token）。Python 3.9+，只用标准库。

给「加入自选」的输入框用：输入 `600519`、`茅台` 或 `gzmt` 都能查到，名称由这里带出来，不用手敲。
只认沪深个股（类型以 GP-A 开头）：ETF、指数、港美股、北交所都过滤掉——本项目的行情、日线、行业链路只覆盖沪深A股。
"""
import json
import re
from urllib.parse import quote

from daily_data import request

ENDPOINT = 'https://smartbox.gtimg.cn/s3/?v=2&t=all&q='
HINT_RE = re.compile(r'v_hint="(.*)"', re.S)
MAX_QUERY = 20


class SearchError(ValueError):
    """查询失败（网络或响应格式）。和"没有结果"是两回事，调用方要能区分。"""


def _unescape(text):
    """smartbox 把中文写成 \\uXXXX 转义。"""
    try:
        return json.loads('"%s"' % text)
    except ValueError:
        return text


def parse(raw):
    """响应文本 → [{'symbol','code','name','pinyin','board'}]。没有结果时源站返回 v_hint="N"。"""
    match = HINT_RE.search(raw)
    if not match:
        raise SearchError('搜索接口返回格式异常')
    body = match.group(1)
    if body in ('N', ''):
        return []
    out = []
    for item in body.split('^'):
        fields = item.split('~')
        if len(fields) < 5:
            continue
        market, code, name, pinyin, kind = fields[:5]
        if market not in ('sh', 'sz') or not re.fullmatch(r'\d{6}', code) or not kind.startswith('GP-A'):
            continue
        out.append({'symbol': market + code, 'code': code, 'name': _unescape(name), 'pinyin': pinyin.upper(),
                    'board': {'GP-A-CYB': '创业板', 'GP-A-KCB': '科创板'}.get(kind, '')})
    return out


def search(query, limit=8, fetch=None):
    query = (query or '').strip()
    if not query:
        return []
    if len(query) > MAX_QUERY:
        raise SearchError('搜索词太长')
    try:
        raw = (fetch or request)(ENDPOINT + quote(query)).decode('utf-8', 'replace')
    except (OSError, ValueError) as exc:
        raise SearchError('搜索接口请求失败：%s' % type(exc).__name__) from exc
    return parse(raw)[:limit]


def resolve(symbol, fetch=None):
    """规范化后的代码（如 sh600519）→ 股票信息；查无此股返回 None（区别于 SearchError）。"""
    hits = search(symbol[2:], fetch=fetch)
    return next((h for h in hits if h['symbol'] == symbol), None)
