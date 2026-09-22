#!/usr/bin/env python3
"""次日关注：从当日涨停池 + 强势股池 + 龙虎榜里，用透明的规则筛出"比较强势、值得第二天重点看"的股票，再让 AI 点评。

**这是短线情绪视图，独立于「候选池」的策略选股**（那边有自己冻结的口径和回测），互不影响。

边界（也写进了 AI 提示词）：
- 分数是**规则打分，不是概率、不是胜率**。每一分从哪来，都以标签+理由的形式随结果保留，可复核。
- AI 只点评规则筛出来的这几只，不增删候选；输入里没有新闻/公告，禁止编造消息面；不给具体价位，
  只写「什么条件下关注、什么信号说明该放弃」。
- 排除：ST/退市整理、次新（N/C 开头）——涨跌幅限制和交易规则不同，套同一套打分没有意义。
- 打分只用当日收盘后的数据，所有阈值集中在 WEIGHTS，改口径改这里并升 RULES_VERSION。
"""
import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

import market_review
from collect_quotes import CST

RULES_VERSION = 'nextday-rules-1'
PROMPT_VERSION = 'nextday-analyst-1'
TOP_N = 12          # 页面展示的规则筛选结果条数
AI_TOP_N = 8        # 送给 AI 点评的条数
VERDICTS = ['focus', 'watch', 'avoid']
VERDICT_LABEL = {'focus': '重点关注', 'watch': '观察', 'avoid': '回避'}

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
    # 一字/秒板：次日大概率买不进，分数照给但扣一点并标注
    'one_word_penalty': -6,
}


def _excluded(name):
    n = (name or '').upper().replace(' ', '')
    return n.startswith(('ST', '*ST', 'SST', 'S*ST')) or 'ST' in n[:4] or n.endswith('退') or n.startswith(('N', 'C'))


def _is_one_word(row):
    """一字板：开盘即封死、全天没开过、换手很低。次日大概率还是一字，散户排不上队。"""
    return (row.get('open_times') == 0 and (row.get('first_seal') or '99') <= '09:30:00'
            and row.get('turnover') is not None and row['turnover'] < 1.5 and row.get('boards', 1) >= 2)


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
    return out


def score_row(row, industry_counts, lhb_net):
    """→ (分数, 标签, 理由, 风险)。每一项加减分都写进理由，保证可复核。"""
    w = WEIGHTS
    score, tags, reasons, risks = w['base'], [], [], []
    boards = row.get('boards', 1)
    add = w['boards'].get(boards, w['boards_high'])
    score += add
    tags.append('首板' if boards == 1 else '%d连板' % boards)
    if boards >= 5:
        risks.append('%d 连板，高位接力风险大，分歧转一致后容易断板' % boards)
    else:
        reasons.append('%d 板（%+d）' % (boards, add))

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

    ot = row.get('open_times') or 0
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
        score += w['one_word_penalty']
        risks.append('一字板，次日大概率仍是一字，难以买入')
        tags.append('一字')
    return max(0, min(100, score)), tags, reasons, risks


def rank(review, top_n=TOP_N):
    """→ {'items': [...], 'sentiment': {...}}。涨停池/强势股池都缺失就没有可排的东西。"""
    pools = review.get('pools') or {}
    zt = pools.get('zt')
    qs = pools.get('qs')
    out = {'rules_version': RULES_VERSION, 'date': review.get('date'), 'sentiment': sentiment(pools), 'items': [],
           'note': None}
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
    items = []
    for r in merged.values():
        if _excluded(r['name']):
            continue
        l = lhb.get(r['symbol'])
        score, tags, reasons, risks = score_row(r, counts, l['net'] if l else None)
        if 'qs' in r.get('source_kinds', []):
            tags.append('强势股')
            new_high = r.get('new_high') or 0
            volume_ratio = r.get('volume_ratio')
            if new_high:
                reasons.append('%d 日新高' % new_high)
            if volume_ratio is not None and volume_ratio >= 1.5:
                reasons.append('量比 %.2f' % volume_ratio)
        items.append({'symbol': r['symbol'], 'name': r['name'], 'score': score, 'boards': r.get('boards', 1),
                      'industry': r.get('industry'), 'price': r.get('price'), 'pct': r.get('pct'),
                      'first_seal': r.get('first_seal'), 'seal_fund': r.get('seal_fund'), 'float_cap': r.get('float_cap'),
                      'open_times': r.get('open_times'), 'turnover': r.get('turnover'), 'new_high': r.get('new_high'),
                      'volume_ratio': r.get('volume_ratio'), 'source_kinds': r.get('source_kinds', []),
                      'lhb_net': l['net'] if l else None, 'tags': tags, 'reasons': reasons, 'risks': risks})
    items.sort(key=lambda x: (-x['score'], -x['boards'], -(x['seal_fund'] or 0)))
    out['items'] = items[:top_n]
    out['candidates'] = len(items)
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
2. view 必须引用输入里的具体数值（连板数、封单占比、首封时间、换手、龙虎榜净买额、板块涨停家数等）。
3. 不给具体买入/止损/目标价位；plan 只写条件（例如"竞价是否高开、开盘后是否被大单砸开、板块是否继续有涨停"），risk 写清什么信号说明该放弃。
4. 规则分数不是胜率，你的 verdict 也不是——不要出现任何胜率/概率数字，不要用"必涨""稳了"之类的措辞。
5. 情绪指标（封板率低、昨日涨停今日平均收跌、跌停增多）转弱时，要相应降低 focus 的数量，甚至全部给 watch/avoid，并在 market_view 里点明。
6. items 里每一只都要给判断，symbol 原样照抄。用中文，措辞克制，这是研究参考，不是投资建议。"""


def build_payload(result, n=AI_TOP_N):
    items = []
    for it in result['items'][:n]:
        items.append({k: it[k] for k in ('symbol', 'name', 'score', 'boards', 'industry', 'pct', 'first_seal', 'open_times',
                                         'turnover', 'lhb_net', 'tags', 'reasons', 'risks')}
                     | {'seal_ratio_pct': round(it['seal_fund'] / it['float_cap'] * 100, 2)
                        if it.get('seal_fund') is not None and it.get('float_cap') else None})
    return {'date': result['date'], 'sentiment': result['sentiment'], 'stocks': items}


def apply_ai(result, data):
    """把模型的输出并进 items。只认我们送进去的 symbol；返回校验问题列表。"""
    sent = {it['symbol'] for it in result['items'][:AI_TOP_N]}
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
    if not result['items']:
        meta.update(status='skipped', error='没有候选')
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


def run(directory=None, ai=False, now=None):
    """读最近一次落盘的复盘 → 规则打分（→ AI 点评）→ 写回同一个文件的 next_day_watch。"""
    review = market_review.load_latest(directory)
    if review is None:
        raise market_review.ReviewError('还没有落盘的市场复盘数据，先运行 market_review.py')
    result = rank(review)
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
    p.add_argument('--dir', type=Path)
    a = p.parse_args()
    if a.ai:
        import llm_settings
        llm_settings.apply()
    try:
        r = run(a.dir, ai=a.ai)
    except market_review.ReviewError as exc:
        print('次日关注生成失败: %s' % exc)
        return 1
    meta = r.get('ai_meta') or {}
    print('次日关注 %s：候选 %s 只，展示 %s 只 · AI %s' % (
        r['date'], r.get('candidates', 0), len(r['items']), meta.get('status') or '未调用'))
    if meta.get('error'):
        print('AI 未生成：%s' % meta['error'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
