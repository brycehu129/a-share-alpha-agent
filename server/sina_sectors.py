"""新浪行业板块资金流 + 个股所属行业映射表。只取公开行情，不生成建议。

**这是东财板块数据的兜底源，不是主力。** 曾经打算让它当主力，实测后推翻了，
理由和当时的推理都记在这里，免得下次再走一遍：

- 吸引人的地方（都属实）：48 个行业**一次请求拿全**（东财 496 个但一页硬顶 100 行）；
  另一台主机，2026-09-24 东财把本机封了 25 分钟以上的同时，新浪十几次请求零失败；
  数据已逐个核对一致（新华传媒 +10.06%、云煤能源 +10.04%、七匹狼 +9.98% 与东财完全吻合）。
- **致命缺陷**：新浪这套行业分类陈旧，成分股只覆盖 **2749 / 5561 ≈ 49%** 的沪深A股，
  其中**科创板（sh688）一只都没有**，创业板只收 460 只，连沪主板也只有约六成。
  （实测：48 个板块逐个翻页，无失败、无截断——没有任何板块的成分股数是 100 的倍数。）
  拿它当主力，`require_sector` 打开时会有近一半候选被判成"板块未匹配"而丢掉，
  丢的还恰恰是科创板/创业板这些波动大、抗跌信号最有意义的票。
- **另外，东财的 100 行截断对这个用途并不是缺陷**：判据是"该股所属板块在主力净流入占比
  前 100 名内"这个**成员判定**，排不进前 100 名本来就意味着板块不够强——截断即过滤。
  截断只在需要全量分布（算百分位）时才有害，而扫描器早就不用百分位了。

所以：东财是板块主力（覆盖 100%、f100 白送、不需要映射表），本模块在东财取不到时顶上；
顶上时调用方必须告诉用户"口径已切换为粗分类，约半数个股无归属"。

--- 两个口径差异，调用方必须知道 -------------------------------------------------

1. **行业粒度不同，名字对不上。** 东财是申万二三级那种细粒度（"出版"、"电子化学品Ⅱ"，496 个），
   新浪是 48 个粗行业（"传媒娱乐"、"煤炭行业"）。所以**个股归属不能跨源混用**——
   东财响应里自带的 f100 是东财口径，配不上新浪的板块清单。这就是本模块要维护
   一张"个股 → 新浪行业"映射表的原因（membership，见下）。
2. **强度分的维度不同。** 东财给涨跌家数，能算"上涨占比"，所以是三维打分
   （涨幅 40% + 上涨占比 30% + 主力净流入占比 30%）；新浪不给家数，只能两维
   （涨幅 50% + 主力净流入占比 50%）。两者的 strength **不可直接比较**，
   所以每行都带 `strength_basis` 标明出处，不靠调用方记得。

--- 映射表 ---------------------------------------------------------------------

新浪不提供"个股→行业"的批量接口，只能按板块列成分股（48 个板块各翻页取一遍，
约 100 次请求）。成分股盘中不变，所以**每个交易日建一次就够**，由独立的定时任务做，
扫描器只读落盘结果。表不在或过期时，扫描器退回东财板块路径，而不是静默把板块条件当成不满足。
"""
import argparse
import json
import os
import sys
import time as _time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import market_rankings
import market_review
from collect_quotes import CST

ENDPOINT = 'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/'
# 实测与字面直觉相反：fenlei=0 返回 new_* 开头的**行业**板块，fenlei=1 返回 gn_* 开头的**概念**板块。
INDUSTRY_FENLEI = 0
CONCEPT_FENLEI = 1
PAGE = 100
MEMBER_GAP_S = 0.35          # 建表时每页之间的间隔，别把新浪也惹毛了
MAX_PAGES_PER_SECTOR = 12    # 单个板块最多翻几页；防止接口异常时无限翻
STRENGTH_BASIS = 'sina-2dim'


class SinaError(ValueError):
    """新浪取数或解析失败。原因会展示给用户，所以要写人话。"""


def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float('inf') else None


def _get(path, params, http=None):
    url = ENDPOINT + path + '?' + urlencode(params)
    try:
        return market_review.get_json(url, http)
    except Exception as exc:
        raise SinaError('新浪请求失败：%s' % str(exc)[:140]) from exc


# --- 板块资金流 ---------------------------------------------------------------

def parse_sectors(payload):
    """新浪的数值全是字符串，涨跌幅与净流入占比是**小数**（0.0879 = 8.79%），这里统一成百分数。"""
    if not isinstance(payload, list):
        raise SinaError('新浪板块响应结构异常（不是列表）')
    out = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        code, name = str(raw.get('category') or ''), str(raw.get('name') or '').strip()
        change, ratio, net = (_num(raw.get(k)) for k in ('avg_changeratio', 'ratioamount', 'netamount'))
        if not code or not name or None in (change, ratio, net):
            continue
        out.append({
            'code': code, 'name': name,
            'change_pct': round(change * 100, 2),
            'main_net': net, 'main_net_pct': round(ratio * 100, 2),
            'up_pct': None,                       # 新浪不给涨跌家数，缺失就是缺失
            'turnover_yi': _num(raw.get('turnover')),
            'lead_symbol': str(raw.get('ts_symbol') or '') or None,
            'lead_name': str(raw.get('ts_name') or '') or None,
        })
    if not out:
        raise SinaError('新浪没有返回可用的板块数据')
    return out


def score(rows):
    """两维合成强度（涨幅 50% + 主力净流入占比 50%）。

    东财那边是三维（多一个"上涨占比"），**两者的 strength 不可直接比较**，
    所以每行带 strength_basis 标明出处。百分位算法复用 market_rankings，保持口径一致。
    """
    pct = {f: market_rankings._percentiles(rows, f) for f in ('change_pct', 'main_net_pct')}
    return [{**r, 'strength': round(pct['change_pct'][r['change_pct']] * .5 +
                                    pct['main_net_pct'][r['main_net_pct']] * .5, 1),
             'strength_basis': STRENGTH_BASIS} for r in rows]


def fetch_sectors(http=None, fenlei=INDUSTRY_FENLEI):
    """一次请求拿全部板块（行业 48 个）并打分。返回 (scored_rows, total, scope)。"""
    payload = _get('MoneyFlow.ssl_bkzj_bk',
                   {'page': 1, 'num': PAGE, 'sort': 'netamount', 'asc': 0,
                    'bankuai': '', 'shizhi': '', 'fenlei': fenlei}, http)
    rows = score(parse_sectors(payload))
    kind = '行业' if fenlei == INDUSTRY_FENLEI else '概念'
    return rows, len(rows), '新浪%s板块全量 %d 个' % (kind, len(rows))


# --- 成分股与映射表 -----------------------------------------------------------

def fetch_members(category, http=None, gap_s=MEMBER_GAP_S):
    """按板块翻页取成分股。空页即终止（实测 page 超出后返回 []）。"""
    members, seen = [], set()
    for page in range(1, MAX_PAGES_PER_SECTOR + 1):
        payload = _get('Market_Center.getHQNodeData',
                       {'page': page, 'num': PAGE, 'sort': 'changepercent', 'asc': 0,
                        'node': category, 'symbol': '', '_s_r_a': 'page'}, http)
        if not isinstance(payload, list) or not payload:
            break
        for raw in payload:
            symbol = str((raw or {}).get('symbol') or '').strip().lower()
            if symbol and symbol not in seen:
                seen.add(symbol)
                members.append(symbol)
        if len(payload) < PAGE:
            break
        if gap_s:
            _time.sleep(gap_s)
    return members


def build_membership(http=None, gap_s=MEMBER_GAP_S, fetch=None):
    """逐个板块取成分股，拼出 {symbol: 板块名}。

    一只股票理论上只属于一个新浪行业；真出现重复时**保留第一个并记进 duplicates**，
    不静默覆盖——归属不唯一会让"板块是否强势"这个判断变得不可复现。
    """
    fetch = fetch or fetch_members
    sectors, _total, _scope = fetch_sectors(http)
    by_symbol, duplicates, failed = {}, [], []
    for row in sectors:
        try:
            members = fetch(row['code'], http, gap_s)
        except SinaError as exc:
            failed.append({'sector': row['name'], 'reason': str(exc)})
            continue
        for symbol in members:
            if symbol in by_symbol:
                duplicates.append({'symbol': symbol, 'kept': by_symbol[symbol], 'also': row['name']})
                continue
            by_symbol[symbol] = row['name']
        if gap_s:
            _time.sleep(gap_s)
    return {'by_symbol': by_symbol,
            'sectors': [r['name'] for r in sectors],
            'duplicates': duplicates[:200], 'duplicate_count': len(duplicates),
            'failed_sectors': failed}


# --- 落盘 ---------------------------------------------------------------------

def data_dir():
    import portfolio_book
    return Path(os.environ.get('SINA_SECTOR_DIR')
                or (Path(portfolio_book.default_dir()).parent / 'sina_sectors'))


def map_path(directory=None):
    return Path(directory or data_dir()) / 'industry_membership.json'


def save_map(payload, directory=None):
    path = map_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)                       # 原子替换，Windows/Linux 都安全
    return path


def load_map(directory=None, max_age_days=3):
    """读映射表。不存在、损坏、或太旧都返回 None——调用方据此退回东财路径，
    而不是拿一张过期的表继续算。"""
    path = map_path(directory)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get('by_symbol'), dict):
        return None
    built = str(data.get('trade_date') or '')
    if max_age_days is not None and built:
        try:
            age = (datetime.now(CST).date() - datetime.strptime(built, '%Y-%m-%d').date()).days
        except ValueError:
            return None
        if age > max_age_days:
            return None
    return data


def run(now=None, directory=None, http=None, gap_s=MEMBER_GAP_S):
    """建一次表并落盘。返回摘要；不抛出——定时任务里一次失败不能影响下一次。"""
    now = now or datetime.now(CST)
    summary = {'at': now.isoformat(), 'built': False}
    try:
        built = build_membership(http, gap_s)
    except SinaError as exc:
        summary['error'] = str(exc)
        return summary
    if not built['by_symbol']:
        summary['error'] = '一只成分股都没取到，不落盘（避免用空表覆盖上一份可用的）'
        return summary
    payload = {'trade_date': now.date().isoformat(), 'built_at': now.isoformat(),
               'source': 'sina', 'sector_count': len(built['sectors']),
               'symbol_count': len(built['by_symbol']),
               'duplicate_count': built['duplicate_count'],
               'failed_sectors': built['failed_sectors'],
               'by_symbol': built['by_symbol']}
    try:
        path = save_map(payload, directory)
    except OSError as exc:
        summary['error'] = '落盘失败：%s' % type(exc).__name__
        return summary
    summary.update(built=True, path=str(path), sectors=payload['sector_count'],
                   symbols=payload['symbol_count'],
                   duplicates=payload['duplicate_count'],
                   failed_sectors=[f['sector'] for f in built['failed_sectors']])
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--build', action='store_true', help='重建个股→行业映射表并落盘')
    p.add_argument('--json', action='store_true')
    a = p.parse_args()
    started = _time.monotonic()
    if a.build:
        result = run()
        result['duration_ms'] = round((_time.monotonic() - started) * 1000)
        if a.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif result.get('built'):
            print('映射表已重建：%d 个行业 / %d 只股票 · 重复 %d · %dms%s' % (
                result['sectors'], result['symbols'], result['duplicates'], result['duration_ms'],
                ' · 失败板块 ' + ','.join(result['failed_sectors']) if result['failed_sectors'] else ''))
        else:
            print('未建成：%s' % result.get('error'))
        return 0 if result.get('built') else 1

    rows, total, scope = fetch_sectors()
    if a.json:
        print(json.dumps({'scope': scope, 'rows': rows}, ensure_ascii=False, indent=2))
    else:
        print('%s（%s）' % (scope, STRENGTH_BASIS))
        for r in sorted(rows, key=lambda x: -x['strength'])[:15]:
            print('  %-10s 强度%5.1f 涨跌%+6.2f%% 主力%9s 占比%+6.2f%% 领涨 %s' % (
                r['name'], r['strength'], r['change_pct'],
                '%.2f亿' % (r['main_net'] / 1e8), r['main_net_pct'], r['lead_name'] or '—'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
