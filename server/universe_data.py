"""Provider-list coverage and bounded Shanghai/Shenzhen quote collection."""
import hashlib
import html
import json
import math
import re
import time
from collections import Counter
from datetime import datetime
from urllib.parse import urlencode

from collect_quotes import CST, parse_quotes
from daily_data import request

BASE = 'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'


def validate_members(rows, expected):
    if not isinstance(rows, list) or len(rows) != expected:
        raise ValueError('分页条数与源总数不符')
    seen, members, excluded = set(), [], []
    for row in rows:
        symbol = row.get('symbol', '')
        if symbol in seen:
            raise ValueError('清单重复代码: ' + symbol)
        seen.add(symbol)
        if not re.fullmatch(r'(sh|sz|bj)\d{6}', symbol) or not row.get('name'):
            raise ValueError('清单代码或名称异常')
        if re.fullmatch(r'(sh6\d{5}|sz[03]\d{5})', symbol):
            members.append({'symbol': symbol, 'name': row['name']})
        else:
            excluded.append(symbol)
    if not members:
        raise ValueError('没有沪深A股成员')
    return members, excluded


def parse_batch(raw, symbols, now):
    quotes, failures = [], []
    for symbol in symbols:
        try:
            q = parse_quotes(raw, [symbol], now)[0]
            q.pop('raw_fields')
            quotes.append(q)
        except (ValueError, ArithmeticError) as exc:
            failures.append({'symbol': symbol, 'reason': str(exc)})
    return quotes, failures


def collect_universe():
    started = time.monotonic()
    data = {'version': 'universe-0.4', 'started_at': datetime.now(CST).isoformat(),
            'scope': '新浪hs_a清单中的沪深A股；未核验交易所完整清单，北交所不在报价范围',
            'status': 'partial', 'members': [], 'excluded': [], 'quotes': [],
            'failures': [], 'requests': [], 'errors': [], 'list_verified': False}

    def get(url):
        if time.monotonic() - started > 300:
            raise TimeoutError('本轮300秒请求预算已用尽')
        raw = request(url)
        data['requests'].append({'url': url, 'fetched_at': datetime.now(CST).isoformat(),
                                 'sha256': hashlib.sha256(raw).hexdigest()})
        time.sleep(0.2)
        return raw

    try:
        count_url = BASE + 'Market_Center.getHQNodeStockCount?node=hs_a'
        count = int(json.loads(get(count_url)))
        if not 1 <= count <= 10000:
            raise ValueError('源清单条数超出边界')
        data['source_count'] = count
        rows = []
        for page in range(1, math.ceil(count / 80) + 1):
            url = BASE + 'Market_Center.getHQNodeData?' + urlencode({
                'page': page, 'num': 80, 'sort': 'symbol', 'asc': 1,
                'node': 'hs_a', 'symbol': '', '_s_r_a': 'page'})
            part = json.loads(get(url))
            if not isinstance(part, list):
                raise ValueError('分页返回非列表')
            rows.extend(part)
        after = int(json.loads(get(count_url)))
        if after != count:
            raise ValueError('采集期间清单数量发生变化，请重跑')
        data['members'], data['excluded'] = validate_members(rows, count)
        data['list_verified'] = True
        symbols = [r['symbol'] for r in data['members']]
        for offset in range(0, len(symbols), 50):
            batch = symbols[offset:offset + 50]
            try:
                raw = get('https://qt.gtimg.cn/q=' + ','.join(batch))
                quotes, failures = parse_batch(raw, batch, datetime.now(CST))
                data['quotes'].extend(quotes)
                data['failures'].extend(failures)
            except (OSError, ValueError, ArithmeticError) as exc:
                data['failures'].extend({'symbol': s, 'reason': str(exc)} for s in batch)
        data['status'] = 'success' if not data['failures'] else 'partial'
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        data['errors'].append(str(exc))
    data['finished_at'] = datetime.now(CST).isoformat()
    data['quote_date_counts'] = dict(Counter(q['quote_at'][:10] for q in data['quotes']))
    data['coverage_pct'] = round(100 * len(data['quotes']) / len(data['members']), 2) if data['members'] else 0
    return data


def universe_markdown(data):
    esc = lambda s: html.escape(str(s)).replace('|', '&#124;').replace('\n', ' ')
    lines = ['', '## 沪深股票清单与批量行情', '',
             '覆盖口径：' + data['scope'] + '。', '',
             '| 检查项 | 结果 |', '|---|---:|',
             f'| 源清单总数 | {data.get("source_count", "未知")} |',
             f'| 分页数量及去重校验 | {data["list_verified"]} |',
             f'| 纳入沪深股票 | {len(data["members"])} |',
             f'| 范围外代码 | {len(data["excluded"])} |',
             f'| 有效报价 | {len(data["quotes"])} |',
             f'| 无有效报价 | {len(data["failures"])} |',
             f'| 清单内报价覆盖率 | {data["coverage_pct"]}% |',
             f'| 采集状态 | {data["status"]} |', '',
             f'采集区间（北京时间）：{data["started_at"]} → {data["finished_at"]}。', '',
             '行情分批获取，不是同一时刻快照。报价覆盖率不代表可交易比例；停牌、ST、退市、涨跌停与上市日期尚未过滤。', '',
             '### 报价日期分布', '', '| 源报价日期 | 股票数 |', '|---|---:|']
    lines += [f'| {esc(day)} | {count} |' for day, count in sorted(data['quote_date_counts'].items())]
    lines += ['', '股票清单、逐股报价、失败原因、请求来源与摘要保存在同编号JSON的 universe 字段。当前清单不是历史成分库，不能直接用于无幸存者偏差回测。', '',
              '不根据这一次快照生成主线、胜率或推荐股票；行业映射、全市场日线与质量过滤仍待接入。', '']
    if data['errors']:
        lines += ['- ' + esc(e) for e in data['errors']]
    if data['failures']:
        lines += ['', '前10项无有效报价记录：', '']
        lines += ['- ' + esc(f['symbol'] + ': ' + f['reason']) for f in data['failures'][:10]]
    return '\n'.join(lines) + '\n'
