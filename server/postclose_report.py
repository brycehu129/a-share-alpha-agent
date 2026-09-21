"""盘后分析编排：实时快照 → 规则复核 → 大盘环境 → AI研判 → 存档/推送。

**报告只存在服务器本地，不进 git。** 本仓库是公开仓库，.history 会被推到公开的
market-data 分支；报告里含真实持仓的成本价和数量，进去就等于公开了自己的账户。
所以这里只读 .history（只读！不 commit、不 push），写到 server/data/private/。

顺带的好处：这个进程完全不碰 git，只在**读** .history 的那段时间拿一下和日线流程
同一把文件锁——否则可能正好撞上 cron_common.sh 的 `git reset --hard`，读到半个状态。

AI 调用失败不会让整份报告失败：规则层的结论（入场带、门槛、浮动盈亏）本身就有用，
降级成纯规则版照样出报告和推送，并在报告里写清楚 AI 为什么没跑成。
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import live_check
import live_quote
import market_context
import portfolio_book
from collect_quotes import CST
from dashboard_export import latest
from session_brief import calendar_state

VERSION = 'postclose-1'
LOCK_PATH = os.environ.get('PIPELINE_LOCK', '/var/lock/alpha-shadow-pipeline.lock')
LOCK_WAIT_SECONDS = 300
WECOM_CHUNK_BYTES = 1800   # 企业微信文本消息上限 2048 字节，留出余量
WECOM_MAX_CHUNKS = 8

VERDICT_LABEL = {
    'buy_tomorrow': '明日可考虑买入', 'watch': '继续观察', 'pass': '本轮放弃',
    'hold': '继续持有', 'add': '可考虑加仓', 'reduce': '减仓', 'exit': '清仓',
    'swing_t': '适合做T',
}
TONE_LABEL = {'risk_on': '偏多', 'neutral': '中性', 'risk_off': '偏空'}
TRACK_LABEL = {'breakout': '突破', 'pullback': '回调反弹'}


class _Lock:
    """和 cron_daily_agent.sh / cron_opening_observer.sh 用的是同一把锁。
    锁文件不存在（本机开发、非 Linux）就直接放行并记录，不阻断。"""

    def __init__(self, path, wait):
        self.path = path
        self.wait = wait
        self.handle = None
        self.note = None

    def __enter__(self):
        # cron_postclose.sh 已经用 fd 9 持有这把锁了。flock 是按"打开文件描述"
        # 算的，Python 在这里重新 open 会拿到不同的描述，于是自己等自己，一直
        # 卡到超时。由包装脚本用环境变量告诉我们它已经拿着了。
        if os.environ.get('PIPELINE_LOCK_HELD'):
            return self
        try:
            import fcntl
            import signal
            self.handle = open(self.path, 'a')

            def _timeout(_sig, _frame):
                raise TimeoutError('等待流水线锁超时')

            previous = signal.signal(signal.SIGALRM, _timeout)
            signal.alarm(self.wait)
            try:
                fcntl.flock(self.handle, fcntl.LOCK_EX)
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, previous)
        except (ImportError, OSError, ValueError, TimeoutError) as exc:
            if self.handle:
                self.handle.close()
                self.handle = None
            self.note = '未取得流水线文件锁(%s)：%s；如果日线流程正在更新归档，' \
                        '本次读到的可能不是完整状态。' % (self.path, type(exc).__name__)
        return self

    def __exit__(self, *_):
        if self.handle:
            import fcntl
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
        return False


def report_dir():
    return Path(portfolio_book.default_dir()) / 'postclose'


def save_report(report):
    directory = report_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (report['id'] + '.json')
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    markdown = render(report)
    md_path = path.with_suffix('.md')
    md_path.write_text(markdown, encoding='utf-8')
    try:
        os.chmod(md_path, 0o600)
    except OSError:
        pass
    return path, markdown


def latest_report():
    directory = report_dir()
    if not directory.is_dir():
        return None
    files = sorted(directory.glob('*.json'))
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding='utf-8'))


def build(history, run_id, now=None):
    now = now or datetime.now(CST)
    report = {'id': run_id, 'version': VERSION, 'generated_at': now.isoformat(),
              'status': 'partial', 'issues': []}

    holdings = portfolio_book.load('holdings')
    watchlist = portfolio_book.load('watchlist')

    with _Lock(LOCK_PATH, LOCK_WAIT_SECONDS) as lock:
        if lock.note:
            report['issues'].append(lock.note)
        report['calendar'] = calendar_state(history, now.strftime('%Y%m%d'))
        _, agent = latest(history, 'agent')

    agent = agent or {}
    candidate_symbols = [c['symbol'] for c in agent.get('candidates', [])]
    agent_date = (agent.get('screen') or {}).get('cutoff')
    if agent_date and agent_date != now.date().isoformat():
        report['issues'].append(
            '候选池数据截至 %s，不是今天（%s）——可能是今天的日线流程还没跑完，'
            '或今天休市。下面的候选判断基于那一天的收盘特征。'
            % (agent_date, now.date().isoformat()))
    if not agent:
        report['issues'].append('本地归档里没有候选池报告，本次只分析持仓和自选股。')

    symbols = sorted({h['symbol'] for h in holdings} | {w['symbol'] for w in watchlist}
                     | set(candidate_symbols))
    if not symbols:
        report['issues'].append('持仓、自选股和候选池都是空的，没有可分析的标的。')
        report['status'] = 'empty'
        return report

    snapshot = live_quote.snapshot(symbols + market_context.CN_INDICES + market_context.OVERSEAS)
    report['session'] = snapshot['session']
    report['session_label'] = live_quote.SESSION_LABEL.get(snapshot['session'], snapshot['session'])
    if snapshot['failures']:
        report['issues'].append('%d 只股票未取得有效实时报价：%s'
                                % (len(snapshot['failures']),
                                   ','.join(f['symbol'] for f in snapshot['failures'][:8])))
    if snapshot['stale_symbols'] and snapshot['session'] in ('morning', 'afternoon'):
        report['issues'].append('盘中时段但部分报价已超过15分钟未更新：'
                                + ','.join(snapshot['stale_symbols'][:8]))

    check = live_check.check(history, snapshot['quotes'], holdings, watchlist, agent)
    context = market_context.collect(history, check['rows'], snapshot)

    report['snapshot'] = {k: snapshot[k] for k in
                          ('fetched_at', 'session', 'status', 'failures', 'stale_symbols', 'requests')}
    report['check'] = check
    report['market'] = context
    report['counts'] = {'holdings': len(holdings), 'watchlist': len(watchlist),
                        'candidates': len(candidate_symbols), 'quoted': len(snapshot['quotes'])}

    import ai_analyst
    ai, ai_meta = ai_analyst.analyze(check, context, holdings, watchlist)
    report['ai'] = ai
    report['ai_meta'] = ai_meta
    if ai is None:
        report['issues'].append('AI 研判未生成（%s：%s）；以下只有规则层结论。'
                                % (ai_meta.get('status'), ai_meta.get('error', '')))
    else:
        report['issues'].extend(ai_meta.get('validation_issues', []))
    report['status'] = 'ready' if ai is not None and not snapshot['failures'] else 'partial'
    report['generated_at'] = datetime.now(CST).isoformat()
    return report


def _row_by_symbol(report):
    return {r['symbol']: r for r in report.get('check', {}).get('rows', [])}


def render(report):
    now = report['generated_at']
    lines = ['# A股盘后分析 ' + now[:10], '',
             '生成 %s · %s · 交易日状态 %s · %s' % (
                 now, report.get('session_label', '—'),
                 {'open': '开市', 'closed': '休市', 'unknown': '未核验'}.get(report.get('calendar'), '—'),
                 report['status']), '']

    ai = report.get('ai')
    rows = _row_by_symbol(report)

    market = report.get('market') or {}
    if market:
        lines += ['## 大盘', '']
        if ai and ai.get('market'):
            m = ai['market']
            lines += ['**AI 研判：%s**' % TONE_LABEL.get(m['tone'], m['tone']), '', m['summary'], '']
            lines += ['- ' + p for p in m.get('key_points', [])]
            if m.get('tomorrow_watch'):
                lines += ['', '明日观察点：']
                lines += ['- ' + p for p in m['tomorrow_watch']]
            lines += ['']
        lines += ['| 指数 | 最新 | 涨跌 | 日期 |', '|---|---:|---:|---|']
        for r in market.get('indices', []) + market.get('overseas', []):
            suffix = '' if r['timezone_verified'] else '（时区未核验）'
            lines.append('| %s | %s | %s%% | %s%s |' % (r['label'], r['last'], r['change_pct'],
                                                        r['quote_date'], suffix))
        b = market.get('breadth') or {}
        if b.get('counted'):
            lines += ['', '全市场宽度（来源 %s，报价日期 %s）：上涨 %d / 下跌 %d / 平 %d，'
                          '上涨占比 %s%%；近似涨停 %d、跌停 %d。'
                      % (b.get('source_generated_at'), ','.join(b.get('quote_dates', {})),
                         b['up'], b['down'], b['flat'], b['up_pct'],
                         b['limit_up_approx'], b['limit_down_approx']),
                      '', b['note']]
        else:
            lines += ['', '全市场宽度：' + b.get('note', '不可用')]
        industries = (market.get('industries') or {}).get('rows', [])
        if industries:
            lines += ['', '样本内行业表现（前5 / 后3）：',
                      '| 行业 | 只数 | 涨幅中位数 | 上涨占比 |', '|---|---:|---:|---:|']
            for r in industries[:5] + industries[-3:] if len(industries) > 8 else industries:
                lines.append('| %s | %d | %s%% | %s%% |' % (r['industry'], r['members'],
                                                            r['median_change_pct'], r['positive_pct']))
            lines += ['', (market['industries']['scope'])]
        lines += ['']

    def section(title, key, role):
        out = ['## ' + title, '']
        judgements = {j['symbol']: j for j in (ai or {}).get(key, [])} if ai else {}
        members = [r for r in report.get('check', {}).get('rows', []) if role in r['roles']]
        if not members:
            return out + ['（空）', '']
        for row in members:
            q, pf = row['quote'], row['price_facts']
            head = '### %s %s' % (row['name'], row['symbol'])
            j = judgements.get(row['symbol'])
            if j:
                head += '　— **%s**（依据强度 %d/5）' % (
                    VERDICT_LABEL.get(j['verdict'], j['verdict']), j['confidence'])
            out += [head, '',
                    '现价 %s，涨跌 %s%%；MA5偏离 %s%%，MA20偏离 %s%%，距20日高点 %s%%。' % (
                        q['last'], q['change_pct'], pf.get('ma5_deviation_pct'),
                        pf.get('ma20_deviation_pct'), pf.get('distance_to_high20_pct'))]
            if row.get('holding'):
                h = row['holding']
                out += ['持仓 %d 股，成本 %s，市值 %s，浮动盈亏 %s（%s%%）。' % (
                    h['shares'], h['cost_price'], h['market_value'],
                    h['unrealized_pnl'], h['unrealized_pct'])]
            if row.get('strategy'):
                s = row['strategy']
                p = s.get('probability') or {}
                prob = ('%s%%（%s，样本%s/日期组%s）' % (p.get('probability'), p.get('source'),
                                                      p.get('n'), p.get('cohorts'))
                        if p.get('probability') is not None else '样本不足，不给数字')
                out += ['策略：%s track，综合分 %s，行业 %s；研究胜率 %s。' % (
                    TRACK_LABEL.get(s.get('track'), s.get('track')), s.get('score'),
                    s.get('industry'), prob)]
            gates = row.get('live_gates') or {}
            if gates:
                out += ['用最新价重算的门槛：' + '；'.join(
                    '%s %s（%s）' % (name, '参考' if ok is None else ('通过' if ok else '**不通过**'), detail)
                    for name, (ok, detail) in gates.items())]
            if row.get('plan'):
                pl = row['plan']
                out += ['冻结计划：参考价 %s，入场带 %s–%s，当前偏离 %s%%，%s。' % (
                    pl.get('reference_price'), pl.get('entry_low'), pl.get('entry_high'),
                    pl.get('gap_pct'), '在带内' if pl.get('in_band') else '**已超出入场带**')]
            lf = row.get('limit_facts') or {}
            if lf.get('to_limit_up_pct') is not None:
                out += ['距涨停 %s%%，距跌停 %s%%。' % (lf['to_limit_up_pct'], lf.get('to_limit_down_pct'))]
            if j:
                out += ['', '**研判**：' + j['reason'], '', '**风险**：' + j['risks'],
                        '', '价位参考：入场 %s ｜ 止损 %s ｜ 目标 %s' % (
                            j['key_levels']['entry_zone'], j['key_levels']['stop'],
                            j['key_levels']['target'])]
            for issue in row['issues']:
                out += ['', '⚠ ' + issue]
            out += ['']
        return out

    lines += section('我的持仓', 'holdings', 'holding')
    lines += section('候选池', 'candidates', 'candidate')

    watch_only = [r for r in report.get('check', {}).get('rows', [])
                  if 'watchlist' in r['roles'] and 'holding' not in r['roles']
                  and 'candidate' not in r['roles']]
    if watch_only:
        lines += ['## 自选股（非持仓、不在候选池）', '',
                  '| 股票 | 现价 | 涨跌 | MA20偏离 | 备注 |', '|---|---:|---:|---:|---|']
        for row in watch_only:
            lines.append('| %s %s | %s | %s%% | %s%% | %s |' % (
                row['name'], row['symbol'], row['quote']['last'], row['quote']['change_pct'],
                row['price_facts'].get('ma20_deviation_pct'),
                (row.get('watch') or {}).get('note') or '—'))
        lines += ['', '这些股票不在策略候选池里，没有经过两条 track 的规则筛选，'
                      '上面的数字只是行情事实。', '']

    lines += ['## 数据与限制', '']
    for item in report['issues']:
        lines.append('- ' + item)
    for item in (ai or {}).get('data_caveats', []):
        lines.append('- （AI标注）' + item)
    for item in report.get('check', {}).get('caveats', []):
        lines.append('- ' + item)
    for item in (report.get('market') or {}).get('caveats', []):
        lines.append('- ' + item)

    meta = report.get('ai_meta') or {}
    if meta.get('status') == 'ok':
        lines += ['', '_AI 研判：%s · effort=%s · prompt=%s · 输入%s/输出%s tokens。'
                      'AI 的结论是研究假设，未经任何前瞻验证，不是投资建议。_'
                  % (meta.get('model'), meta.get('effort'), meta.get('prompt_version'),
                     meta.get('input_tokens'), meta.get('output_tokens'))]
    lines += ['', '_本报告只做研究参考，不构成投资建议。系统不会自动下单。_', '']
    return '\n'.join(lines)


def summarize_for_push(report):
    """企业微信用的短摘要。完整报告去网页看——群消息塞不下也没人读。"""
    ai = report.get('ai') or {}
    rows = _row_by_symbol(report)
    lines = ['【A股盘后分析 %s】%s' % (report['generated_at'][:10], report['status'])]
    if ai.get('market'):
        m = ai['market']
        lines += ['', '大盘：%s — %s' % (TONE_LABEL.get(m['tone'], m['tone']), m['summary'])]
    else:
        lines += ['', '（AI 研判未生成，以下为规则层结论）']

    def block(title, key, role, highlight):
        out = []
        judgements = {j['symbol']: j for j in ai.get(key, [])}
        members = [r for r in report.get('check', {}).get('rows', []) if role in r['roles']]
        if not members:
            return out
        out.append('')
        out.append(title)
        for row in members:
            j = judgements.get(row['symbol'])
            verdict = VERDICT_LABEL.get(j['verdict'], j['verdict']) if j else '—'
            mark = '★' if j and j['verdict'] in highlight else '·'
            extra = ''
            if row.get('holding'):
                extra = ' 浮动%s%%' % row['holding']['unrealized_pct']
            elif row.get('plan') and not row['plan'].get('in_band'):
                extra = ' 已超出入场带'
            out.append('%s %s %s %s%% → %s%s' % (
                mark, row['name'], row['quote']['last'], row['quote']['change_pct'], verdict, extra))
        return out

    lines += block('持仓：', 'holdings', 'holding', ('exit', 'reduce', 'add', 'swing_t'))
    lines += block('候选：', 'candidates', 'candidate', ('buy_tomorrow',))
    if report['issues']:
        lines += ['', '提示：' + report['issues'][0]]
    lines += ['', '完整报告见后台 /postclose。研究参考，非投资建议。']
    return '\n'.join(lines)


TRUNCATION_NOTICE = '\n…（内容过长已截断，完整报告见后台）'


def _head(text, limit):
    """按 UTF-8 字节截断，绝不切出半个汉字。"""
    return text.encode('utf-8')[:limit].decode('utf-8', 'ignore')


def chunk_text(text, limit=WECOM_CHUNK_BYTES, max_chunks=WECOM_MAX_CHUNKS):
    """按行切成每块都**不超过** limit 字节。单行超长就按字节硬切，不丢内容。

    两个容易写错的地方：截断提示语本身也占字节，必须先给它腾出位置再拼，否则
    最后一块会正好超限被企业微信拒收；长行硬切前要先把已攒的 current 冲掉，
    不然切出来的片段会插到前面的内容之前，顺序就乱了。"""
    chunks, current = [], ''

    def flush():
        nonlocal current
        if current:
            chunks.append(current)
            current = ''

    for line in text.split('\n'):
        while len(line.encode('utf-8')) > limit:
            flush()
            head = _head(line, limit)
            chunks.append(head)
            line = line[len(head):]
        candidate = (current + '\n' + line) if current else line
        if len(candidate.encode('utf-8')) > limit:
            flush()
            current = line
        else:
            current = candidate
    flush()

    if len(chunks) > max_chunks:
        chunks = chunks[:max_chunks]
        room = limit - len(TRUNCATION_NOTICE.encode('utf-8'))
        chunks[-1] = _head(chunks[-1], max(room, 0)) + TRUNCATION_NOTICE
    return chunks


def push(report):
    from wecom_push import ConfigError, PushError, load_config, send_wecom_message
    import webapp
    try:
        webhook = load_config(webapp.config_path()).get('webhook_url')
    except (ConfigError, OSError, ValueError) as exc:
        return {'sent': False, 'reason': '读取 webhook 配置失败：%s' % exc}
    if not webhook:
        return {'sent': False, 'reason': '尚未配置企业微信 webhook'}
    chunks = chunk_text(summarize_for_push(report))
    for index, chunk in enumerate(chunks):
        try:
            send_wecom_message(webhook, chunk)
        except PushError as exc:
            return {'sent': index > 0, 'chunks': index, 'reason': str(exc)}
    return {'sent': True, 'chunks': len(chunks)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--history', type=Path, required=True)
    p.add_argument('--run-id', help='默认用当前时间戳')
    p.add_argument('--dry-run', action='store_true', help='不推送企业微信')
    p.add_argument('--no-ai', action='store_true', help='跳过AI调用，只出规则层报告')
    a = p.parse_args()

    run_id = a.run_id or datetime.now(CST).strftime('%Y%m%d%H%M%S') + '-1'
    if not re.fullmatch(r'\d+-\d+', run_id):
        raise SystemExit('运行编号格式应为 数字-数字')
    import llm_settings
    llm_settings.apply()      # 页面保存的 key/模型优先于环境变量
    if a.no_ai:
        # 两个后端的 key 都要去掉：只去 Anthropic 的话，配了 OpenRouter 时 --no-ai 会照样调用并花钱。
        # 用 environ 里的值直接删而不再 apply，所以页面保存的 key 也不会被重新放回来。
        os.environ.pop('ANTHROPIC_API_KEY', None)
        os.environ.pop('OPENROUTER_API_KEY', None)

    report = build(a.history, run_id)
    if report['status'] == 'empty':
        print('\n'.join(report['issues']), file=sys.stderr)
        return 1
    path, _ = save_report(report)
    print('盘后报告已保存：%s（%s）' % (path, report['status']))
    for issue in report['issues']:
        print('  - ' + issue)
    if a.dry_run:
        print('--dry-run：跳过企业微信推送')
    else:
        result = push(report)
        print('推送：' + ('成功，%d 段' % result['chunks'] if result['sent']
                          else '未发送（%s）' % result.get('reason')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
