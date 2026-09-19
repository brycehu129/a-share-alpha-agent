"""盘前"买入计划"卡片：把当天新冻结的、允许模拟成交的计划整理成一条能直接读懂的消息。Python 3.9+，只用标准库。

一张卡片回答你要的几件事：**买哪只、在什么价位买、止损在哪、目标价是多少、最多持有几天、仓位多大、有没有夜间公告**。
所有价位都相对冻结时的参考价（昨收）估算，实际以成交价为准；止损/目标是**这只股票自己的波动率（ATR）**定的，不是统一比例。

**每条消息都带证据状态**：这些是规则生成的研究假设，不是已验证的买入建议。随机基线（策略 vs 随手抽样）
还没有足够的前瞻数据时，消息里会明说"样本不足/目前无法区分"，不会因为想让信号"好用"而隐去。
"""
import os
from datetime import datetime

from collect_quotes import CST

TRACK_LABEL = {'breakout': '突破', 'pullback': '回调反弹'}
NEWS_TEXT = {'clear': '夜间无高风险公告', 'flag': '夜间有需留意的公告（见下）', 'unverified': '夜间公告未核验（请自己看一眼公告）',
             None: '未做夜间公告核查'}


def evidence_line(report):
    b = (report.get('baseline') or {})
    result = b.get('primary_result') or {}
    verdict = {'better': '前瞻上策略优于随机', 'worse': '前瞻上策略不如随机', 'indistinguishable': '前瞻上目前无法区分策略与随机'}.get(
        result.get('verdict'))
    if verdict:
        return '证据状态：%s（%d 个日期组）。' % (verdict, result.get('days', 0))
    resolved = b.get('resolved')
    return '证据状态：前瞻样本不足（已验收基线 %s 份，需要至少 15 个日期组），策略是否优于随机尚无结论。' % (resolved if resolved is not None else 0)


def _money_range(lo, hi):
    return '%.2f' % hi if lo is None else '%.2f–%.2f' % (lo, hi)


def card(f, index):
    lv = f['plan_levels']
    kind = TRACK_LABEL.get(f['strategy_type'], f['strategy_type'])
    size = lv['sizing']
    zone = _money_range(lv['entry_low'], lv['entry_high'])
    entry_rule = '现价升到区间内（向上确认后）买入，超出上沿不追' if f['strategy_type'] == 'breakout' \
        else '现价回落到上沿以内就买（不追高）；同时不能跌破 MA20'
    lines = ['%s %s %s（%s，排名 %s）' % ('①②③④⑤'[index] if index < 5 else '·', f['name'], f['symbol'], kind, f.get('rank')),
             ' 入场：%s，区间 %s 元，仅 %s–%s 有效，当日没触发就放弃' % (entry_rule, zone, lv['window'][0], lv['window'][1]),
             ' 作废：跌破 %.2f 当日放弃' % lv['void_price'],
             ' 止损：−%.1f%%（参考价下约 %.2f） 目标：+%.1f%%（参考价上约 %.2f） 最长持有 %d 个交易日（%s）' % (
                 lv['stop_pct'] * 100, lv['stop_price_at_reference'], lv['target_pct'] * 100, lv['target_price_at_reference'],
                 lv['hold_sessions'], '按该股 ATR %.1f%% 定' % (lv['atr_pct'] * 100) if lv.get('atr_pct') else '典型波动估算'),
             ' 仓位：约占净值 %.0f%%（%s），触发止损最多亏约 %.2f%% 净值（不含成本）；盈亏平衡胜率约 %.0f%%' % (
                 size['position_pct'],
                 '被单只上限 %.0f%% 卡住，风险预算本来允许 %.0f%%' % (size['weight_cap_pct'], size['risk_cap_pct'])
                 if size['binding'] == 'weight_cap' else '被单笔风险预算卡住，单只上限是 %.0f%%' % size['weight_cap_pct'],
                 size['max_loss_pct'], lv['breakeven_win_rate_pct'])]
    news = f.get('news')
    text = NEWS_TEXT.get(news['status'] if news else None)
    if news and news['status'] == 'clear':
        text = '夜间无高风险公告' + ('，新发布的公告供参考：' if news['items'] else '（也没有新公告）')
    lines.append(' 公告：' + text)
    if news and news['items']:
        for item in news['items'][:2]:
            lines.append('   · [%s] %s' % (item['time'][11:16], item['title']))
    return '\n'.join(lines)


def build_message(report, now=None):
    now = now or datetime.now(CST)
    new = set(report.get('new_forecast_ids') or [])
    plans = [f for f in report.get('forecasts', []) if f['id'] in new and f.get('paper_eligible')]
    plans.sort(key=lambda f: f.get('rank') or 99)
    head = '[Alpha Shadow 盘前买入计划] %s（选股 %s，依据 %s 收盘数据）' % (
        now.strftime('%m-%d %H:%M'), report.get('selection_version'), (report.get('screen') or {}).get('cutoff', '—'))
    lines = [head, evidence_line(report)]
    nc = report.get('news_check')
    if nc:
        lines.append('夜间公告核查：查了 %d 只，剔除 %d 只，需留意 %d 只，未核验 %d 只。' % (
            nc['checked'], len(nc['blocked']), nc['flagged'], nc['unverified']))
        for b in nc['blocked'][:5]:
            why = '；'.join('%s（%s）' % (i['reason'], i['title'][:30]) for i in b['items'][:1])
            lines.append(' 已剔除 %s %s：%s' % (b['name'], b['symbol'], why))
    if plans:
        lines.append('')
        lines += [card(f, i) for i, f in enumerate(plans)]
    else:
        reasons = sorted({r for f in report.get('forecasts', []) if f['id'] in new for r in f.get('plan_reasons', [])})
        lines.append('今天没有允许成交的计划。' + ('原因：' + '；'.join(reasons[:4]) if reasons else ''))
    lines += ['', '以上由固定规则生成，是研究假设，不是已验证的买入建议、也不构成投资建议；盘中实际是否成交、是否触发止损止盈，'
              '以系统按实时行情的记录为准（看板"操作记录"）。']
    return '\n'.join(lines)


def push(report):
    """推送到企业微信；没配 webhook 就返回原因而不是抛异常。"""
    from postclose_report import chunk_text
    from wecom_push import load_config, send_wecom_message
    config_path = os.environ.get('CONFIG_PATH') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'webapp_config.json')
    url = load_config(config_path).get('webhook_url')
    if not url:
        return '未配置企业微信 webhook，未推送'
    chunks = chunk_text(build_message(report))
    for chunk in chunks:
        send_wecom_message(url, chunk)
    return '已推送 %d 段' % len(chunks)
