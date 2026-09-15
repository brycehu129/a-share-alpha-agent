"""Source-labelled industry mapping and conservative research exclusions."""
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from datetime import date
from collections import defaultdict
from decimal import Decimal
from urllib.parse import urlencode


def parse_industry_rows(rows, total):
    if len(rows) != total:
        raise ValueError('东方财富分页数与总数不一致')
    seen, groups = set(), defaultdict(list)
    for row in rows:
        code, market = str(row['f12']), row['f13']
        if market not in (0, 1) or not re.fullmatch(r'\d{6}', code):
            raise ValueError('东方财富代码或市场异常')
        symbol = ('sh' if market == 1 else 'sz') + code
        if symbol in seen:
            raise ValueError('东方财富清单重复代码')
        seen.add(symbol)
        industry = row.get('f100')
        if isinstance(industry, str) and industry.strip() not in ('', '-', '--'):
            groups[industry.strip()].append(symbol)
    if not groups:
        raise ValueError('响应未提供有效f100行业字段')
    return [{'node': 'em:' + name, 'name': name, 'symbols': symbols,
             'expected': len(symbols), 'status': 'success'} for name, symbols in sorted(groups.items())]


def eastmoney_industries(get):
    base = 'https://push2.eastmoney.com/api/qt/clist/get?'
    params = {'pz': 100, 'po': 1, 'np': 1, 'fltt': 2, 'invt': 2, 'fid': 'f12',
              'fs': 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23', 'fields': 'f12,f13,f14,f100'}
    first = json.loads(get(base + urlencode({**params, 'pn': 1})))['data']
    total = first['total']
    if not isinstance(total, int) or not 1 <= total <= 10000:
        raise ValueError('东方财富总数异常')
    rows = first['diff']
    if not isinstance(rows, list) or not any(r.get('f100') not in (None, '', '-') for r in rows):
        raise ValueError('东方财富第一页无有效行业字段')
    for page in range(2, math.ceil(total / 100) + 1):
        body = json.loads(get(base + urlencode({**params, 'pn': page})))['data']
        if body['total'] != total or not isinstance(body['diff'], list):
            raise ValueError('东方财富分页发生变化')
        rows.extend(body['diff'])
    return parse_industry_rows(rows, total)


def exclude_reason(member, quote, quote_date):
    names = [member.get('name', ''), (quote or {}).get('name', '')]
    if any('ST' in re.sub(r'\s+', '', n).upper() for n in names):
        return 'ST或*ST名称标记'
    if any('退' in n for n in names):
        return '退市相关名称标记'
    if quote is None:
        return '缺少报价'
    if quote_date is None or quote['quote_at'][:10] != quote_date:
        return '报价日期不一致或未知'
    try:
        prices = [Decimal(quote[key]) for key in ('open', 'last', 'previous_close')]
        if any(not p.is_finite() or p <= 0 for p in prices):
            return '开盘或价格无效，交易状态待核验'
    except (KeyError, ArithmeticError):
        return '价格字段异常'
    return None  # Research eligible only; never equivalent to tradable.


def baostock_industries(today):
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('baostock_industry.py'))],
                            capture_output=True, text=True, timeout=65, check=True)
    payload = json.loads(result.stdout)
    seen, groups, dates, classifications = set(), defaultdict(list), set(), set()
    for row in payload['rows']:
        symbol = row['code'].replace('.', '')
        if not re.fullmatch(r'(sh6\d{5}|sz[03]\d{5})', symbol):
            continue
        if symbol in seen:
            raise ValueError('BaoStock duplicate code')
        seen.add(symbol)
        industry, classification = row['industry'], row['industryClassification']
        if not industry or not classification:
            continue
        updated = date.fromisoformat(row['updateDate'])
        if updated > today or (today - updated).days > 370:
            raise ValueError('BaoStock classification date out of accepted range')
        dates.add(updated.isoformat())
        classifications.add(classification)
        groups[classification + ':' + industry].append(symbol)
    if not groups or len(classifications) != 1:
        raise ValueError('BaoStock empty or mixed classification systems')
    items = [{'node': 'bs:' + name, 'name': name, 'symbols': symbols,
              'expected': len(symbols), 'status': 'success'} for name, symbols in sorted(groups.items())]
    metadata = {'provider_version': payload['provider_version'], 'update_dates': sorted(dates),
                'classification': next(iter(classifications))}
    return items, metadata
