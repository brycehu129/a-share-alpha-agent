"""持仓的派生状态：移动止损用的历史最高价（peak_price）、补仓次数与时间（add_count/
last_add_at）。全部可以从日线缓存 + 成交流水重新推导，丢了不心疼——所以叫"派生状态"，
不是账本，不用 trades.json 那种"绝不能丢"的硬规则。

存 `private/book_state.json`：`{symbol: {peak_price, peak_at, add_count, last_add_at}}`。
原子写，和 portfolio_book._save 同一个模式（tmp + rename，chmod 0600）。

**为什么不需要单独存"保本止损是否已武装"**：保本止损的启动条件是"浮盈曾经达到止盈幅度的
一半"，等价于"peak_price 曾经到过 cost×(1+breakeven_arm_pct)"——已经有 peak_price 就能
直接判断，不用再存一个布尔位，少一处会和 peak_price 打架的状态。

**为什么不 import portfolio_book 拿 default_dir()**：portfolio_book.sell() 在整笔卖出时
要调用这里的 clear()（新开仓不该继承上一轮持仓的峰值和补仓计数），双向 import 会成环。
default_dir() 的算法就复制一份——intraday_engine.py 和 portfolio_book.py 本来就各自独立
算过一遍同样的环境变量，这是仓库里已有的写法，不是新发明的技巧。
"""
import json
import os
import tempfile


def default_dir():
    config_path = os.environ.get('CONFIG_PATH') or os.path.join(
        os.path.dirname(__file__), 'data', 'webapp_config.json')
    return os.environ.get('PRIVATE_DATA_DIR') or os.path.join(
        os.path.dirname(os.path.abspath(config_path)), 'private')


def _path(directory=None):
    return os.path.join(directory or default_dir(), 'book_state.json')


def load(directory=None):
    path = _path(directory)
    if not os.path.exists(path):
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _save(data, directory=None):
    path = _path(directory)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix='.tmp')
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
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


def get(symbol, directory=None):
    return load(directory).get(symbol) or {}


def _iso(now):
    return now.isoformat() if hasattr(now, 'isoformat') else now


def touch_peak(symbol, price, now, bars=None, opened_on=None, directory=None):
    """记录/更新这只持仓自建仓以来观测到的最高价，移动止损用。返回目前的 peak_price（可能是
    刚更新的，也可能是历史已有的，`price` 为 None 时原样返回不做任何写入）。

    第一次调用且给了 bars+opened_on 时，先从本地日线历史把开仓以来的最高价补上——否则新增
    这个功能那天，之前几天/几周已经走出的高点会被当作"还没发生"，移动止损从今天的价格起算，
    等于第一天就把止损顶到了当前价附近。之后只认实际观测到的价格，单调只增不减。"""
    data = load(directory)
    row = dict(data.get(symbol) or {})
    if 'peak_price' not in row and bars and opened_on:
        hist = [float(b['high']) for b in bars if b.get('date', '') >= opened_on and b.get('high') is not None]
        if hist:
            row['peak_price'] = max(hist)
    if price is None:
        return row.get('peak_price')
    price = float(price)
    if row.get('peak_price') is None or price > row['peak_price']:
        row['peak_price'], row['peak_at'] = price, _iso(now)
        data[symbol] = row
        _save(data, directory)
    elif symbol not in data and row:
        data[symbol] = row              # 只是把历史高点第一次落盘，价格没有创新高也要保存
        _save(data, directory)
    return row.get('peak_price')


def record_add(symbol, now, directory=None):
    """补仓一次：次数 +1，记下时间（add_signal 的"补仓次数上限/间隔冷却"两条硬否决都靠它）。"""
    data = load(directory)
    row = dict(data.get(symbol) or {})
    row['add_count'] = row.get('add_count', 0) + 1
    row['last_add_at'] = _iso(now)
    data[symbol] = row
    _save(data, directory)
    return row


def clear(symbol, directory=None):
    """整笔卖出后调用：清掉这只股票的峰值与补仓计数。下次再买入是全新的一笔仓位，不该继承
    上一轮的历史高点（移动止损会算错）或补仓次数（会把新仓位一开始就锁死补仓额度）。"""
    data = load(directory)
    if symbol in data:
        del data[symbol]
        _save(data, directory)
