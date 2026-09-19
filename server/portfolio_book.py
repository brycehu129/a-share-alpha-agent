"""真实持仓与自选股的本地账本（手工录入）。Python 3.9+，只用标准库。

**这里存的是真金白银的数据**（成本价、持仓量），所以有两条硬规则：

1. 只存在服务器本地 `server/data/private/`，**绝不进 git**。本仓库是公开仓库，
   `.history` 会被推到公开的 market-data 分支——持仓数据进去就等于公开了自己的
   账户。`.gitignore` 里已把 `server/data/` 排除。
2. 原子写（tmp + rename），和 wecom_push.save_config 一样：写一半断电也不会留下
   半个 JSON 把账本弄坏。

和 alpha_portfolio.py 的虚拟账户完全无关，两者不共享状态也不互相影响：那个是策略
的模拟账本，这个是用户自己报给系统的真实持仓，只用于分析和提醒，系统永远不下单。
"""
import json
import os
import re
import tempfile
from datetime import date, datetime

from collect_quotes import CST

SYMBOL_RE = re.compile(r'(sh|sz)\d{6}')
INTENTS = ('buy', 'watch', 'sell')
INTENT_LABEL = {'buy': '想买入', 'watch': '观察', 'sell': '想卖出'}
MAX_ENTRIES = 200


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


def _positive(raw, field):
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise BookError(field + ' 必须是数字') from exc
    if not value > 0 or value != value or value in (float('inf'), float('-inf')):
        raise BookError(field + ' 必须大于 0')
    return value


def _lot_size(symbol):
    """科创板最小申报数量是 200 股，其余 A 股 100 股。"""
    return 200 if symbol.startswith('sh688') else 100


def validate_holding(form):
    symbol = normalize_symbol(form.get('symbol'))
    shares = _positive(form.get('shares'), '持仓数量')
    if shares != int(shares):
        raise BookError('持仓数量必须是整数股')
    shares = int(shares)
    lot = _lot_size(symbol)
    if shares % lot:
        # 拒绝而不是四舍五入：数量错了会让后面算出来的盈亏和仓位全错。
        raise BookError('持仓数量必须是 %d 股的整数倍（%s）' % (lot, symbol))
    opened_on = (form.get('opened_on') or '').strip()
    if opened_on:
        try:
            parsed = date.fromisoformat(opened_on)
        except ValueError as exc:
            raise BookError('建仓日期格式应为 YYYY-MM-DD') from exc
        if parsed > datetime.now(CST).date():
            raise BookError('建仓日期不能是未来')
        opened_on = parsed.isoformat()
    return {'symbol': symbol, 'name': (form.get('name') or '').strip()[:20],
            'shares': shares, 'cost_price': round(_positive(form.get('cost_price'), '成本价'), 4),
            'opened_on': opened_on or None, 'note': (form.get('note') or '').strip()[:200],
            'updated_at': datetime.now(CST).isoformat()}


def validate_watch(form):
    intent = (form.get('intent') or 'watch').strip()
    if intent not in INTENTS:
        raise BookError('关注类型只能是 ' + '/'.join(INTENTS))
    return {'symbol': normalize_symbol(form.get('symbol')),
            'name': (form.get('name') or '').strip()[:20], 'intent': intent,
            'note': (form.get('note') or '').strip()[:200],
            'added_on': datetime.now(CST).date().isoformat(),
            'updated_at': datetime.now(CST).isoformat()}


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


def add_holding(form, directory=None):
    return upsert('holdings', validate_holding(form), directory)


def add_watch(form, directory=None):
    return upsert('watchlist', validate_watch(form), directory)


def all_symbols(directory=None):
    """持仓 + 自选，去重后按代码排序。给实时快照当输入用。"""
    symbols = {r['symbol'] for r in load('holdings', directory)}
    symbols.update(r['symbol'] for r in load('watchlist', directory))
    return sorted(symbols)
