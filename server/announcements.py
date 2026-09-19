"""个股公告的盘前核查：候选出来之后、冻结计划之前，看一眼**晚间到清晨发布的公告**。Python 3.9+，只用标准库。

**为什么。** 选股用的是昨天收盘的日线，可这一夜可能出了公告：减持、立案调查、业绩预亏、停牌……
这些不会体现在昨天的 K 线里，却会决定今天开盘是跳水还是照常。所以选股改在盘前（08:40）做，
并且在冻结计划之前对候选逐只核查夜间公告。

**数据源与局限（必须说清楚）：**
- 东方财富公告接口（`np-anotice-stock.eastmoney.com`），免费、免 token，**非官方、可能变动或限流**。
  每只股票一次请求，带发布时刻（精确到秒）、标题、分类。
- **只看公告，不是新闻。** 传闻、研报、行业消息、盘前的集合竞价信息都不在这里；更"近实时"的消息面（财联社等）是后续计划。
- **关键词分类很粗**：按标题里的字判断，会漏（标题不含关键词的坏消息）也会错杀（比如"关于不存在减持的说明"）。
  所以只有命中**高风险**关键词才剔除，其余只标注让你看到标题。
- **取不到就明说，不装作没事**：接口失败/超时的股票标 `unverified`（未核验），计划里和推送里都会写"公告未核验"，
  由你决定要不要信；它不会被剔除，也不会被当作"已确认无风险"。
"""
import json
import re
import time
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from collect_quotes import CST

ENDPOINT = 'https://np-anotice-stock.eastmoney.com/api/security/ann?'
PAGE_SIZE = 30
TOTAL_BUDGET_S = 60          # 整批核查的时间预算；用完剩下的标 unverified，不拖住必需的选股流程
MAX_TITLES = 6

# 命中即剔除：出了这类公告，今天开盘的方向和风险都不是昨天的 K 线能反映的。
BLOCK = [
    ('立案', '被立案调查'), ('涉嫌', '涉嫌违法违规'), ('行政处罚', '行政处罚'), ('处罚决定', '行政处罚'),
    ('退市', '退市风险'), ('终止上市', '终止上市'), ('暂停上市', '暂停上市'), ('风险警示', '被实施风险警示(ST)'),
    ('停牌', '停牌'), ('破产', '破产'), ('违约', '债务违约'), ('资金占用', '资金占用'), ('财务造假', '财务造假'),
    ('无法表示意见', '审计意见异常'), ('否定意见', '审计意见异常'), ('司法冻结', '股份被冻结'), ('被冻结', '股份被冻结'),
    ('预亏', '业绩预亏'), ('预减', '业绩预减'), ('续亏', '业绩续亏'), ('首亏', '业绩首亏'), ('由盈转亏', '业绩由盈转亏'),
    ('业绩大幅下降', '业绩大幅下降'), ('业绩下滑', '业绩下滑'),
    ('减持', '股东/高管减持'),
]
# 只标注不剔除：可能有影响，但要人来判断。
FLAG = [
    ('问询函', '收到问询函'), ('关注函', '收到关注函'), ('监管函', '收到监管函'), ('警示函', '收到警示函'),
    ('异常波动', '股价异常波动'), ('澄清', '澄清公告'), ('风险提示', '风险提示'), ('重大资产重组', '重大资产重组'),
    ('筹划', '筹划事项'), ('解禁', '限售股解禁'), ('限售股', '限售股流通'), ('质押', '股份质押'),
    ('诉讼', '诉讼'), ('仲裁', '仲裁'), ('辞职', '高管辞职'), ('变更董事长', '董事长变更'), ('变更法定代表人', '法定代表人变更'),
]
# 这些字样会让上面的关键词失效（"不减持承诺""减持完毕""复牌""澄清不存在…"等）——出现就只算标注，不剔除。
NEGATING = ('不减持', '不存在减持', '减持完毕', '减持计划实施完毕', '提前终止减持', '复牌', '未收到', '不存在')


class AnnouncementError(RuntimeError):
    pass


def classify(title):
    """返回 (level, reason)：block / flag / info。"""
    for word, reason in BLOCK:
        if word in title:
            if any(neg in title for neg in NEGATING):
                return 'flag', reason + '（标题含否定/已完成字样，需人工看）'
            return 'block', reason
    for word, reason in FLAG:
        if word in title:
            return 'flag', reason
    return 'info', ''


def _parse_time(text):
    return datetime.strptime(text[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=CST)


def fetch(symbol, timeout=8):
    """一只股票最近一页公告：[{'time': datetime, 'title': str, 'columns': [...]}]，新的在前。失败抛 AnnouncementError。"""
    code = symbol[2:]
    url = ENDPOINT + urlencode({'sr': -1, 'page_size': PAGE_SIZE, 'page_index': 1, 'ann_type': 'A', 'client_source': 'web',
                                'stock_list': code, 'f_node': 0, 's_node': 0})
    try:
        request = Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read(2_000_000).decode('utf-8'))
        rows = body['data']['list']
        return [{'time': _parse_time(r['display_time']), 'title': r['title_ch'] or r['title'],
                 'columns': [c.get('column_name', '') for c in r.get('columns', [])]} for r in rows]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AnnouncementError('%s: %s' % (type(exc).__name__, str(exc)[:80])) from exc


def check_symbol(symbol, since, now, fetch_fn=fetch):
    """核查一只：since 之后发布的公告。返回 {'status', 'items', 'error'}。"""
    if not re.fullmatch(r'(sh|sz)\d{6}', symbol):
        return {'status': 'unverified', 'items': [], 'error': '代码格式不支持'}
    try:
        rows = fetch_fn(symbol)
    except AnnouncementError as exc:
        return {'status': 'unverified', 'items': [], 'error': str(exc)}
    items = []
    for row in rows:
        if row['time'] <= since:
            continue
        level, reason = classify(row['title'])
        items.append({'time': row['time'].isoformat(), 'title': row['title'][:120], 'level': level, 'reason': reason,
                      'columns': row['columns'][:2]})
    levels = {i['level'] for i in items}
    status = 'block' if 'block' in levels else 'flag' if 'flag' in levels else 'clear'
    # 重要的排前面，标题只留几条：推送和页面都要读得动
    items.sort(key=lambda i: ({'block': 0, 'flag': 1, 'info': 2}[i['level']], i['time']))
    return {'status': status, 'items': items[:MAX_TITLES], 'total_items': len(items), 'error': None}


def check_many(symbols, since, now, fetch_fn=fetch, sleep=time.sleep, monotonic=time.monotonic, delay=0.15):
    """逐只核查，整批有时间预算：超时后剩下的标 unverified（明说"没查"，不是"没事"）。返回 {symbol: 结果}。"""
    started = monotonic()
    out = {}
    for symbol in symbols:
        if monotonic() - started > TOTAL_BUDGET_S:
            out[symbol] = {'status': 'unverified', 'items': [], 'error': '整批核查超出时间预算'}
            continue
        out[symbol] = check_symbol(symbol, since, now, fetch_fn)
        sleep(delay)
    return out
