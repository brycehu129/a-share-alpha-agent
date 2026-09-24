"""系统健康告警：这套系统坏了，要让你知道。Python 3.9+，只用标准库。

**为什么需要它。** 盘中引擎每分钟跑一次、日线流程每天跑、备份每天跑……任何一个静默失败，你都得等到
"怎么今天没推送/没报告"才发现，而那时可能已经错过了一整个交易日。这里每 5 分钟检查一遍，出问题推企业微信。

**看"产出"，不看"退出码"。** 只查 systemd 单元有没有失败不够——引擎可能每分钟都"成功"退出，却什么都没做
（行情源全挂、日历读不到、没有需要监控的股票）。所以每项检查都找**该出现的证据**：盘中引擎的最后一轮
是不是 4 分钟内、今天的日线报告有没有生成、盘后报告有没有 AI 研判、备份是不是新的……systemd 失败单元只是
其中一项补充。

**告警要克制，否则你会开始无视它：**
- **去抖**：`warn` 要连续两次检查（约 5–10 分钟）都成立才推，瞬时抖动（比如一次 50 秒超时）不打扰你；
  `crit` 立即推。
- **不重复轰炸**：同一个问题 `crit` 每 2 小时最多再提醒一次，`warn` 每 12 小时一次；夜间（22:30–07:00）
  只发首次的 `crit`，`warn` 攒到早上。
- **恢复也告诉你**：问题消失时推一条"已恢复"，你就不用一直惦记着它还在不在。
- **一次检查只推一条消息**：多个问题合并成一条。
- **只在该运行的时候查**：非交易日、午休、开盘前不会因为"引擎没在跑"报警。

**局限，必须说清楚：**
1. 这个检查器和被检查的系统在同一台机器上。**机器整个挂了，它也发不出告警。**
   补救是外部心跳：`/health/deep` 是个不需要登录的端点（只返回 ok/stale/critical，不含任何细节），
   仓库里的 GitHub Actions 定时任务会定期访问它，异常时 GitHub 会给你发邮件——这是唯一独立于这台机器的一环。
2. 企业微信没配 webhook 时告警**发不出去**，只能在后台页面看到；页面和检查里都会明说。
3. 检查阈值（4 分钟、两次去抖等）是经验值，没在真实盘中校准过；第一周很可能要调。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path

import backup
import intraday_engine
import portfolio_book
from collect_quotes import CST

OK, WARN, CRIT, SKIP = 'ok', 'warn', 'crit', 'skip'
INTERVAL_MIN = 5                         # 定时器周期，心跳新鲜度按它算
HEARTBEAT_STALE_MIN = 20
ENGINE_MAX_AGE_S = 240                   # 盘中引擎最后一轮距现在超过这个数就是停了
WARN_CONFIRM_RUNS = 2
CRIT_REMIND_H = 2
WARN_REMIND_H = 12
QUIET_START, QUIET_END = clock_time(22, 30), clock_time(7, 0)
FREQUENT_UNITS = ('alpha-shadow-intraday', 'alpha-shadow-sentinel-analyst', 'alpha-shadow-flow',
                  'alpha-shadow-resilience')      # 高频（每分钟或每 5 分钟）：偶发失败不算严重
LLM_FATAL = {'authentication_error', 'payment_required', 'permission_denied', 'model_not_found'}
QUEUE_STUCK_MIN = 10
DISK_WARN_PCT, DISK_CRIT_PCT = 15, 5
CERT_WARN_DAYS, CERT_CRIT_DAYS = 45, 14
STATE_NAME = 'health-state.json'


class Ctx:
    """检查所需的一切环境，都可以在测试里替换。"""

    def __init__(self, now=None, history=None, private=None, run=None, calendar=None):
        self.now = now or datetime.now(CST)
        self.history = Path(history or os.environ.get('HISTORY_DIR') or '.history')
        self.private = Path(private or portfolio_book.default_dir())
        self.run = run or _run
        self._calendar = calendar

    @property
    def today(self):
        return self.now.date().isoformat()

    def calendar_state(self):
        if self._calendar:
            return self._calendar(self.now)
        from session_brief import calendar_state
        return calendar_state(self.history, self.now.strftime('%Y%m%d'))

    def engine_window(self):
        """现在是不是盘中引擎应该在连续运行的时段（已留出起止余量）。"""
        t = self.now.time()
        return clock_time(9, 33) <= t <= clock_time(11, 29) or clock_time(13, 3) <= t <= clock_time(14, 58)


def _run(argv, timeout=10):
    """子进程封装，便于测试替换。返回 (返回码, 输出)；命令不存在抛 FileNotFoundError。"""
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or '') + (p.stderr or '')


def check(key, title, level, message='', at=None):
    """at：这一项所依据的那次执行/产出的时间（ISO，北京时间）——「上次执行」列。实时读取的项传检查时刻，
    没有可依据的执行记录就留 None。什么时候该执行（schedule）是静态的，见 SCHEDULES，由 run_checks 补上。"""
    return {'key': key, 'title': title, 'level': level, 'message': message, 'at': at}


# 每项检查所对应的任务「什么时候该跑」，来自 systemd/*.timer；改了 timer 要一起改这里。
SCHEDULES = {
    'calendar': '每次检查时实时读取',
    'engine': '交易日 09:30–15:00 每分钟',
    'quotes': '随盘中引擎，每分钟一轮',
    'evaluators': '随盘中引擎，每分钟一轮',
    'sentinel_queue': '交易日盘中每分钟（第 30 秒）',
    'sentinel_ai': '交易日盘中每分钟（第 30 秒）',
    'daily': '工作日 15:35（18:20 与每小时 :17 补跑）',
    'premarket': '工作日 08:40（09:25 前须冻结计划）',
    'postclose': '工作日 16:30',
    'review': '工作日 16:30、17:30',
    'reconcile': '工作日 15:20',
    'alert_audit': '工作日 15:20',
    'backup': '每天 17:30',
    'units': '每次检查时实时查询',
    'disk': '每次检查时实时读取',
    'cert': '每次检查时实时读取',
    'webhook': '每次检查时实时读取',
    'llm': '每次检查时实时读取',
}


def _cst_iso(dt):
    return dt.astimezone(CST).isoformat()


# --- 各项检查 -----------------------------------------------------------------

def _read_jsonl_tail(path, n):
    try:
        lines = path.read_text(encoding='utf-8').splitlines()[-n:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _ran_ticks(path, n):
    """ticks 日志里既有跑完的轮，也有「时段内被挡掉」的跳过记录（ran=false，只用来解释为什么少跑）。
    引擎存活/行情/评估器检查只该看真正跑完的轮，否则一条跳过记录会被当成"最后一次成功运行"。"""
    return [t for t in _read_jsonl_tail(path, n * 3) if t.get('ran', True)][-n:]


def _monitored_symbols():
    try:
        return portfolio_book.all_symbols()
    except Exception:
        return []


def check_calendar(ctx):
    state = ctx.calendar_state()
    if state == 'unknown' and ctx.now.weekday() < 5:
        return check('calendar', '交易日历', WARN, '交易日历读不到（%s）。盘中引擎在这种状态下**不会运行**，报警窗口内不会有任何推送。'
                     % (ctx.history / 'tushare_data/calendars'), ctx.now.isoformat())
    return check('calendar', '交易日历', OK, '今天：%s' % state, ctx.now.isoformat())


def check_engine(ctx):
    title = '盘中引擎'
    if ctx.calendar_state() != 'open':
        return check('engine', title, SKIP, '今天不是确认开市的交易日')
    if not ctx.engine_window():
        return check('engine', title, SKIP, '不在连续运行时段')
    if not _monitored_symbols():
        return check('engine', title, SKIP, '没有持仓/自选，引擎无事可做')
    ticks = _ran_ticks(intraday_engine.data_dir() / ('ticks-%s.jsonl' % ctx.today), 12)
    if not ticks:
        path = intraday_engine.data_dir() / ('ticks-%s.jsonl' % ctx.today)
        skips = [t.get('skipped') for t in _read_jsonl_tail(path, 5) if t.get('skipped')]
        why = '，最近一次被跳过的原因：%s' % skips[-1] if skips else '（找不到 ticks-%s.jsonl）' % ctx.today
        return check('engine', title, CRIT, '今天到现在一轮都没有成功运行%s。' % why)
    last = datetime.fromisoformat(ticks[-1]['at'])
    age = (ctx.now - last).total_seconds()
    if age > ENGINE_MAX_AGE_S:
        return check('engine', title, CRIT, '盘中引擎已 %d 分钟没有成功运行（最后一轮 %s）。' % (age // 60, last.strftime('%H:%M:%S')), last.isoformat())
    return check('engine', title, OK, '最后一轮 %d 秒前（今天已 %d 轮）' % (age, len(ticks)), last.isoformat())


def check_quotes(ctx):
    title = '行情源'
    if ctx.calendar_state() != 'open' or not ctx.engine_window():
        return check('quotes', title, SKIP, '不在连续运行时段')
    ticks = _ran_ticks(intraday_engine.data_dir() / ('ticks-%s.jsonl' % ctx.today), 10)
    if len(ticks) < 5:
        return check('quotes', title, SKIP, '样本不足（%d 轮）' % len(ticks))
    bad = [t for t in ticks if t.get('failures') or t.get('fresh') == 0 or t.get('error')]
    if len(bad) * 2 >= len(ticks):
        return check('quotes', title, WARN, '最近 %d 轮里有 %d 轮取不到行情或行情不新鲜：盘中信号会不可靠，甚至完全静默。' % (len(ticks), len(bad)), ticks[-1].get('at'))
    return check('quotes', title, OK, '最近 %d 轮里 %d 轮有异常' % (len(ticks), len(bad)), ticks[-1].get('at'))


def check_evaluators(ctx):
    title = '盘中评估器'
    if ctx.calendar_state() != 'open' or not ctx.engine_window():
        return check('evaluators', title, SKIP, '不在连续运行时段')
    ticks = _ran_ticks(intraday_engine.data_dir() / ('ticks-%s.jsonl' % ctx.today), 10)
    errors = [e for t in ticks for e in (t.get('evaluator_errors') or [])]
    if errors:
        return check('evaluators', title, WARN, '最近有评估器报错（一个坏了不会影响别的，但它负责的信号已经静默）：%s' % errors[-1][:160], ticks[-1].get('at'))
    return check('evaluators', title, OK, '无报错', ticks[-1].get('at') if ticks else None)


def check_sentinel_queue(ctx):
    title = '哨兵研判队列'
    pending = ctx.private / 'sentinel' / 'pending'
    if not pending.is_dir() or ctx.calendar_state() != 'open':
        return check('sentinel_queue', title, SKIP, '无待研判项或非交易日')
    stuck = []
    for p in pending.glob('*.json'):
        try:
            created = datetime.fromisoformat(json.loads(p.read_text(encoding='utf-8'))['created_at'])
        except (OSError, ValueError, KeyError):
            continue
        if (ctx.now - created).total_seconds() > QUEUE_STUCK_MIN * 60:
            stuck.append((ctx.now - created).total_seconds() / 60)
    if stuck:
        return check('sentinel_queue', title, WARN, '%d 条待研判积压超过 %d 分钟（最久 %d 分钟）：研判进程可能没在跑，或 AI 一直失败。'
                     % (len(stuck), QUEUE_STUCK_MIN, max(stuck)))
    return check('sentinel_queue', title, OK, '没有积压')


def check_sentinel_ai(ctx):
    title = '哨兵 AI 研判'
    done = ctx.private / 'sentinel' / 'done'
    if not done.is_dir():
        return check('sentinel_ai', title, SKIP, '尚无研判记录')
    recent = []
    for p in sorted(done.glob('*.json'))[-30:]:
        try:
            item = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        finished = item.get('finished_at') or ''
        if finished[:10] == ctx.today:
            recent.append(item)
    failed = [i for i in recent if i.get('status') == 'ai_failed']
    if not recent:
        return check('sentinel_ai', title, SKIP, '今天还没有研判')
    last_at = max(i.get('finished_at') or '' for i in recent) or None
    tail = recent[-3:]
    if len(tail) >= 2 and all(i.get('status') == 'ai_failed' for i in tail):
        reason = ((tail[-1].get('meta') or {}).get('status')) or '未知'
        level = CRIT if reason in LLM_FATAL else WARN
        return check('sentinel_ai', title, level, '最近 %d 次 AI 研判都失败了（%s）%s' % (
            len(tail), reason, '：key 无效/余额不足/模型不存在，重试没有用，请到后台"推送配置"页检查。' if reason in LLM_FATAL else '。'), last_at)
    return check('sentinel_ai', title, OK, '今天 %d 次研判，%d 次失败' % (len(recent), len(failed)), last_at)


def _report_files(directory):
    return sorted(directory.glob('*.json')) if directory.is_dir() else []


def _report_time(name):
    """报告文件名 YYYYMMDDHHMMSS-N 的前 14 位是 UTC 时间戳；认不出来返回 None。"""
    try:
        return _cst_iso(datetime.strptime(name[:14], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc))
    except ValueError:
        return None


def check_daily_pipeline(ctx):
    title = '收盘流程（15:35）'
    if ctx.calendar_state() != 'open' or ctx.now.time() < clock_time(17, 15):
        return check('daily', title, SKIP, '尚未到检查时间或非交易日')
    names = [p.name for p in _report_files(ctx.history / 'agent')]
    # 报告文件名是 UTC 时间戳 YYYYMMDDHHMMSS-N。08:40 的盘前那轮也会写报告，不能拿它冒充收盘流程：
    # 收盘流程 15:35 北京时间 = 07:35 UTC，所以要找当天 07:30 UTC 之后的。
    utc_day = ctx.now.astimezone(timezone.utc).strftime('%Y%m%d')
    latest = _report_time(names[-1]) if names else None
    if any(n[:8] == utc_day and n[8:14] >= '073000' for n in names):
        return check('daily', title, OK, '今天收盘后的报告已生成', latest)
    return check('daily', title, CRIT, '今天 15:35/18:20 的收盘流程没有产出报告（agent/ 下没有今天的记录）：今天的验收、估值、证据和基线都没有更新。', latest)


def check_premarket_plans(ctx):
    """盘前选股（08:40）必须在执行器的 09:25 截止之前把今天的计划冻结出来——否则今天整天没有买入计划。"""
    title = '盘前选股（08:40）'
    if ctx.calendar_state() != 'open' or not (clock_time(9, 26) <= ctx.now.time() <= clock_time(16, 0)):
        return check('premarket', title, SKIP, '尚未到检查时间或非交易日')
    from tushare_sync import read
    files = sorted((ctx.history / 'predictions').glob('select-*.json'), reverse=True) if (ctx.history / 'predictions').is_dir() else []
    # 文件名是 select-<版本>-<截止日>-<代码>：按截止日倒序看最新的一批，看它们是不是今天冻结的
    import re
    def cutoff_of(path):
        found = re.search(r'\d{4}-\d{2}-\d{2}', path.name)
        return found.group(0) if found else ''
    files = sorted(files, key=cutoff_of, reverse=True)[:12]
    for path in files:
        try:
            created_at = read(path).get('created_at') or ''
        except Exception:
            continue
        if created_at[:10] == ctx.today:
            return check('premarket', title, OK, '今天的买入计划已冻结', created_at)
    ops = ctx.history / 'operations.json'
    if ops.exists():
        try:
            checkpoints = json.loads(ops.read_text(encoding='utf-8')).get('checkpoints', {})
        except (OSError, ValueError):
            checkpoints = {}
        phase = 'premarket_after_open' if ctx.now.time() >= clock_time(9, 35) else 'premarket'
        prior = checkpoints.get(phase) or checkpoints.get('premarket') or {}
        if prior.get('report_date') == ctx.today and prior.get('status') in ('ready', 'closed'):
            return check('premarket', title, OK, '盘前流程已完成；今天没有可冻结的买入计划。', prior.get('finished_at') or prior.get('updated_at'))
    return check('premarket', title, CRIT, '今天到现在没有冻结出任何买入计划：盘前选股没有跑完（或选出 0 只）。'
                                            '执行器 09:25 之后不再等计划，今天不会有条件入场。')


def check_postclose(ctx):
    title = '盘后分析（16:30）'
    if ctx.calendar_state() != 'open' or ctx.now.time() < clock_time(17, 15):
        return check('postclose', title, SKIP, '尚未到检查时间或非交易日')
    files = _report_files(ctx.private / 'postclose')
    latest = None
    if files:
        try:
            latest = json.loads(files[-1].read_text(encoding='utf-8'))
        except (OSError, ValueError):
            latest = None
    generated_at = (latest or {}).get('generated_at') or None
    if not latest or (generated_at or '')[:10] != ctx.today:
        return check('postclose', title, WARN, '今天的盘后报告没有生成。', generated_at)
    meta = latest.get('ai_meta') or {}
    if meta.get('status') not in (None, 'ok'):
        return check('postclose', title, WARN, '今天的盘后报告生成了，但**没有 AI 研判**（%s：%s）。'
                     % (meta.get('status'), str(meta.get('error') or '')[:120]), generated_at)
    return check('postclose', title, OK, '已生成，带 AI 研判', generated_at)


def check_review(ctx):
    """市场复盘数据（涨跌停池、龙虎榜、次日关注、上一交易日的兑现结算）。看板这一块曾因为数据源要手动
    同步而长期缺失，所以 17:30 那轮之后必须有今天的文件，且涨停池、龙虎榜都不是空的。"""
    title = '市场复盘数据（16:30 / 17:30）'
    if ctx.calendar_state() != 'open' or ctx.now.time() < clock_time(17, 45):
        return check('review', title, SKIP, '尚未到检查时间或非交易日')
    import market_review
    import watch_outcome
    directory = ctx.private.parent / 'market_review'
    review = market_review.load_latest(directory)
    generated_at = (review or {}).get('fetched_at') or None
    if not review or review.get('trade_date') != ctx.now.strftime('%Y%m%d'):
        return check('review', title, WARN, '今天的市场复盘数据没有生成：看板的涨跌停池、龙虎榜会停在旧的一天。'
                     '`journalctl -u alpha-shadow-review -n 50` 看原因。', generated_at)
    missing = [name for name, ok in (('涨停池', (review['pools'].get('zt') or {}).get('total')),
                                     ('龙虎榜', ((review.get('lhb') or {}).get('rows'))),
                                     ('次日关注', (review.get('next_day_watch') or {}).get('items'))) if not ok]
    # 只有存在「待结算的上一交易日名单」时才要求今天的 prev_watch；首次上线/长假后没有可结算的
    # 上一天，静默跳过，不告警。
    prior = watch_outcome.prior_watch_file(directory, review['trade_date'])
    if prior is not None and not (review.get('prev_watch') or {}).get('items'):
        missing.append('上一交易日兑现结算')
    if missing:
        return check('review', title, WARN, '今天的复盘数据缺：%s（接口失败或尚未发布）。' % '、'.join(missing), generated_at)
    return check('review', title, OK, '已生成，涨停池、龙虎榜、次日关注、上一交易日兑现结算齐全', generated_at)


def check_reconcile(ctx):
    title = '情景收盘对账（15:20）'
    if ctx.calendar_state() != 'open' or ctx.now.time() < clock_time(15, 45):
        return check('reconcile', title, SKIP, '尚未到检查时间或非交易日')
    done = ctx.private / 'sentinel' / ('reconcile-%s.json' % ctx.today)
    if done.exists():
        return check('reconcile', title, OK, '今天已对账', _cst_iso(datetime.fromtimestamp(done.stat().st_mtime, timezone.utc)))
    return check('reconcile', title, WARN, '今天没有情景对账记录：命中率统计会缺一天。')


def check_alert_audit(ctx):
    """规则层告警的机制核对（alert_ledger.py，和情景对账同一个 15:20 窗口）。任何不一致
    都意味着代码本身有 bug（事件丢了/重了、推送文字对不上观测价、watch 档漏刷屏、重新武装
    间隔被违反），不是"数据不够"，所以升级成 CRIT，而不是像结果标签那样只是 WARN。"""
    title = '哨兵告警核对（15:20）'
    if ctx.calendar_state() != 'open' or ctx.now.time() < clock_time(15, 45):
        return check('alert_audit', title, SKIP, '尚未到检查时间或非交易日')
    path = ctx.private / 'sentinel' / ('alert_audit-%s.json' % ctx.today)
    if not path.exists():
        return check('alert_audit', title, WARN, '今天没有告警核对记录：机制核对和结果标签都会缺一天。')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except ValueError:
        return check('alert_audit', title, WARN, '告警核对文件损坏，读不出来。')
    at = _cst_iso(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
    mismatches = (data.get('audit') or {}).get('mismatches') or []
    if mismatches:
        return check('alert_audit', title, CRIT, '发现 %d 处不一致（%s……），告警管线本身有 bug，不是数据不够。'
                     % (len(mismatches), mismatches[0].get('why', '')[:60]), at)
    checked = (data.get('audit') or {}).get('checked', 0)
    return check('alert_audit', title, OK, '今天核对 %d 条告警，全部一致' % checked, at)


def check_backup(ctx):
    level, text = backup.health()
    last_ok = backup.read_status().get('last_success_at') or None
    if level == 'warn':
        return check('backup', '私有数据备份', WARN, text, last_ok)
    return check('backup', '私有数据备份', SKIP if level == 'none' else OK, text, last_ok)


def check_units(ctx):
    title = 'systemd 失败单元'
    try:
        code, out = ctx.run(['systemctl', 'list-units', '--failed', '--no-legend', '--plain', 'alpha-shadow-*'])
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return check('units', title, SKIP, '本机没有 systemctl（开发环境）')
    failed = [line.split()[0] for line in out.splitlines() if line.strip() and 'alpha-shadow' in line]
    if not failed:
        return check('units', title, OK, '没有失败单元', ctx.now.isoformat())
    frequent = all(any(u.startswith(f) for f in FREQUENT_UNITS) for u in failed)
    return check('units', title, WARN if frequent else CRIT, '失败的单元：%s。`journalctl -u <单元名> -n 50` 看原因。' % '、'.join(failed), ctx.now.isoformat())


def check_disk(ctx):
    title = '磁盘空间'
    try:
        usage = shutil.disk_usage(ctx.private if ctx.private.exists() else ctx.private.parent)
    except OSError:
        return check('disk', title, SKIP, '无法读取')
    free_pct = usage.free / usage.total * 100
    text = '剩余 %.1f%%（%.1f GB）' % (free_pct, usage.free / 1e9)
    if free_pct < DISK_CRIT_PCT or usage.free < 500e6:
        return check('disk', title, CRIT, text + '：写文件很快会失败（账本、备份、日志）。', ctx.now.isoformat())
    if free_pct < DISK_WARN_PCT:
        return check('disk', title, WARN, text, ctx.now.isoformat())
    return check('disk', title, OK, text, ctx.now.isoformat())


def check_cert(ctx):
    title = 'HTTPS 证书'
    path = os.environ.get('TLS_CERT_PATH')
    if not path:
        return check('cert', title, SKIP, '未配置 TLS_CERT_PATH（纯 HTTP）')
    try:
        code, out = ctx.run(['openssl', 'x509', '-noout', '-enddate', '-in', path])
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return check('cert', title, SKIP, '本机没有 openssl，无法读取证书有效期')
    if code != 0 or 'notAfter=' not in out:
        return check('cert', title, WARN, '读不出证书有效期：%s' % out.strip()[:120])
    try:
        end = datetime.strptime(out.split('notAfter=')[1].strip(), '%b %d %H:%M:%S %Y %Z')
    except ValueError:
        return check('cert', title, WARN, '证书有效期格式无法解析：%s' % out.strip()[:120])
    days = (end - ctx.now.replace(tzinfo=None)).days
    text = '还有 %d 天到期（%s）' % (days, end.date())
    if days < CERT_CRIT_DAYS:
        return check('cert', title, CRIT, text + '：过期后后台页面会打不开。', ctx.now.isoformat())
    if days < CERT_WARN_DAYS:
        return check('cert', title, WARN, text, ctx.now.isoformat())
    return check('cert', title, OK, text, ctx.now.isoformat())


def check_webhook(ctx):
    title = '企业微信 webhook'
    if not _webhook():
        return check('webhook', title, WARN, '没有配置 webhook：**所有告警都发不出去**，只能在后台页面看到。', ctx.now.isoformat())
    return check('webhook', title, OK, '已配置', ctx.now.isoformat())


def check_llm(ctx):
    title = 'AI 大模型配置'
    import claude_client
    import llm_settings
    llm_settings.apply()
    problems, configured = [], []
    for label, env in (('盘后报告', 'POSTCLOSE_MODEL'), ('盘中情景', 'SENTINEL_MODEL'), ('次日观察', 'NEXTDAY_MODEL')):
        selected = os.environ.get(env) or None
        ok, why = claude_client.available(selected)
        if not ok:
            problems.append('%s：%s' % (label, why))
        else:
            configured.append('%s=%s' % (label, claude_client.provider(selected)))
    if problems:
        return check('llm', title, WARN, '部分 AI 场景不可用；' + '；'.join(problems), ctx.now.isoformat())
    return check('llm', title, OK, '后端已配置：' + '；'.join(configured), ctx.now.isoformat())


CHECKS = [check_calendar, check_engine, check_quotes, check_evaluators, check_sentinel_queue, check_sentinel_ai,
          check_daily_pipeline, check_premarket_plans, check_postclose, check_review, check_reconcile,
          check_alert_audit, check_backup, check_units, check_disk, check_cert, check_webhook, check_llm]


def run_checks(ctx):
    results = []
    for fn in CHECKS:
        try:
            results.append(fn(ctx))
        except Exception as exc:          # 检查本身出 bug：也要让你知道，而不是这一项悄悄消失
            results.append(check(fn.__name__[6:], fn.__name__[6:], WARN, '检查自身出错：%s: %s' % (type(exc).__name__, str(exc)[:120])))
    for r in results:
        r['schedule'] = SCHEDULES.get(r['key'], '')
    return results


# --- 告警状态机 ---------------------------------------------------------------

def _quiet(now):
    t = now.time()
    return t >= QUIET_START or t < QUIET_END


def evaluate(results, state, now):
    """返回 (新状态, 要推送的行列表, 未送达的原因或 None)。纯函数：不发送、不写盘。

    规则见模块说明。state['alerts'][key] = {level, message, first_seen, seen_runs, last_sent}。"""
    alerts = {k: dict(v) for k, v in (state.get('alerts') or {}).items()}
    lines, sent_keys = [], []
    active = set()
    for r in results:
        if r['level'] not in (WARN, CRIT):
            continue
        key = r['key']
        active.add(key)
        prev = alerts.get(key)
        escalated = bool(prev) and prev['level'] == WARN and r['level'] == CRIT
        a = alerts.setdefault(key, {'first_seen': now.isoformat(), 'seen_runs': 0, 'last_sent': None})
        a.update(level=r['level'], message=r['message'], title=r['title'], seen_runs=a['seen_runs'] + 1)
        last = datetime.fromisoformat(a['last_sent']) if a.get('last_sent') else None
        since_h = None if last is None else (now - last).total_seconds() / 3600
        if r['level'] == CRIT:
            due = last is None or escalated or (since_h >= CRIT_REMIND_H and not _quiet(now))
            tag = '严重'
        else:
            due = a['seen_runs'] >= WARN_CONFIRM_RUNS and (last is None or since_h >= WARN_REMIND_H) and not _quiet(now)
            tag = '注意'
        if due:
            lines.append('[%s] %s：%s' % (tag, r['title'], r['message']))
            sent_keys.append(key)
    # 只有"这次检查明确通过（ok）"才算恢复。skip 是"这会儿没法检查"（比如引擎的报警窗口已结束、
    # 非交易日），不是"好了"——把它当恢复，会在引擎还挂着的时候报"已恢复"。
    ok_keys = {r['key'] for r in results if r['level'] == OK}
    for key in list(alerts):
        if key in ok_keys or key not in {r['key'] for r in results}:
            gone = alerts.pop(key)
            if gone.get('last_sent'):                         # 之前推过，才需要告诉你恢复了
                lines.append('[已恢复] %s' % gone.get('title', key))
                sent_keys.append('recovered:' + key)
    new_state = dict(state)
    new_state['alerts'] = alerts
    new_state['_pending_sent'] = sent_keys
    return new_state, lines, None


def mark_sent(state, now):
    """消息真的发出去之后才记 last_sent；发不出去就保持原样，下一轮重试。"""
    for key in state.pop('_pending_sent', []):
        if key in state['alerts']:
            state['alerts'][key]['last_sent'] = now.isoformat()
    return state


def state_path(private=None):
    return Path(private or portfolio_book.default_dir()) / STATE_NAME


def load_state(private=None):
    try:
        return json.loads(state_path(private).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_state(state, private=None):
    path = state_path(private)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=str(path.parent), suffix='.tmp')
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as f:
            json.dump({k: v for k, v in state.items() if not k.startswith('_')}, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --- 送达 ---------------------------------------------------------------------

def _config_path():
    return os.environ.get('CONFIG_PATH') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'webapp_config.json')


def _webhook():
    try:
        from wecom_push import load_config
        return load_config(_config_path()).get('webhook_url')
    except Exception:
        return None


def send(text):
    """返回 (是否送达, 原因)。"""
    url = _webhook()
    if not url:
        return False, '没有配置企业微信 webhook'
    try:
        from wecom_push import send_wecom_message
        send_wecom_message(url, text)
        return True, ''
    except Exception as exc:
        return False, '%s: %s' % (type(exc).__name__, str(exc)[:120])


def run_once(ctx=None, send_fn=None, private=None, deliver=True, persist=True):
    """一次完整检查：跑检查 → 状态机 → 推送 → 落盘。返回 (检查结果, 推送行, 送达情况)。"""
    ctx = ctx or Ctx()
    send_fn = send_fn or send
    results = run_checks(ctx)
    state = load_state(private)
    state, lines, _ = evaluate(results, state, ctx.now)
    delivered = None
    if lines and deliver:
        text = '[Alpha Shadow 系统健康] %s\n%s' % (ctx.now.strftime('%m-%d %H:%M'), '\n'.join(lines))
        ok, why = send_fn(text)
        delivered = {'ok': ok, 'reason': why, 'at': ctx.now.isoformat(), 'lines': len(lines)}
        if ok:
            state = mark_sent(state, ctx.now)
        else:
            state.pop('_pending_sent', None)
    else:
        state.pop('_pending_sent', None)
    state.update(last_run_at=ctx.now.isoformat(), checks=results)
    if delivered:
        state['last_delivery'] = delivered
    if persist:
        save_state(state, private)
    return results, lines, delivered


# --- 页面 / 外部心跳用 -----------------------------------------------------------

def summary(private=None, now=None):
    """返回 {'heartbeat_age_min', 'stale', 'critical', 'checks', 'last_delivery'}。"""
    now = now or datetime.now(CST)
    state = load_state(private)
    last = state.get('last_run_at')
    age = None if not last else (now - datetime.fromisoformat(last)).total_seconds() / 60
    checks = state.get('checks') or []
    return {'heartbeat_age_min': age, 'stale': age is None or age > HEARTBEAT_STALE_MIN,
            'critical': any(c['level'] == CRIT for c in checks), 'checks': checks,
            'last_delivery': state.get('last_delivery'), 'alerts': state.get('alerts') or {}}


def deep_status(private=None, now=None):
    """给不需要登录的 /health/deep：(HTTP 状态码, 文本)。不含任何细节。"""
    s = summary(private, now)
    if s['stale']:
        return 503, 'stale'
    if s['critical']:
        return 503, 'critical'
    return 200, 'ok'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('run', help='检查一遍，必要时推送（定时器调用）')
    p.add_argument('--no-send', action='store_true', help='只检查并打印：不推送、不写状态（不影响去抖计数和心跳）')
    sub.add_parser('status', help='打印最近一次检查结果')
    a = ap.parse_args(argv)
    if a.cmd == 'status':
        s = summary()
        print('心跳：%s' % ('从未运行' if s['heartbeat_age_min'] is None else '%.1f 分钟前' % s['heartbeat_age_min']))
        for c in s['checks']:
            print('  [%-4s] %s：%s' % (c['level'], c['title'], c['message']))
        return 0
    import llm_settings
    llm_settings.apply()
    results, lines, delivered = run_once(deliver=not a.no_send, persist=not a.no_send)
    for c in results:
        print('[%-4s] %s：%s' % (c['level'], c['title'], c['message']))
    if lines:
        print('\n推送内容：\n' + '\n'.join(lines))
        print('送达：%s' % ('（--no-send）' if a.no_send else (delivered or {}).get('ok')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
