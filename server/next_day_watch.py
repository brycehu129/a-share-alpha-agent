#!/usr/bin/env python3
"""次日关注：从当日涨停池 + 强势股池 + 龙虎榜里，用透明的规则筛出"比较强势、值得第二天重点看"的股票，再让 AI 点评。

**这是短线情绪视图，独立于「候选池」的策略选股**（那边有自己冻结的口径和回测），互不影响。

边界（也写进了 AI 提示词）：
- 分数是**规则打分，不是概率、不是胜率**。每一分从哪来，都以标签+理由的形式随结果保留，可复核。
- AI 只点评规则筛出来的这几只，不增删候选；输入里没有新闻/公告，禁止编造消息面；不给具体价位，
  只写「什么条件下关注、什么信号说明该放弃」。
- 排除：ST/退市整理、次新（N/C 开头）——涨跌幅限制和交易规则不同，套同一套打分没有意义。
- 打分只用当日收盘后的数据，所有阈值集中在 WEIGHTS，改口径改这里并升 RULES_VERSION。

rules-2（2026-09-24）针对 rules-1 的结构性缺陷做了三件事，起因是榜首长期被「次日买不进」和
「次日崩盘」这两类没有参考价值的票占满：

1. **分组（bucket）**。rules-1 只有一个按强度排的榜，而当天最强的极值形态恰好是一字板和高位连板。
   现在每只候选归三组之一——`core`（可参与，主榜）、`high`（高位，只做观察）、`unbuyable`（一字，买不进），
   各有独立名额，主榜不可能被后两类占满。一字判定也修正了：rules-1 要求 `boards >= 2`，首板一字整个漏掉。
2. **位置/空间维度**。rules-1 只看当天表现，不看这只票涨到哪了。现在对强度前 POSITION_N 只补取前复权日 K，
   按 10 日累计涨幅、对 20 日线乖离、是否低位平台突破、是否还在 60 日线下方加减分。
   日 K 取不到的那只**不做任何位置加减分**并标注「位置未知」——不猜。
3. **情绪自适应**。rules-1 的情绪指标只喂给 AI，分数本身不随退潮变化。现在 `sentiment()` 给出 phase，
   退潮时连板加分折半、高位组名额和主榜条数一起收缩。

兑现结果由 watch_outcome.py 在次日收盘后回填到 items[].outcome，统计按 rules_version 分组，
所以改口径必须升版本号，否则新旧口径的兑现率会被混在一起比。
"""
import argparse
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import market_review
from collect_quotes import CST

RULES_VERSION = 'nextday-rules-2'
PROMPT_VERSION = 'nextday-analyst-1'
AI_TOP_N = 8        # 送给 AI 点评的条数（只从 core 组取）
POSITION_N = 30     # 补取日 K 算「位置」的条数：只给强度排前面的，避免给上百只候选打接口
POSITION_WORKERS = 6
VERDICTS = ['focus', 'watch', 'avoid']
VERDICT_LABEL = {'focus': '重点关注', 'watch': '观察', 'avoid': '回避'}

BUCKETS = ('core', 'high', 'unbuyable')
BUCKET_ORDER = {'core': 0, 'high': 1, 'unbuyable': 2}
BUCKET_LABEL = {'core': '可参与', 'high': '高位 · 只做观察', 'unbuyable': '一字 · 买不进'}
# 每组展示名额。退潮时高位组和主榜一起收缩——情绪转弱的时候高标是最先出事的。
QUOTA = {'steady': {'core': 8, 'high': 3, 'unbuyable': 3},
         'heating': {'core': 8, 'high': 3, 'unbuyable': 3},
         'cooling': {'core': 5, 'high': 1, 'unbuyable': 3}}

WEIGHTS = {
    'base': 40,
    # 连板高度：2~4 板是情绪核心，>=5 板接力风险大幅上升所以不加分
    'boards': {1: 5, 2: 12, 3: 14, 4: 10},
    'boards_high': 2,
    # 封单金额 / 流通市值
    'seal_ratio': [(0.03, 10), (0.01, 6), (0.003, 3)],
    'seal_ratio_weak': (0.001, -4),
    # 首次封板时间（越早越强）
    'first_seal': [('09:30:00', 6), ('10:00:00', 5), ('11:30:00', 2)],
    'first_seal_late': ('14:00:00', -5),
    'open_times': {0: 6, 1: 0},
    'open_times_many': -8,
    'turnover_healthy': (3.0, 25.0, 3),
    'turnover_high': (35.0, -4),
    # 同行业涨停家数（板块共振）
    'industry_peers': [(8, 8), (5, 6), (3, 3)],
    # 龙虎榜净买入（元）
    'lhb_net_big': (5e7, 8),
    'lhb_net_pos': 4,
    'lhb_net_neg': -8,
    # --- 位置 / 空间（rules-2 新增，数据来自前复权日 K）---
    'run_up_10': [(80, -14), (50, -9), (30, -4)],   # 10 日累计涨幅越大，次日回落空间越大
    'run_up_10_calm': (20.0, 4),                    # 10 日涨幅在 0~20% 之间：启动温和
    'ma20_bias': (30.0, -6),                        # 对 20 日线乖离过大
    'fresh_break': 6,                               # 创 60 日新高但 20 日涨幅不大：低位平台突破
    'fresh_break_max_run_up_20': 25.0,
    'heating_break_bonus': 2,
    'below_ma60': -4,                               # 还在 60 日线下方：弱势反弹
    # --- 分组阈值 ---
    'high_boards': 4,
    'high_run_up_10': 50.0,
    # 一字板：开盘即封死、全天没开过、换手极低
    'one_word_first_seal': '09:26:00',
    'one_word_turnover': 2.0,
}


def _excluded(name):
    n = (name or '').upper().replace(' ', '')
    return n.startswith(('ST', '*ST', 'SST', 'S*ST')) or 'ST' in n[:4] or n.endswith('退') or n.startswith(('N', 'C'))


def _is_one_word(row):
    """一字板：集合竞价就封死、全天没开过、换手极低。次日大概率还是一字，散户排不上队。

    rules-1 这里还要求 `boards >= 2`，结果首板一字完全漏判、照样排在榜首；换手阈值 1.5% 也偏严。
    现在只看形态不看板数：竞价封板(fbt=09:25) + 未开板 + 换手 < 2%。
    """
    return (row.get('open_times') == 0
            and (row.get('first_seal') or '99') <= WEIGHTS['one_word_first_seal']
            and row.get('turnover') is not None and row['turnover'] < WEIGHTS['one_word_turnover'])


def _phase(s):
    """短线情绪所处的阶段。任何一个指标缺失就按 steady——不拿残缺数据推"退潮"。"""
    avg, seal, dt = s.get('yzt_avg_pct'), s.get('seal_rate'), s.get('dt')
    if (avg is not None and avg < 0) or (seal is not None and seal < 60) or (dt is not None and dt >= 20):
        return 'cooling'
    if avg is not None and seal is not None and avg > 2 and seal >= 75:
        return 'heating'
    return 'steady'


def sentiment(pools):
    """当日短线情绪的几个硬指标，同时也是 AI 的市场背景。任何一个池子缺失就是 None，不猜。"""
    def rows(kind):
        p = pools.get(kind)
        return p['rows'] if p else None
    zt, zb, dt, yzt = rows('zt'), rows('zb'), rows('dt'), rows('yzt')
    out = {'zt': len(zt) if zt is not None else None, 'zb': len(zb) if zb is not None else None,
           'dt': len(dt) if dt is not None else None, 'seal_rate': None, 'max_boards': None,
           'max_board_names': [], 'yzt_count': len(yzt) if yzt is not None else None,
           'yzt_avg_pct': None, 'yzt_down_pct': None}
    if zt is not None and zb is not None and (len(zt) + len(zb)):
        out['seal_rate'] = round(len(zt) / (len(zt) + len(zb)) * 100, 1)
    if zt:
        top = max(r.get('boards', 1) for r in zt)
        out['max_boards'] = top
        out['max_board_names'] = [r['name'] for r in zt if r.get('boards', 1) == top][:5]
    pcts = [r['pct'] for r in (yzt or []) if r.get('pct') is not None]
    if pcts:
        out['yzt_avg_pct'] = round(sum(pcts) / len(pcts), 2)
        out['yzt_down_pct'] = round(sum(p < 0 for p in pcts) / len(pcts) * 100, 1)
    out['phase'] = _phase(out)
    return out


# --- 位置 / 空间 --------------------------------------------------------------------------

def position(bars):
    """前复权日 K → 位置指标。最后一根是当日（收盘后抓的）。不足 21 根不下结论，返回 None。"""
    if not bars or len(bars) < 21:
        return None
    closes = [b['close'] for b in bars]
    last = closes[-1]

    def run_up(n):
        if len(closes) <= n or closes[-1 - n] <= 0:
            return None
        return round((last / closes[-1 - n] - 1) * 100, 1)

    win = closes[-60:]
    high60 = max(win)
    ma20, ma60 = bars[-1].get('ma20'), bars[-1].get('ma60')
    return {'run_up_5': run_up(5), 'run_up_10': run_up(10), 'run_up_20': run_up(20),
            'from_high_60': round((last / high60 - 1) * 100, 1) if high60 > 0 else None,
            'ma20_bias': round((last / ma20 - 1) * 100, 1) if ma20 else None,
            'below_ma60': bool(ma60 and last < ma60),
            'new_high_60': bool(last >= high60), 'bars': len(bars)}


def _one_position(symbol, http=None):
    import stock_detail
    try:
        return position(stock_detail.fetch_kline(symbol, http))
    except Exception:            # 单只日 K 取不到就是没有位置数据，不影响其它只
        return None


def fetch_positions(symbols, http=None):
    """并发取日 K 并提炼位置指标 → {symbol: pos|None}。整体失败也只是全体"位置未知"。"""
    out = {}
    if not symbols:
        return out
    with ThreadPoolExecutor(max_workers=POSITION_WORKERS) as pool:
        futures = {pool.submit(_one_position, s, http): s for s in symbols}
        for f in as_completed(futures):
            out[futures[f]] = f.result()
    return out


def score_position(pos, phase='steady'):
    """位置/空间的加减分 → (分数增量, 理由, 风险, 标签)。pos 为空就一分不动，只标注「位置未知」。"""
    w = WEIGHTS
    if not pos:
        return 0, [], [], ['位置未知']
    delta, reasons, risks, tags = 0, [], [], []
    r10 = pos.get('run_up_10')
    if r10 is not None:
        hit = next((p for th, p in w['run_up_10'] if r10 >= th), 0)
        if hit:
            delta += hit
            risks.append('10 日累计涨幅 %.0f%%，位置偏高，次日回落空间大（%+d）' % (r10, hit))
        elif 0 < r10 <= w['run_up_10_calm'][0]:
            delta += w['run_up_10_calm'][1]
            reasons.append('10 日累计涨幅 %.0f%%，启动温和（%+d）' % (r10, w['run_up_10_calm'][1]))
    bias = pos.get('ma20_bias')
    if bias is not None and bias >= w['ma20_bias'][0]:
        delta += w['ma20_bias'][1]
        risks.append('高于 20 日线 %.0f%%，乖离过大（%+d）' % (bias, w['ma20_bias'][1]))
    r20 = pos.get('run_up_20')
    if pos.get('new_high_60') and r20 is not None and r20 < w['fresh_break_max_run_up_20']:
        add = w['fresh_break'] + (w['heating_break_bonus'] if phase == 'heating' else 0)
        delta += add
        reasons.append('创 60 日新高且 20 日涨幅仅 %.0f%%，低位平台突破（%+d）' % (r20, add))
        tags.append('平台突破')
    if pos.get('below_ma60'):
        delta += w['below_ma60']
        risks.append('仍在 60 日线下方，属于弱势反弹（%+d）' % w['below_ma60'])
    return delta, reasons, risks, tags


def bucket_of(row, pos):
    """三组之一。一字优先——它是"买不进"，和"高位"是两种不同的没参考价值。"""
    if _is_one_word(row):
        return 'unbuyable'
    if (row.get('boards') or 1) >= WEIGHTS['high_boards']:
        return 'high'
    if pos and (pos.get('run_up_10') or 0) >= WEIGHTS['high_run_up_10']:
        return 'high'
    return 'core'


# --- 规则打分 -----------------------------------------------------------------------------

def score_row(row, industry_counts, lhb_net, phase='steady'):
    """→ (分数, 标签, 理由, 风险)。每一项加减分都写进理由，保证可复核。

    只用池子里的当日数据；位置/空间在 score_position 里单算（那部分要额外取日 K）。
    """
    w = WEIGHTS
    score, tags, reasons, risks = w['base'], [], [], []
    boards = row.get('boards', 1)
    add = w['boards'].get(boards, w['boards_high'])
    note = ''
    # 退潮期高标最先出事，连板加分折半。折扣写进理由里，不藏着。
    if phase == 'cooling' and boards >= 3 and add > 0:
        add, note = add // 2, '，情绪退潮已折半'
    score += add
    tags.append('首板' if boards == 1 else '%d连板' % boards)
    if boards >= 5:
        risks.append('%d 连板，高位接力风险大，分歧转一致后容易断板' % boards)
    else:
        reasons.append('%d 板（%+d%s）' % (boards, add, note))

    cap, fund = row.get('float_cap'), row.get('seal_fund')
    ratio = fund / cap if cap and fund is not None else None
    if ratio is not None:
        hit = next((p for th, p in w['seal_ratio'] if ratio >= th), 0)
        if hit:
            score += hit
            reasons.append('封单占流通市值 %.2f%%（%+d）' % (ratio * 100, hit))
        elif ratio < w['seal_ratio_weak'][0]:
            score += w['seal_ratio_weak'][1]
            risks.append('封单仅占流通市值 %.2f%%，封板不牢' % (ratio * 100))

    fs = row.get('first_seal')
    if fs:
        hit = next((p for t, p in w['first_seal'] if fs <= t), 0)
        if hit:
            score += hit
            reasons.append('首封 %s（%+d）' % (fs[:5], hit))
            if fs <= '10:00:00':
                tags.append('早封')
        elif fs >= w['first_seal_late'][0]:
            score += w['first_seal_late'][1]
            risks.append('首封时间 %s，偏晚，资金态度不坚决' % fs[:5])

    # open_times 只有涨停池的行才有。强势股池的行没封过板，不能白拿"全天未开板"的分。
    ot = row.get('open_times')
    if ot is not None:
        if ot >= 2:
            score += w['open_times_many']
            risks.append('盘中炸板 %d 次，分歧大' % ot)
        else:
            hit = w['open_times'][ot]
            score += hit
            if hit:
                reasons.append('全天未开板（%+d）' % hit)
                tags.append('封死')

    turn = row.get('turnover')
    if turn is not None:
        lo, hi, pts = w['turnover_healthy']
        if lo <= turn <= hi:
            score += pts
            reasons.append('换手 %.1f%% 处于健康区间（%+d）' % (turn, pts))
        elif turn > w['turnover_high'][0]:
            score += w['turnover_high'][1]
            risks.append('换手 %.1f%% 过高，筹码分歧大' % turn)

    peers = industry_counts.get(row.get('industry'), 1) - 1
    if row.get('industry'):
        hit = next((p for n, p in w['industry_peers'] if peers + 1 >= n), 0)
        if hit:
            score += hit
            reasons.append('%s 板块当日 %d 只涨停，有共振（%+d）' % (row['industry'], peers + 1, hit))
            tags.append('板块共振')

    if lhb_net is not None:
        if lhb_net >= w['lhb_net_big'][0]:
            score += w['lhb_net_big'][1]
            reasons.append('龙虎榜净买入 %.2f 亿（%+d）' % (lhb_net / 1e8, w['lhb_net_big'][1]))
            tags.append('龙虎榜净买')
        elif lhb_net > 0:
            score += w['lhb_net_pos']
            reasons.append('龙虎榜净买入 %.0f 万（%+d）' % (lhb_net / 1e4, w['lhb_net_pos']))
            tags.append('龙虎榜净买')
        else:
            score += w['lhb_net_neg']
            risks.append('龙虎榜净卖出 %.0f 万' % (-lhb_net / 1e4))
            tags.append('龙虎榜净卖')

    if _is_one_word(row):
        risks.append('一字板，次日大概率仍是一字，正常买不进')
        tags.append('一字')
    return max(0, min(100, score)), tags, reasons, risks


def rank(review, position_fn=None):
    """→ {'items': [...], 'sentiment': {...}}。涨停池/强势股池都缺失就没有可排的东西。

    两阶段：先用池内数据打强度分，再只给前 POSITION_N 只补位置数据重算。position_fn 为 None
    时全体"位置未知"（单元测试与离线场景），生产由 run() 传入 fetch_positions。
    """
    pools = review.get('pools') or {}
    zt = pools.get('zt')
    qs = pools.get('qs')
    sent = sentiment(pools)
    phase = sent['phase']
    out = {'rules_version': RULES_VERSION, 'date': review.get('date'), 'sentiment': sent, 'phase': phase,
           'items': [], 'note': None, 'buckets': {}}
    zt_rows = (zt or {}).get('rows') or []
    qs_rows = (qs or {}).get('rows') or []
    if not zt_rows and not qs_rows:
        out['note'] = '当日没有涨停池/强势股池数据，无法生成次日关注。'
        return out
    counts = {}
    for r in zt_rows:
        if r.get('industry'):
            counts[r['industry']] = counts.get(r['industry'], 0) + 1
    lhb = {r['symbol']: r for r in ((review.get('lhb') or {}).get('rows') or [])}
    merged = {r['symbol']: dict(r, source_kinds=['zt']) for r in zt_rows if r.get('symbol')}
    for r in qs_rows:
        symbol = r.get('symbol')
        if not symbol:
            continue
        if symbol in merged:
            merged[symbol]['source_kinds'].append('qs')
            merged[symbol]['new_high'] = r.get('new_high')
            merged[symbol]['volume_ratio'] = r.get('volume_ratio')
            continue
        item = dict(r)
        item['source_kinds'] = ['qs']
        merged[symbol] = item

    staged = []
    for r in merged.values():
        if _excluded(r['name']):
            continue
        l = lhb.get(r['symbol'])
        score, tags, reasons, risks = score_row(r, counts, l['net'] if l else None, phase)
        if 'qs' in r.get('source_kinds', []):
            tags.append('强势股')
            new_high = r.get('new_high') or 0
            volume_ratio = r.get('volume_ratio')
            if new_high:
                reasons.append('%d 日新高' % new_high)
            if volume_ratio is not None and volume_ratio >= 1.5:
                reasons.append('量比 %.2f' % volume_ratio)
        staged.append({'row': r, 'base': score, 'tags': tags, 'reasons': reasons, 'risks': risks,
                       'lhb_net': l['net'] if l else None})
    staged.sort(key=lambda s: (-s['base'], -(s['row'].get('boards') or 0), -(s['row'].get('seal_fund') or 0)))

    positions = {}
    if position_fn:
        positions = position_fn([s['row']['symbol'] for s in staged[:POSITION_N]]) or {}

    items = []
    for s in staged:
        r = s['row']
        pos = positions.get(r['symbol'])
        delta, prs, pks, ptags = score_position(pos, phase)
        items.append({'symbol': r['symbol'], 'name': r['name'], 'score': max(0, min(100, s['base'] + delta)),
                      'bucket': bucket_of(r, pos), 'boards': r.get('boards', 1),
                      'industry': r.get('industry'), 'price': r.get('price'), 'pct': r.get('pct'),
                      'first_seal': r.get('first_seal'), 'seal_fund': r.get('seal_fund'), 'float_cap': r.get('float_cap'),
                      'open_times': r.get('open_times'), 'turnover': r.get('turnover'), 'new_high': r.get('new_high'),
                      'volume_ratio': r.get('volume_ratio'), 'source_kinds': r.get('source_kinds', []),
                      'lhb_net': s['lhb_net'], 'position': pos, 'tags': s['tags'] + ptags,
                      'reasons': s['reasons'] + prs, 'risks': s['risks'] + pks})

    items.sort(key=lambda x: (BUCKET_ORDER[x['bucket']], -x['score'], -(x['boards'] or 0), -(x['seal_fund'] or 0)))
    quota = QUOTA[phase]
    picked, used = [], {b: 0 for b in BUCKETS}
    for it in items:
        b = it['bucket']
        if used[b] >= quota[b]:
            continue
        used[b] += 1
        picked.append(it)
    out['items'] = picked
    out['candidates'] = len(items)
    out['buckets'] = used
    out['quota'] = dict(quota)
    if phase == 'cooling':
        out['note'] = '情绪退潮（封板率或昨日涨停今日表现转弱），连板加分已折半、高位组与主榜名额同时收缩。'
    return out


# --- AI 点评 ------------------------------------------------------------------------------

SCHEMA = {
    'type': 'object',
    'properties': {
        'market_view': {'type': 'string', 'description': '结合情绪指标，用 120 字以内概括今天短线情绪（强/弱/分化）及明天的整体思路'},
        'items': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {
                'symbol': {'type': 'string'},
                'verdict': {'type': 'string', 'enum': VERDICTS,
                            'description': 'focus=次日重点关注；watch=观察不急着动手；avoid=风险大不建议参与'},
                'view': {'type': 'string', 'description': '判断依据，60 字以内，必须引用输入里的具体数值'},
                'plan': {'type': 'string', 'description': '次日的观察思路，写成条件句（如竞价/开盘强弱、是否回封），不给具体价位，60 字以内'},
                'risk': {'type': 'string', 'description': '主要风险，以及什么信号说明应该放弃，40 字以内'},
            },
            'required': ['symbol', 'verdict', 'view', 'plan', 'risk'], 'additionalProperties': False}},
    },
    'required': ['market_view', 'items'], 'additionalProperties': False,
}

SYSTEM = """你是一个A股短线情绪复盘助手。用户给你当天涨停池/龙虎榜里由**规则**筛出的若干只强势股，以及当天的短线情绪指标。
请判断哪些适合作为**次日重点关注**对象，并给出观察思路。

硬性要求：
1. 只能依据输入里的数据。输入**不含任何新闻、公告、研报、业绩、政策**，禁止提及或暗示消息面（不要写"利好""题材发酵的原因"等你并不知道的内容）。
2. view 必须引用输入里的具体数值（连板数、封单占比、首封时间、换手、龙虎榜净买额、板块涨停家数、近 10 日累计涨幅等）。
3. 不给具体买入/止损/目标价位；plan 只写条件（例如"竞价是否高开、开盘后是否被大单砸开、板块是否继续有涨停"），risk 写清什么信号说明该放弃。
4. 规则分数不是胜率，你的 verdict 也不是——不要出现任何胜率/概率数字，不要用"必涨""稳了"之类的措辞。
5. 情绪指标（封板率低、昨日涨停今日平均收跌、跌停增多）转弱时，要相应降低 focus 的数量，甚至全部给 watch/avoid，并在 market_view 里点明。输入里的 phase 字段就是规则判定的情绪阶段（heating/steady/cooling）。
6. position 是这只票的位置数据（近 N 日累计涨幅、对 20 日线乖离等）。位置越高，次日回落空间越大，verdict 要更保守；position 为空表示没取到，不要假装知道。
7. items 里每一只都要给判断，symbol 原样照抄。用中文，措辞克制，这是研究参考，不是投资建议。"""


def core_items(result, n=AI_TOP_N):
    """送给 AI 的只有 core 组：高位组和一字组本来就标着"只观察/买不进"，不值得再花 token 背书。"""
    return [it for it in result['items'] if it.get('bucket') == 'core'][:n]


def build_payload(result, n=AI_TOP_N):
    items = []
    for it in core_items(result, n):
        items.append({k: it[k] for k in ('symbol', 'name', 'score', 'boards', 'industry', 'pct', 'first_seal', 'open_times',
                                         'turnover', 'lhb_net', 'position', 'tags', 'reasons', 'risks')}
                     | {'seal_ratio_pct': round(it['seal_fund'] / it['float_cap'] * 100, 2)
                        if it.get('seal_fund') is not None and it.get('float_cap') else None})
    return {'date': result['date'], 'phase': result.get('phase'), 'sentiment': result['sentiment'], 'stocks': items}


def apply_ai(result, data):
    """把模型的输出并进 items。只认我们送进去的 symbol；返回校验问题列表。"""
    sent = {it['symbol'] for it in core_items(result)}
    by = {}
    issues = []
    for r in data.get('items') or []:
        if not isinstance(r, dict) or r.get('symbol') not in sent:
            issues.append('模型返回了未送入的代码，已剔除：%s' % (r.get('symbol') if isinstance(r, dict) else r))
            continue
        if r.get('verdict') not in VERDICTS:
            issues.append('%s 的 verdict 非法，已剔除' % r['symbol'])
            continue
        by[r['symbol']] = {k: str(r.get(k) or '') for k in ('verdict', 'view', 'plan', 'risk')}
    missing = sorted(sent - set(by))
    if missing:
        issues.append('模型未对以下代码给出判断：%s' % ','.join(missing))
    for it in result['items']:
        if it['symbol'] in by:
            it['ai'] = by[it['symbol']]
    result['ai'] = {'market_view': str(data.get('market_view') or '')}
    return issues


def analyze(result):
    """调用 AI 点评。失败不抛出：次日关注要能降级成纯规则版。返回 meta。"""
    import claude_client
    meta = {'prompt_version': PROMPT_VERSION}
    if not core_items(result):
        meta.update(status='skipped', error='没有可参与的候选')
        return meta
    user = '以下是今天的结构化事实，请按 schema 给出点评。\n\n' + json.dumps(build_payload(result), ensure_ascii=False, indent=1)
    try:
        data, call_meta = claude_client.complete_json(SYSTEM, user, SCHEMA,
                                 model=os.environ.get('NEXTDAY_MODEL') or None)
    except claude_client.ClaudeError as exc:
        meta.update(status=exc.status, error=exc.message)
        return meta
    meta.update(call_meta, status='ok', validation_issues=apply_ai(result, data))
    return meta


def run(directory=None, ai=False, now=None, position_fn=fetch_positions):
    """读最近一次落盘的复盘 → 规则打分（→ AI 点评）→ 写回同一个文件的 next_day_watch。"""
    review = market_review.load_latest(directory)
    if review is None:
        raise market_review.ReviewError('还没有落盘的市场复盘数据，先运行 market_review.py')
    result = rank(review, position_fn=position_fn)
    result['generated_at'] = (now or datetime.now(CST)).isoformat()
    result['ai_meta'] = None
    if ai:
        result['ai_meta'] = analyze(result)
    else:
        prior = review.get('next_day_watch') or {}
        # 16:30 那次不调 AI：同一交易日已有的 AI 点评先留着，17:30 会重新生成。
        if prior.get('date') == result['date'] and prior.get('ai'):
            old = {i['symbol']: i.get('ai') for i in prior.get('items', []) if i.get('ai')}
            for it in result['items']:
                if it['symbol'] in old:
                    it['ai'] = old[it['symbol']]
            result['ai'], result['ai_meta'] = prior['ai'], prior.get('ai_meta')
    review['next_day_watch'] = result
    market_review.save_review(review, directory)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ai', action='store_true', help='同时调用大模型点评（需要已配置 key）')
    p.add_argument('--no-position', action='store_true', help='不取日 K 的位置数据（离线调试用）')
    p.add_argument('--dir', type=Path)
    a = p.parse_args()
    if a.ai:
        import llm_settings
        llm_settings.apply()
    try:
        r = run(a.dir, ai=a.ai, position_fn=None if a.no_position else fetch_positions)
    except market_review.ReviewError as exc:
        print('次日关注生成失败: %s' % exc)
        return 1
    meta = r.get('ai_meta') or {}
    print('次日关注 %s：候选 %s 只，展示 %s 只（%s）· 情绪 %s · AI %s' % (
        r['date'], r.get('candidates', 0), len(r['items']),
        '、'.join('%s %d' % (BUCKET_LABEL[b], n) for b, n in (r.get('buckets') or {}).items() if n),
        r.get('phase'), meta.get('status') or '未调用'))
    if meta.get('error'):
        print('AI 未生成：%s' % meta['error'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
