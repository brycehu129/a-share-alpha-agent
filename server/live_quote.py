"""实时行情快照（腾讯 qt.gtimg.cn，免token，批量）。Python 3.9+，只用标准库。

和 collect_quotes.py 的区别：那个模块是"采集并落库到 sqlite"的审计型采集器，只取
4 个字段；这个模块是给盘后分析和盘中哨兵用的**只读快照**，把同一个接口返回的全部
有用字段解析出来，不落盘、不写审计记录。

腾讯同一个接口同时支持 A股 / 港股 / 美股，但**三者字段数和时间格式都不一样**：

  sh/sz 个股   88 字段   时间 20260918161436        涨停价(47)/跌停价(48) 是真实价格
  sh/sz 指数   88 字段   时间 同上                   涨停价/跌停价 返回 -1（指数无涨跌停）
  hk           78 字段   时间 2026/09/18 18:31:31   无换手率/涨跌停等 A 股特有字段
  us           73 字段   时间 2026-09-18 17:52:27   同上

只有下标 1/2/3/4/5/30/31/32/33/34 在三者之间是通用的，其余必须按市场分支取，
否则会把港股的某个字段当成 A 股的换手率用。

美股时间戳的时区腾讯没有明确说明（既不是北京时间也不像标准收盘时间），所以本模块
**不对美股计算延迟秒数**，只保留原始字符串和日期，并标注时区未核验——外围市场对
盘后分析来说只需要"昨夜涨跌多少"，不需要精确到秒的新鲜度。
"""
import argparse
import hashlib
import json
import re
import time
from datetime import datetime, time as clock_time
from decimal import Decimal, InvalidOperation

from collect_quotes import CST
from daily_data import request

ENDPOINT = 'https://qt.gtimg.cn/q='
BATCH_SIZE = 50
STALE_SECONDS = 900

# 各市场字段数的下限。腾讯偶尔会加字段（只会变多不会变少），所以用 >= 而不是 ==。
MARKET_MIN_FIELDS = {'cn': 88, 'hk': 78, 'us': 73}

# 三个市场通用的字段下标。
COMMON = {'name': 1, 'code': 2, 'last': 3, 'previous_close': 4, 'open': 5,
          'quote_time': 30, 'change': 31, 'change_pct': 32, 'high': 33, 'low': 34}

# 只有 A 股（含 A 股指数）才有的字段下标。
CN_EXTRA = {'volume_raw': 36, 'amount_wan': 37, 'turnover_pct': 38, 'pe': 39,
            'amplitude_pct': 43, 'float_cap_yi': 44, 'total_cap_yi': 45, 'pb': 46,
            'limit_up': 47, 'limit_down': 48, 'volume_ratio': 49}


class QuoteError(ValueError):
    """行情解析失败。绝不返回半截数据冒充有效报价。"""


def market_of(symbol):
    if re.fullmatch(r'(sh|sz)\d{6}', symbol):
        return 'cn'
    if re.fullmatch(r'hk\w{2,8}', symbol):
        return 'hk'
    if re.fullmatch(r'us[.\w]{1,10}', symbol):
        return 'us'
    raise QuoteError('不支持的代码格式: ' + symbol)


def _number(fields, index, symbol, allow_missing=False):
    """把字段取成 Decimal。空串和无法解析的值按缺失处理（返回 None），
    而不是悄悄变成 0——0 在价格和成交量上都是有含义的值。"""
    raw = fields[index] if index < len(fields) else ''
    if raw in ('', '-', 'null'):
        if allow_missing:
            return None
        raise QuoteError('字段缺失(下标%d): %s' % (index, symbol))
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        if allow_missing:
            return None
        raise QuoteError('字段非数值(下标%d): %s' % (index, symbol)) from exc
    if not value.is_finite():
        if allow_missing:
            return None
        raise QuoteError('字段非有限值(下标%d): %s' % (index, symbol))
    return value


def parse_quote_time(raw, market, symbol):
    """返回 (datetime 或 None, 原始字符串)。美股返回 None，因为时区未核验。"""
    raw = (raw or '').strip()
    if market == 'cn':
        if not re.fullmatch(r'\d{14}', raw):
            raise QuoteError('行情时间字段异常: ' + symbol)
        return datetime.strptime(raw, '%Y%m%d%H%M%S').replace(tzinfo=CST), raw
    if market == 'hk':
        # 港股时间就是香港时间，和北京时间同为 UTC+8，可以直接比。
        try:
            return datetime.strptime(raw, '%Y/%m/%d %H:%M:%S').replace(tzinfo=CST), raw
        except ValueError as exc:
            raise QuoteError('港股行情时间异常: ' + symbol) from exc
    # 美股：只校验形状，不赋时区，也不算延迟。
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', raw):
        raise QuoteError('美股行情时间异常: ' + symbol)
    return None, raw


def parse_one(body, symbol, fetched_at):
    market = market_of(symbol)
    fields = body.split('~')
    if len(fields) < MARKET_MIN_FIELDS[market]:
        raise QuoteError('字段数不足(%d): %s' % (len(fields), symbol))
    if not fields[COMMON['name']]:
        raise QuoteError('名称缺失: ' + symbol)
    if market == 'cn' and fields[COMMON['code']] != symbol[2:]:
        raise QuoteError('响应代码与请求不匹配: ' + symbol)

    last = _number(fields, COMMON['last'], symbol)
    previous = _number(fields, COMMON['previous_close'], symbol)
    if last <= 0 or previous <= 0:
        raise QuoteError('最新价或昨收无有效值: ' + symbol)

    quote_at, quote_at_raw = parse_quote_time(fields[COMMON['quote_time']], market, symbol)
    age = None
    if quote_at is not None:
        age = (fetched_at - quote_at).total_seconds()
        if age < -300:
            raise QuoteError('行情时间超前超过5分钟，请检查服务器时钟: ' + symbol)

    optional = {k: _number(fields, COMMON[k], symbol, allow_missing=True)
                for k in ('open', 'high', 'low')}
    quote = {
        'symbol': symbol, 'market': market, 'name': fields[COMMON['name']],
        'last': str(last), 'previous_close': str(previous),
        # None 和 0 必须区分：None 是"源没给这个字段"，0 是"源说成交价为0"（无效行情）。
        **{k: (str(v) if v is not None else None) for k, v in optional.items()},
        # 涨跌幅用昨收自己算，不直接信 f[32]——口径一致比省几行代码重要。
        'change_pct': str(((last / previous - 1) * 100).quantize(Decimal('0.01'))),
        'quote_at': quote_at.isoformat() if quote_at else None,
        'quote_at_raw': quote_at_raw,
        'quote_date': quote_at.date().isoformat() if quote_at else quote_at_raw[:10],
        'age_seconds': round(age) if age is not None else None,
        'timezone_verified': market != 'us',
    }
    if market != 'cn':
        return quote

    for key, index in CN_EXTRA.items():
        value = _number(fields, index, symbol, allow_missing=True)
        quote[key] = str(value) if value is not None else None
    # 指数没有涨跌停，腾讯返回 -1；不能当成 -1 元的限价。
    for key in ('limit_up', 'limit_down'):
        if quote[key] is not None and Decimal(quote[key]) <= 0:
            quote[key] = None
    # 按代码前缀判定，不靠"有没有涨跌停价"——sz000001(平安银行) 和 sh000001(上证指数)
    # 前三位相同，只有前缀+市场能分开：沪市股票是 6xxxxx/688xxx，sh000/sh880 必是指数；
    # 深市 sz399 必是指数，sz000/001/002/003 是股票。
    quote['is_index'] = symbol.startswith(('sh000', 'sh880', 'sz399'))
    # 成交量单位不统一：科创板(sh688)返回"股"，其余沪深个股返回"手"(100股)。实测
    # sh688061 累计量 2500240 对应成交额 1.13 亿、价格 45 元——只可能是股。字段原先
    # 叫 volume_hand，对科创板是错的；现在叫 volume_raw 并显式标注单位。指数的成交量
    # 单位另有口径，这里不断言。
    quote['volume_unit'] = None if quote['is_index'] else ('share' if symbol.startswith('sh688') else 'hand')
    return quote


def parse_batch(raw, symbols, fetched_at):
    """返回 (quotes, failures)。逐只记录失败原因，不因为一只失败丢掉整批。"""
    text = raw.decode('gb18030', errors='strict')
    bodies = {}
    for name, body in re.findall(r'v_([A-Za-z0-9.]+)="([^"\r\n]*)"\s*;', text):
        if name in bodies:
            raise QuoteError('响应中代码重复: ' + name)
        bodies[name] = body
    quotes, failures = [], []
    for symbol in symbols:
        body = bodies.get(symbol)
        if body is None:
            failures.append({'symbol': symbol, 'reason': '响应中缺少该代码'})
            continue
        try:
            quotes.append(parse_one(body, symbol, fetched_at))
        except QuoteError as exc:
            failures.append({'symbol': symbol, 'reason': str(exc)})
    return quotes, failures


def clock_session(now=None):
    """只按时钟判断所处时段，**不核验今天是否交易日**——交易日状态由调用方用
    session_brief.calendar_state() 叠加。周末直接返回 weekend 是纯日历事实，
    不涉及节假日判断。"""
    now = now or datetime.now(CST)
    if now.weekday() >= 5:
        return 'weekend'
    t = now.time()
    if t < clock_time(9, 15):
        return 'pre_open'
    if t < clock_time(9, 30):
        return 'call_auction'
    if t < clock_time(11, 30):
        return 'morning'
    if t < clock_time(13, 0):
        return 'lunch_break'
    if t < clock_time(15, 0):
        return 'afternoon'
    if t < clock_time(15, 30):
        return 'closing'
    return 'post_close'


SESSION_LABEL = {'weekend': '周末休市', 'pre_open': '盘前', 'call_auction': '集合竞价',
                 'morning': '上午盘中', 'lunch_break': '午间休市', 'afternoon': '下午盘中',
                 'closing': '收盘处理', 'post_close': '盘后'}


def snapshot(symbols, now=None, batch_size=BATCH_SIZE, pause=0.2):
    """批量取实时快照。返回 {'fetched_at','session','quotes','failures','requests'}。

    分批请求意味着这**不是同一时刻的截面**，批次之间可能相差几秒——盘中用的时候
    要注意这一点，输出里保留每批的请求时间。"""
    now = now or datetime.now(CST)
    symbols = list(dict.fromkeys(symbols))
    result = {'fetched_at': now.isoformat(), 'session': clock_session(now),
              'quotes': [], 'failures': [], 'requests': []}
    for offset in range(0, len(symbols), batch_size):
        batch = symbols[offset:offset + batch_size]
        if offset:
            time.sleep(pause)
        url = ENDPOINT + ','.join(batch)
        started = datetime.now(CST)
        try:
            raw = request(url)
            batch_hash = hashlib.sha256(raw).hexdigest()
            quotes, failures = parse_batch(raw, batch, datetime.now(CST))
            for q in quotes:
                # 事件要能追溯到"哪一次响应"：同批报价共用一个哈希与采集时刻。
                q['batch_sha256'] = batch_hash
                q['batch_fetched_at'] = started.isoformat()
            result['quotes'].extend(quotes)
            result['failures'].extend(failures)
            status = 'success' if not failures else 'partial'
        except (OSError, ValueError, ArithmeticError) as exc:
            result['failures'].extend({'symbol': s, 'reason': str(exc)[:200]} for s in batch)
            status = 'failed'
        result['requests'].append({'count': len(batch), 'fetched_at': started.isoformat(), 'status': status})
    stale = [q['symbol'] for q in result['quotes']
             if q['age_seconds'] is not None and q['age_seconds'] > STALE_SECONDS]
    result['stale_symbols'] = stale
    result['status'] = 'success' if not result['failures'] else ('partial' if result['quotes'] else 'failed')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--symbols', required=True, help='逗号分隔，如 sh600519,sz000001,hkHSI,usDJI')
    p.add_argument('--json', action='store_true', help='输出完整JSON而不是人读表格')
    a = p.parse_args()
    data = snapshot([s.strip() for s in a.symbols.split(',') if s.strip()])
    if a.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print('采集时间 %s · 时段 %s · 状态 %s' % (
        data['fetched_at'], SESSION_LABEL.get(data['session'], data['session']), data['status']))
    for q in data['quotes']:
        extra = ''
        if q['market'] == 'cn':
            extra = '  量比 %s  换手 %s%%  振幅 %s%%' % (
                q.get('volume_ratio') or '-', q.get('turnover_pct') or '-', q.get('amplitude_pct') or '-')
        age = '延迟%ss' % q['age_seconds'] if q['age_seconds'] is not None else '时区未核验'
        print('%-9s %-10s 最新 %10s  涨跌 %7s%%  %s%s' % (
            q['symbol'], q['name'], q['last'], q['change_pct'], age, extra))
    for f in data['failures']:
        print('失败 %s: %s' % (f['symbol'], f['reason']))
    if data['stale_symbols']:
        print('提示：%s 的报价距采集时间超过15分钟，可能已收盘、停牌或数据延迟。'
              % ','.join(data['stale_symbols']))
    return 0 if data['status'] == 'success' else 1


if __name__ == '__main__':
    raise SystemExit(main())
