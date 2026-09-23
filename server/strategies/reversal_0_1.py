"""select-rev-0.1：超跌反弹（研究标签，只留档、不进约束账户）。

ROADMAP.md 第 3 项的由来：本地 35 个交易日回放里，最弱 10% 行业的随机抽样屏障净收益
最好、强势行业内反而最差——一段反转行情。select-0.5 的前提是"强势行业内找短线动量
延续"；如果前瞻基线（weak_industry/mid_industry）也系统性地显示"越弱越反弹"，
这个前提就要重新考虑。与其拍脑袋改 select-0.5 的行业闸门方向，不如让这条相反方向的
track 和它并行跑、吃同一套证据体系（contract_labels/baseline，按 selection_version
分组，互不混池），4 周后用数据回答"强者恒强还是弱者反弹"，而不是猜。

TRADABLE = False：这条 track 的候选永远只留档做研究，不会进约束账户，也不会被
alpha_engine 计入 paper_eligible 名额。

阈值是起始设定，和 shortterm_model.py 的 BREAKOUT/PULLBACK 一开始一样，没有走过
walk-forward 验证——先收集证据，不是先追求准。
"""
from collections import Counter

from alpha_model import clip, percentiles
from shortterm_model import LIQUIDITY_MIN_PCT, SHORT_POLICY, stock_features

STRATEGY_ID = 'select-rev-0.1'
NAME = '超跌反弹（研究，不成交）'
TARGET = '下一可验证交易日开盘至第3个交易间隔收盘（1/2日辅助验收），扣0.5%假设往返成本后盈利且跑赢同期沪深300价格指数'
ARCHIVE_SIZE = 10
TRADABLE = False
# 风控红线（本金/持仓数/单笔风险等）是账户层面的常量，不是策略层面的——这条 track
# 永不实际成交，这里只是借用同一份形状供 plan_levels()/sizing() 展示止损止盈位。
POLICY = SHORT_POLICY

# "最弱"不是"没那么强"：中位数超额必须为负、多数股票在跌，才算真正在跌的行业。
REVERSAL = {
    'deviation_max': -8.0,        # 偏离MA20不超过 -8%（超跌）
    'excess20_max': -5.0,         # 20日跑输沪深300至少5pp（确认弱势，不是噪音）
    'volume_ratio_5d_min': 1.1,   # 今日温和放量：完全没人接的票，反弹缺乏承接
    'volume_ratio_5d_max': 2.5,   # 上限排除恐慌性放量抛售——那不是反弹信号
}


def weak_industries(industry_rows):
    """strong_industries() 的镜像：不是取前10%，是取最弱10%，且要求中位数超额为负、
    近5日上涨比例不过半——排除"只是没那么强"的行业，只留"真的在跌"的。"""
    if not industry_rows:
        return {}
    ranked = sorted(industry_rows, key=lambda r: (r['score'], r['name']))  # 升序，最弱在前
    bottom_n = max(1, -(-len(ranked) * 10 // 100))  # ceil(10%)
    return {r['name']: r for r in ranked[:bottom_n] if r['median_excess20_pp'] < 0 and r['positive5_pct'] <= 50}


def screen_reversal(stocks, series, benchmark, cutoff, industry_rows):
    weak = weak_industries(industry_rows)
    candidates, excluded = [], {}
    for code, r in stock_features(stocks, series, benchmark, cutoff).items():
        reason = None
        if r['industry'] not in weak:
            reason = '行业未进入最弱10%'
        elif r['liquidity_pct'] < LIQUIDITY_MIN_PCT:
            reason = '相对流动性后20%'
        elif r['deviation_pct'] > REVERSAL['deviation_max']:
            reason = '偏离MA20不足，尚未超跌'
        elif r['excess20_pp'] > REVERSAL['excess20_max']:
            reason = '20日超额跑输不够多，不算确认弱势'
        elif not REVERSAL['volume_ratio_5d_min'] <= r['volume_ratio_5d'] <= REVERSAL['volume_ratio_5d_max']:
            reason = '今日量比不在企稳区间'
        elif not r['is_green']:
            reason = '当日未收阳，无反转确认'
        if reason:
            excluded[code] = reason
            continue
        raw = 50 + abs(r['deviation_pct']) * 1.0 + (r['volume_ratio_5d'] - 1) * 5
        candidates.append({**r, 'strategy_type': 'reversal', 'score': round(clip(raw), 2),
                            'raw_score': round(raw, 4), 'hotmoney': None,
                            'reasons': ['行业最弱10%', f"偏离MA20≤{REVERSAL['deviation_max']}%（超跌）",
                                        f"20日超额≤{REVERSAL['excess20_max']}pp（确认弱势）",
                                        '温和放量企稳', '当日收阳反转确认']})
    candidates.sort(key=lambda r: (-r['raw_score'], r['symbol']))
    return {'candidates': candidates, 'excluded': excluded, 'exclusion_counts': dict(Counter(excluded.values()))}


def screen(world, tuning, hotmoney):
    """world: {'stocks','series','benchmark','cutoff','mid'}。不使用 hotmoney——游资净买入的
    打分含义是给"正在被资金追捧的动量票"加分，语义上不适用于"超跌无人问津"的反转候选，
    暂不接入（不是"没有数据=0分"的情况，是这条 track 目前完全不用这个维度）。"""
    mid = world['mid']
    result = screen_reversal(world['stocks'], world['series'], world['benchmark'], world['cutoff'],
                              mid['industries'])
    return {'cutoff': world['cutoff'], 'listed': mid['listed'], 'eligible': mid['eligible'], 'valid': mid['valid'],
            'complete': mid['complete'], 'coverage_pct': mid['coverage_pct'],
            'market_score': mid['market_score'], 'regime': mid['regime'],
            'market_score_pause': mid['tuning']['market_score_pause'],
            'exclusion_counts': result['exclusion_counts'], 'reversal': result}


def rank(screened, max_n):
    """只有一条 track，仍然走百分位（不是直接按 raw_score 排）——和 select_0_5.rank() 同一把尺子，
    下游报表的 rank_pct 字段含义才一致。"""
    rows = screened['reversal']['candidates']
    if not rows:
        return []
    pct = percentiles({r['symbol']: r['raw_score'] for r in rows})
    for r in rows:
        r['rank_pct'] = round(pct[r['symbol']], 2)
    ranked = sorted(rows, key=lambda r: (-r['rank_pct'], -r['liquidity_pct'], r['symbol']))
    return ranked[:max_n] if max_n else ranked


def track_of(candidate):
    return candidate['strategy_type']
