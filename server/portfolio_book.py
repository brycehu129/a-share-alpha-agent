"""真实持仓与自选股的本地账本（手工录入）。Python 3.9+，只用标准库。

**这里存的是真金白银的数据**（成本价、持仓量），所以有两条硬规则：

1. 只存在服务器本地 `server/data/private/`，**绝不进 git**。本仓库是公开仓库，
   `.history` 会被推到公开的 market-data 分支——持仓数据进去就等于公开了自己的
   账户。`.gitignore` 里已把 `server/data/` 排除。
2. 原子写（tmp + rename），和 wecom_push.save_config 一样：写一半断电也不会留下
   半个 JSON 把账本弄坏。

和 alpha_portfolio.py 的虚拟账户完全无关，两者不共享状态也不互相影响：那个是策略
的模拟账本，这个是用户自己报给系统的真实持仓，只用于分析和提醒，系统永远不下单。

**录入方式照券商来：** 自选股只记「关注哪只」；持仓不能凭空录入，只能从自选股「买入」、
从持仓「卖出」，每一笔都进成交流水（trades.json）。所以：

- 成本价 = 你每次买入价的加权平均，不是手填的一个数字；卖出不改变剩余持仓的成本价，只记已实现盈亏。
- 系统按 T+1 算「可卖数量」：当天买入的股当天卖不出去，做T 的底仓只能是这部分之外的老仓。
- 止损位/止盈位/做T底仓不再由用户声明——系统按策略（ATR）和可卖数量自己算，见 book_levels.py。
"""
import json
import os
import re
import tempfile
import uuid
from datetime import date, datetime

from collect_quotes import CST

SYMBOL_RE = re.compile(r'(sh|sz)\d{6}')
MAX_ENTRIES = 200
MAX_SHARES = 100_000_000
MAX_PRICE = 100_000


class BookError(ValueError):
    """录入数据不合法。宁可拒绝，也不存一条会让后续分析算错的记录。"""


def default_dir():
    """默认和 webapp 的配置文件放在一起（Railway/VPS 上那是挂载的持久目录），
    但多一层 private/ 以示区别。"""
    config_path = os.environ.get('CONFIG_PATH') or os.path.join(
        os.path.dirname(__file__), 'data', 'webapp_config.json')
    return os.environ.get('PRIVATE_DATA_DIR') or os.path.join(
        os.path.dirname(os.path.abspath(config_path)), 'private')


def _path(kind, directory=None):
    return os.path.join(directory or default_dir(), kind + '.json')


def _today(now=None):
    return (now or datetime.now(CST)).date()


def normalize_symbol(raw):
    """接受 sh600519 / 600519 / 600519.SH / 600519.sh 几种写法，统一成 sh600519。
    只给出 6 位数字时按交易所规则推断：6 开头是沪市，0/3 开头是深市。"""
    text = (raw or '').strip().lower().replace(' ', '')
    if not text:
        raise BookError('股票代码不能为空')
    if SYMBOL_RE.fullmatch(text):
        return text
    match = re.fullmatch(r'(\d{6})\.?(sh|sz)?', text)
    if not match:
        raise BookError('无法识别的股票代码: ' + raw)
    digits, suffix = match.group(1), match.group(2)
    if suffix:
        return suffix + digits
    if digits.startswith('6'):
        return 'sh' + digits
    if digits[0] in '03':
        return 'sz' + digits
    # 8/4 开头是北交所，本项目的数据链路（清单、日线、行业）都不覆盖北交所。
    raise BookError('暂不支持该市场的代码（本项目只覆盖沪深A股）: ' + raw)


def _positive(raw, field, cap=None):
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise BookError(field + ' 必须是数字') from exc
    if not value > 0 or value != value or value in (float('inf'), float('-inf')):
        raise BookError(field + ' 必须大于 0')
    if cap is not None and value > cap:
        raise BookError('%s 超出合理范围（不超过 %s）' % (field, cap))
    return value


def lot_size(symbol):
    """科创板最小申报数量是 200 股，其余 A 股 100 股。"""
    return 200 if symbol.startswith('sh688') else 100


def _shares(raw, symbol, field):
    shares = _positive(raw, field, MAX_SHARES)
    if shares != int(shares):
        raise BookError(field + '必须是整数股')
    shares = int(shares)
    lot = lot_size(symbol)
    if shares % lot:
        # 拒绝而不是四舍五入：数量错了会让后面算出来的盈亏和仓位全错。
        raise BookError('%s必须是 %d 股的整数倍（%s）' % (field, lot, symbol))
    return shares


def _trade_date(raw, today):
    text = (raw or '').strip()
    if not text:
        return today.isoformat()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise BookError('成交日期格式应为 YYYY-MM-DD') from exc
    if parsed > today:
        raise BookError('成交日期不能是未来')
    return parsed.isoformat()


def validate_watch(form, now=None):
    return {'symbol': normalize_symbol(form.get('symbol')),
            'name': (form.get('name') or '').strip()[:20],
            'note': (form.get('note') or '').strip()[:200],
            'added_on': _today(now).isoformat(),
            'updated_at': (now or datetime.now(CST)).isoformat()}


def validate_buy(form, now=None):
    symbol = normalize_symbol(form.get('symbol'))
    return {'symbol': symbol, 'price': round(_positive(form.get('price'), '买入价', MAX_PRICE), 4),
            'shares': _shares(form.get('shares'), symbol, '买入数量'),
            'date': _trade_date(form.get('date'), _today(now)),
            'note': (form.get('note') or '').strip()[:200]}


def validate_sell(form, now=None):
    symbol = normalize_symbol(form.get('symbol'))
    price = round(_positive(form.get('price'), '卖出价', MAX_PRICE), 4)
    raw = _positive(form.get('shares'), '卖出数量', MAX_SHARES)
    if raw != int(raw):
        raise BookError('卖出数量必须是整数股')
    return {'symbol': symbol, 'price': price, 'shares': int(raw),
            'date': _trade_date(form.get('date'), _today(now)),
            'note': (form.get('note') or '').strip()[:200]}


def load(kind, directory=None):
    path = _path(kind, directory)
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise BookError('账本文件内容损坏（不是 JSON 数组）: ' + path)
    return data


def _save(kind, rows, directory=None):
    path = _path(kind, directory)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix='.tmp')
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return rows


def upsert(kind, entry, directory=None):
    """同一只股票只保留一条：再次录入就是修改，不会留下两条互相矛盾的成本价。"""
    rows = [r for r in load(kind, directory) if r.get('symbol') != entry['symbol']]
    if len(rows) >= MAX_ENTRIES:
        raise BookError('最多只能保存 %d 条记录' % MAX_ENTRIES)
    rows.append(entry)
    rows.sort(key=lambda r: r['symbol'])
    return _save(kind, rows, directory)


def remove(kind, symbol, directory=None):
    symbol = normalize_symbol(symbol)
    rows = load(kind, directory)
    kept = [r for r in rows if r.get('symbol') != symbol]
    if len(kept) == len(rows):
        raise BookError('账本里没有这只股票: ' + symbol)
    return _save(kind, kept, directory)


def add_watch(form, directory=None, now=None):
    """加入自选。已经在自选里就只更新名称/备注，保留最初加入的日期。"""
    entry = validate_watch(form, now)
    for row in load('watchlist', directory):
        if row.get('symbol') == entry['symbol']:
            entry['added_on'] = row.get('added_on') or entry['added_on']
            entry['name'] = entry['name'] or row.get('name') or ''
            if not (form.get('note') or '').strip():
                entry['note'] = row.get('note') or ''
    return upsert('watchlist', entry, directory)


# --- 成交流水与 T+1 -----------------------------------------------------------

def _append_trade(trade, directory=None):
    trades = load('trades', directory)
    trades.append(trade)
    _save('trades', trades, directory)


def bought_on(trades, symbol, day):
    """某只股票在某一天买入的股数：这部分按 T+1 当天不能卖。"""
    return sum(t['shares'] for t in trades
               if t.get('symbol') == symbol and t.get('side') == 'buy' and t.get('date') == day)


def sellable_shares(holding, trades, day):
    return max(0, int(holding['shares']) - bought_on(trades, holding['symbol'], day))


def with_sellable(holdings, trades=None, now=None, directory=None):
    """给每条持仓补上 sellable_shares（今天可卖）。返回新列表，不改账本。"""
    trades = load('trades', directory) if trades is None else trades
    day = _today(now).isoformat()
    return [{**h, 'sellable_shares': sellable_shares(h, trades, day)} for h in holdings]


def recent_trades(limit=50, directory=None):
    return list(reversed(load('trades', directory)[-limit:]))


def _new_trade(now, side, entry, name, **extra):
    return {'id': '%s-%s' % (now.strftime('%Y%m%d%H%M%S'), uuid.uuid4().hex[:4]), 'at': now.isoformat(),
            'side': side, 'symbol': entry['symbol'], 'name': name, 'price': entry['price'],
            'shares': entry['shares'], 'date': entry['date'], 'note': entry['note'], **extra}


def buy(form, directory=None, now=None):
    """从自选股买入。已持有就按加权平均更新成本价（不含费用，和页面上的盈亏口径一致）。

    先写流水、后写持仓：万一第二步失败，多出来的只是一条没有对应持仓的买入记录（无害）；
    反过来的话持仓里有一笔流水里没有的买入，T+1 的可卖数量就会多算。"""
    now = now or datetime.now(CST)
    entry = validate_buy(form, now)
    watch = next((w for w in load('watchlist', directory) if w.get('symbol') == entry['symbol']), None)
    if watch is None:
        raise BookError('买入请从自选股操作：先把 %s 加入自选股' % entry['symbol'])
    current = next((h for h in load('holdings', directory) if h.get('symbol') == entry['symbol']), None)
    if current:
        total = current['shares'] + entry['shares']
        cost = (current['cost_price'] * current['shares'] + entry['price'] * entry['shares']) / total
        opened = min(filter(None, [current.get('opened_on'), entry['date']]))
    else:
        total, cost, opened = entry['shares'], entry['price'], entry['date']
    name = (watch.get('name') or (current or {}).get('name') or '')
    holding = {'symbol': entry['symbol'], 'name': name, 'shares': total, 'cost_price': round(cost, 4),
               'opened_on': opened, 'note': (current or {}).get('note') or '',
               'updated_at': now.isoformat()}
    if not current and len(load('holdings', directory)) >= MAX_ENTRIES:
        raise BookError('最多只能保存 %d 条记录' % MAX_ENTRIES)
    _append_trade(_new_trade(now, 'buy', entry, name), directory)
    return upsert('holdings', holding, directory)


def sell(form, directory=None, now=None):
    """从持仓卖出。全部卖出就清掉这条持仓；部分卖出不改变剩余持仓的成本价，只记已实现盈亏。"""
    now = now or datetime.now(CST)
    entry = validate_sell(form, now)
    current = next((h for h in load('holdings', directory) if h.get('symbol') == entry['symbol']), None)
    if current is None:
        raise BookError('持仓里没有这只股票: ' + entry['symbol'])
    held, shares = int(current['shares']), entry['shares']
    if shares > held:
        raise BookError('卖出数量 %d 超过持仓 %d' % (shares, held))
    lot = lot_size(entry['symbol'])
    if shares % lot and shares != held:
        raise BookError('卖出数量必须是 %d 股的整数倍（全部卖出除外）' % lot)
    sellable = sellable_shares(current, load('trades', directory), entry['date'])
    if shares > sellable:
        raise BookError('T+1：当天买入的 %d 股当天不能卖，目前最多可卖 %d 股' % (held - sellable, sellable))
    cost = float(current['cost_price'])
    trade = _new_trade(now, 'sell', entry, current.get('name') or '', cost_price=cost,
                       realized_pnl=round((entry['price'] - cost) * shares, 2))
    _append_trade(trade, directory)
    if shares == held:
        return remove('holdings', entry['symbol'], directory)
    # 显式列出字段而不是 {**current}：旧版本录入的行带着 hold_type/止损/目标/做T底仓，趁这次改写把它们丢掉。
    return upsert('holdings', {'symbol': current['symbol'], 'name': current.get('name') or '', 'shares': held - shares,
                               'cost_price': cost, 'opened_on': current.get('opened_on'),
                               'note': current.get('note') or '', 'updated_at': now.isoformat()}, directory)


def all_symbols(directory=None):
    """持仓 + 自选，去重后按代码排序。给实时快照当输入用。"""
    symbols = {r['symbol'] for r in load('holdings', directory)}
    symbols.update(r['symbol'] for r in load('watchlist', directory))
    return sorted(symbols)
