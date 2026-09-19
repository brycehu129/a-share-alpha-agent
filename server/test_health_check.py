import base64
import http.client
import io
import json
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import backup
import health_check as hc
import webapp
import wecom_push

CST = hc.CST
PASSWORD = 'pw-for-tests'
AUTH = 'Basic ' + base64.b64encode(('x:' + PASSWORD).encode()).decode()


def at(text):
    return datetime.fromisoformat('2026-09-21T%s+08:00' % text)          # 2026-09-21 是周一


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.private = self.tmp / 'private'
        (self.private / 'intraday').mkdir(parents=True)
        self.history = self.tmp / 'history'
        self.history.mkdir()
        env = patch.dict(os.environ, {'PRIVATE_DATA_DIR': str(self.private), 'BACKUP_DIR': str(self.tmp / 'backups'),
                                      'CONFIG_PATH': str(self.tmp / 'config.json'), 'ADMIN_PASSWORD': PASSWORD,
                                      'HISTORY_DIR': str(self.history)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('TLS_CERT_PATH', None)
        os.environ.pop('INTRADAY_DIR', None)
        sym = patch.object(hc, '_monitored_symbols', return_value=['sh600000'])
        sym.start()
        self.addCleanup(sym.stop)

    def ctx(self, when='10:00:00', state='open', run=None):
        return hc.Ctx(now=at(when), history=self.history, private=self.private, run=run, calendar=lambda now: state)

    def ticks(self, rows, day='2026-09-21'):
        (self.private / 'intraday' / ('ticks-%s.jsonl' % day)).write_text('\n'.join(json.dumps(r) for r in rows))

    def tick(self, when, **kw):
        return {'at': at(when).isoformat(), 'ran': True, 'fresh': 3, 'failures': [], 'evaluator_errors': [], **kw}


class EngineTests(Base):
    def test_skips_when_it_should_not_be_running(self):
        for kw in ({'state': 'closed'}, {'when': '12:00:00'}, {'when': '09:30:00'}, {'when': '15:30:00'}, {'when': '08:00:00'}):
            self.assertEqual(hc.check_engine(self.ctx(**kw))['level'], hc.SKIP, kw)

    def test_skips_when_there_is_nothing_to_monitor(self):
        with patch.object(hc, '_monitored_symbols', return_value=[]):
            self.assertEqual(hc.check_engine(self.ctx())['level'], hc.SKIP)

    def test_no_tick_file_during_the_window_is_critical(self):
        r = hc.check_engine(self.ctx('10:00:00'))
        self.assertEqual(r['level'], hc.CRIT)
        self.assertIn('一轮都没有', r['message'])

    def test_stale_engine_is_critical_and_a_fresh_one_is_ok(self):
        self.ticks([self.tick('09:58:00')])
        r = hc.check_engine(self.ctx('10:04:30'))
        self.assertEqual(r['level'], hc.CRIT)
        self.assertIn('6 分钟', r['message'])
        self.assertEqual(hc.check_engine(self.ctx('10:00:20'))['level'], hc.OK)

    def test_yesterdays_ticks_do_not_count_as_todays(self):
        self.ticks([self.tick('09:58:00')], day='2026-09-18')
        self.assertEqual(hc.check_engine(self.ctx('10:00:00'))['level'], hc.CRIT)

    def test_quote_source_failure_is_a_warning(self):
        self.ticks([self.tick('09:5%d:00' % i, failures=['sh600000'], fresh=0) for i in range(6)])
        self.assertEqual(hc.check_quotes(self.ctx('09:59:30'))['level'], hc.WARN)
        self.ticks([self.tick('09:5%d:00' % i) for i in range(6)])
        self.assertEqual(hc.check_quotes(self.ctx('09:59:30'))['level'], hc.OK)
        self.ticks([self.tick('09:50:00', fresh=0)])
        self.assertEqual(hc.check_quotes(self.ctx('09:59:30'))['level'], hc.SKIP)          # 样本太少不下结论

    def test_evaluator_errors_are_surfaced(self):
        self.ticks([self.tick('09:58:00', evaluator_errors=['Sentinel.evaluate: KeyError: x'])])
        r = hc.check_evaluators(self.ctx('10:00:00'))
        self.assertEqual(r['level'], hc.WARN)
        self.assertIn('KeyError', r['message'])


class SentinelTests(Base):
    def pending(self, created):
        d = self.private / 'sentinel' / 'pending'
        d.mkdir(parents=True, exist_ok=True)
        (d / 'p1.json').write_text(json.dumps({'created_at': created.isoformat()}))

    def done(self, name, status, meta_status=None, finished='10:00:00'):
        d = self.private / 'sentinel' / 'done'
        d.mkdir(parents=True, exist_ok=True)
        (d / (name + '.json')).write_text(json.dumps({'status': status, 'finished_at': at(finished).isoformat(),
                                                      'meta': {'status': meta_status}}))

    def test_backlog_is_flagged_only_when_old(self):
        self.pending(at('09:50:00'))
        self.assertEqual(hc.check_sentinel_queue(self.ctx('10:05:00'))['level'], hc.WARN)
        self.assertEqual(hc.check_sentinel_queue(self.ctx('09:55:00'))['level'], hc.OK)

    def test_repeated_fatal_ai_errors_are_critical_but_transient_ones_only_warn(self):
        for i, s in enumerate(('authentication_error',) * 3):
            self.done('a%d' % i, 'ai_failed', s)
        r = hc.check_sentinel_ai(self.ctx('11:00:00'))
        self.assertEqual(r['level'], hc.CRIT)
        self.assertIn('重试没有用', r['message'])
        for f in (self.private / 'sentinel' / 'done').glob('*.json'):
            f.unlink()
        for i in range(3):
            self.done('b%d' % i, 'ai_failed', 'connection_error')
        self.assertEqual(hc.check_sentinel_ai(self.ctx('11:00:00'))['level'], hc.WARN)

    def test_a_single_failure_or_recovery_is_not_an_alarm(self):
        self.done('a1', 'ai_failed', 'rate_limited')
        self.done('a2', 'done', finished='10:01:00')
        self.assertEqual(hc.check_sentinel_ai(self.ctx('11:00:00'))['level'], hc.OK)

    def test_no_activity_today_is_a_skip(self):
        self.assertEqual(hc.check_sentinel_ai(self.ctx())['level'], hc.SKIP)


class PipelineTests(Base):
    def test_daily_pipeline_must_have_produced_todays_report(self):
        (self.history / 'agent').mkdir()
        self.assertEqual(hc.check_daily_pipeline(self.ctx('16:00:00'))['level'], hc.SKIP)
        self.assertEqual(hc.check_daily_pipeline(self.ctx('17:30:00', state='closed'))['level'], hc.SKIP)
        self.assertEqual(hc.check_daily_pipeline(self.ctx('17:30:00'))['level'], hc.CRIT)
        (self.history / 'agent' / '20260921004000-1.json').write_text('{}')                       # 08:40 北京时间的盘前那轮
        self.assertEqual(hc.check_daily_pipeline(self.ctx('17:30:00'))['level'], hc.CRIT)         # 不能冒充收盘流程
        (self.history / 'agent' / '20260921073500-1.json').write_text('{}')                       # 15:35 北京时间 = 07:35 UTC
        self.assertEqual(hc.check_daily_pipeline(self.ctx('17:30:00'))['level'], hc.OK)
        (self.history / 'agent' / '20260921073500-1.json').unlink()
        (self.history / 'agent' / '20260921004000-1.json').unlink()
        (self.history / 'agent' / '20260918153500-1.json').write_text('{}')
        self.assertEqual(hc.check_daily_pipeline(self.ctx('17:30:00'))['level'], hc.CRIT)      # 昨天的不算

    def freeze_plan(self, name, created):
        from tushare_sync import save
        save(self.history / 'predictions' / name, {'id': name, 'created_at': created})

    def test_todays_plans_must_be_frozen_before_the_executors_deadline(self):
        self.assertEqual(hc.check_premarket_plans(self.ctx('09:20:00'))['level'], hc.SKIP)          # 还没到检查时间
        self.assertEqual(hc.check_premarket_plans(self.ctx('10:00:00', state='closed'))['level'], hc.SKIP)
        r = hc.check_premarket_plans(self.ctx('09:30:00'))
        self.assertEqual(r['level'], hc.CRIT)
        self.assertIn('盘前选股没有跑完', r['message'])
        self.freeze_plan('select-0.5-2026-09-17-sh600000.json', '2026-09-18T15:40:00+08:00')     # 昨天冻结的不算
        self.assertEqual(hc.check_premarket_plans(self.ctx('09:30:00'))['level'], hc.CRIT)
        self.freeze_plan('select-0.5-2026-09-18-sh600001.json', '2026-09-21T08:45:00+08:00')
        self.assertEqual(hc.check_premarket_plans(self.ctx('09:30:00'))['level'], hc.OK)
        self.assertEqual(hc.check_premarket_plans(self.ctx('16:30:00'))['level'], hc.SKIP)            # 收盘后不再查

    def postclose(self, generated, ai_status=None):
        d = self.private / 'postclose'
        d.mkdir(exist_ok=True)
        (d / 'r.json').write_text(json.dumps({'generated_at': generated, 'ai_meta': {'status': ai_status, 'error': 'boom'}}))

    def test_postclose_report_and_its_ai_part(self):
        self.assertEqual(hc.check_postclose(self.ctx('17:30:00'))['level'], hc.WARN)
        self.postclose('2026-09-18T16:40:00+08:00', 'ok')
        self.assertEqual(hc.check_postclose(self.ctx('17:30:00'))['level'], hc.WARN)            # 不是今天的
        self.postclose('2026-09-21T16:40:00+08:00', 'unavailable')
        r = hc.check_postclose(self.ctx('17:30:00'))
        self.assertEqual(r['level'], hc.WARN)
        self.assertIn('没有 AI 研判', r['message'])
        self.postclose('2026-09-21T16:40:00+08:00', 'ok')
        self.assertEqual(hc.check_postclose(self.ctx('17:30:00'))['level'], hc.OK)

    def test_reconcile_record_expected_after_the_close(self):
        self.assertEqual(hc.check_reconcile(self.ctx('15:30:00'))['level'], hc.SKIP)
        self.assertEqual(hc.check_reconcile(self.ctx('16:00:00'))['level'], hc.WARN)
        (self.private / 'sentinel').mkdir()
        (self.private / 'sentinel' / 'reconcile-2026-09-21.json').write_text('{}')
        self.assertEqual(hc.check_reconcile(self.ctx('16:00:00'))['level'], hc.OK)

    def test_calendar_unreadable_on_a_weekday_is_flagged_but_not_on_weekends(self):
        self.assertEqual(hc.check_calendar(self.ctx(state='unknown'))['level'], hc.WARN)
        sat = hc.Ctx(now=datetime.fromisoformat('2026-09-19T10:00:00+08:00'), history=self.history, private=self.private,
                     calendar=lambda n: 'unknown')
        self.assertEqual(hc.check_calendar(sat)['level'], hc.OK)


class SystemTests(Base):
    def units(self, text=''):
        return lambda argv, timeout=10: (0, text)

    def test_failed_units(self):
        self.assertEqual(hc.check_units(self.ctx(run=self.units()))['level'], hc.OK)
        frequent = 'alpha-shadow-intraday.service loaded failed failed x\n'
        self.assertEqual(hc.check_units(self.ctx(run=self.units(frequent)))['level'], hc.WARN)         # 偶发的每分钟任务
        daily = frequent + 'alpha-shadow-daily.service loaded failed failed y\n'
        r = hc.check_units(self.ctx(run=self.units(daily)))
        self.assertEqual(r['level'], hc.CRIT)
        self.assertIn('alpha-shadow-daily.service', r['message'])

    def test_missing_systemctl_is_a_skip_not_a_crash(self):
        def boom(argv, timeout=10):
            raise FileNotFoundError
        self.assertEqual(hc.check_units(self.ctx(run=boom))['level'], hc.SKIP)

    def test_disk_thresholds(self):
        usage = lambda free, total=100_000_000_000: type('U', (), {'free': free, 'total': total})()
        for free, level in ((50e9, hc.OK), (10e9, hc.WARN), (3e9, hc.CRIT), (400e6, hc.CRIT)):
            with patch('shutil.disk_usage', return_value=usage(free, 100e9 if free > 1e9 else 100e9)):
                self.assertEqual(hc.check_disk(self.ctx())['level'], level, free)

    def test_certificate_expiry(self):
        def run_with(days):
            end = (at('10:00:00').replace(tzinfo=None) + timedelta(days=days)).strftime('%b %d %H:%M:%S %Y GMT')
            return lambda argv, timeout=10: (0, 'notAfter=%s\n' % end)
        with patch.dict(os.environ, {'TLS_CERT_PATH': '/x/cert.pem'}):
            for days, level in ((200, hc.OK), (30, hc.WARN), (5, hc.CRIT), (-3, hc.CRIT)):
                self.assertEqual(hc.check_cert(self.ctx(run=run_with(days)))['level'], level, days)
            self.assertEqual(hc.check_cert(self.ctx(run=lambda a, timeout=10: (1, 'no such file')))['level'], hc.WARN)
            self.assertEqual(hc.check_cert(self.ctx(run=lambda a, timeout=10: (0, 'notAfter=garbage')))['level'], hc.WARN)

            def nossl(argv, timeout=10):
                raise FileNotFoundError
            self.assertEqual(hc.check_cert(self.ctx(run=nossl))['level'], hc.SKIP)
        self.assertEqual(hc.check_cert(self.ctx())['level'], hc.SKIP)                                  # 没配 TLS

    def test_webhook_and_backup_and_llm(self):
        self.assertEqual(hc.check_webhook(self.ctx())['level'], hc.WARN)
        wecom_push.save_config(os.environ['CONFIG_PATH'], wecom_push.WEBHOOK_PREFIX + '?key=abc')
        self.assertEqual(hc.check_webhook(self.ctx())['level'], hc.OK)
        with patch.object(backup, 'health', return_value=('warn', '已 40 小时没有新的成功快照')):
            self.assertEqual(hc.check_backup(self.ctx())['level'], hc.WARN)
        with patch.object(backup, 'health', return_value=('none', '尚未运行')):
            self.assertEqual(hc.check_backup(self.ctx())['level'], hc.SKIP)
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': '', 'ANTHROPIC_API_KEY': '', 'LLM_PROVIDER': ''}):
            self.assertEqual(hc.check_llm(self.ctx())['level'], hc.WARN)

    def test_a_broken_check_is_reported_instead_of_vanishing(self):
        def bad(ctx):
            raise ValueError('bug')
        bad.__name__ = 'check_bad'
        with patch.object(hc, 'CHECKS', [bad, hc.check_calendar]):
            results = hc.run_checks(self.ctx())
        self.assertEqual(results[0]['level'], hc.WARN)
        self.assertIn('检查自身出错', results[0]['message'])
        self.assertEqual(len(results), 2)


def r(key, level, msg='m', title=None):
    return {'key': key, 'title': title or key, 'level': level, 'message': msg}


class StateMachineTests(unittest.TestCase):
    def step(self, state, results, when):
        new, lines, _ = hc.evaluate(results, state, at(when))
        return new, lines

    def sent(self, state, when):
        return hc.mark_sent(state, at(when))

    def test_critical_is_sent_immediately_and_only_once_per_two_hours(self):
        s, lines = self.step({}, [r('engine', hc.CRIT, '停了')], '10:00:00')
        self.assertEqual(len(lines), 1)
        self.assertIn('[严重]', lines[0])
        s = self.sent(s, '10:00:00')
        s, lines = self.step(s, [r('engine', hc.CRIT, '停了')], '10:05:00')
        self.assertEqual(lines, [])
        s, lines = self.step(s, [r('engine', hc.CRIT, '停了')], '11:59:00')
        self.assertEqual(lines, [])
        s, lines = self.step(s, [r('engine', hc.CRIT, '停了')], '12:01:00')
        self.assertEqual(len(lines), 1)

    def test_warning_needs_two_consecutive_runs(self):
        s, lines = self.step({}, [r('quotes', hc.WARN)], '10:00:00')
        self.assertEqual(lines, [])
        s, lines = self.step(s, [r('quotes', hc.WARN)], '10:05:00')
        self.assertEqual(len(lines), 1)
        self.assertIn('[注意]', lines[0])

    def test_a_blip_that_clears_before_confirmation_is_silent(self):
        s, _ = self.step({}, [r('quotes', hc.WARN)], '10:00:00')
        s, lines = self.step(s, [r('quotes', hc.OK)], '10:05:00')
        self.assertEqual(lines, [])
        self.assertEqual(s['alerts'], {})

    def test_warning_reminders_are_twelve_hours_apart(self):
        s, _ = self.step({}, [r('disk', hc.WARN)], '10:00:00')
        s, lines = self.step(s, [r('disk', hc.WARN)], '10:05:00')
        s = self.sent(s, '10:05:00')
        s, lines = self.step(s, [r('disk', hc.WARN)], '18:00:00')
        self.assertEqual(lines, [])
        s, lines = self.step(s, [r('disk', hc.WARN)], '22:00:00')       # 还没过 12 小时
        self.assertEqual(lines, [])

    def test_night_holds_warnings_and_critical_reminders_but_not_a_first_critical(self):
        s, lines = self.step({}, [r('disk', hc.WARN)], '23:00:00')
        s, lines = self.step(s, [r('disk', hc.WARN)], '23:05:00')
        self.assertEqual(lines, [])                                       # 夜里不发 warn
        s, lines = self.step(s, [r('disk', hc.WARN)], '07:05:00')
        self.assertEqual(len(lines), 1)                                   # 早上补发
        s, lines = self.step({}, [r('daily', hc.CRIT, '没产出')], '23:10:00')
        self.assertEqual(len(lines), 1)                                   # 首次 crit 夜里也发
        s = self.sent(s, '23:10:00')
        s, lines = self.step(s, [r('daily', hc.CRIT, '没产出')], '02:30:00')
        self.assertEqual(lines, [])                                       # 夜里不重复提醒

    def test_escalation_from_warning_to_critical_is_sent_at_once(self):
        s, _ = self.step({}, [r('cert', hc.WARN)], '10:00:00')
        s, _ = self.step(s, [r('cert', hc.WARN)], '10:05:00')
        s = self.sent(s, '10:05:00')
        s, lines = self.step(s, [r('cert', hc.CRIT)], '10:10:00')
        self.assertEqual(len(lines), 1)
        self.assertIn('[严重]', lines[0])

    def test_recovery_is_announced_only_if_the_problem_was(self):
        s, _ = self.step({}, [r('engine', hc.CRIT)], '10:00:00')
        s = self.sent(s, '10:00:00')
        s, lines = self.step(s, [r('engine', hc.OK)], '10:10:00')
        self.assertEqual(len(lines), 1)
        self.assertIn('已恢复', lines[0])
        s, lines = self.step(s, [r('engine', hc.OK)], '10:15:00')
        self.assertEqual(lines, [])

    def test_skip_is_not_recovery(self):
        """引擎在 14:57 挂了并报了警；15:05 窗口结束、检查变成 skip——这不是"恢复"。"""
        s, _ = self.step({}, [r('engine', hc.CRIT)], '14:57:00')
        s = self.sent(s, '14:57:00')
        s, lines = self.step(s, [r('engine', hc.SKIP)], '15:05:00')
        self.assertEqual(lines, [])
        self.assertIn('engine', s['alerts'])
        s, lines = self.step(s, [r('engine', hc.OK)], '09:40:00')
        self.assertEqual(len(lines), 1)
        self.assertIn('已恢复', lines[0])

    def test_unsent_alerts_stay_due_so_delivery_is_retried(self):
        s, lines = self.step({}, [r('engine', hc.CRIT)], '10:00:00')
        self.assertEqual(len(lines), 1)                                   # 假设这次没发出去：不调用 mark_sent
        s.pop('_pending_sent')
        s, lines = self.step(s, [r('engine', hc.CRIT)], '10:05:00')
        self.assertEqual(len(lines), 1)                                   # 下一轮仍然到期

    def test_several_problems_are_merged_into_one_message(self):
        _, lines = self.step({}, [r('engine', hc.CRIT), r('daily', hc.CRIT), r('disk', hc.OK)], '17:30:00')
        self.assertEqual(len(lines), 2)


class RunOnceTests(Base):
    def setUp(self):
        super().setUp()
        self.messages = []

    def sender(self, ok=True):
        def send(text):
            self.messages.append(text)
            return (ok, '' if ok else '没有配置企业微信 webhook')
        return send

    def failing_ctx(self, when='10:00:00'):
        return self.ctx(when)                    # 引擎检查会 crit（没有 tick 文件）

    def test_one_merged_message_is_pushed_and_not_repeated(self):
        c = self.failing_ctx()
        hc.run_once(c, self.sender(), private=self.private)
        self.assertEqual(len(self.messages), 1)
        self.assertIn('盘中引擎', self.messages[0])
        hc.run_once(self.failing_ctx('10:05:00'), self.sender(), private=self.private)
        # 第二轮可能推出别的、刚满两次确认的 warn（如 webhook/AI 没配），但盘中引擎那条不能重复
        self.assertTrue(all('盘中引擎' not in m for m in self.messages[1:]))

    def test_undelivered_alerts_are_retried_and_the_failure_is_recorded(self):
        hc.run_once(self.failing_ctx(), self.sender(ok=False), private=self.private)
        state = hc.load_state(self.private)
        self.assertFalse(state['last_delivery']['ok'])
        self.assertIsNone(state['alerts']['engine']['last_sent'])
        hc.run_once(self.failing_ctx('10:05:00'), self.sender(ok=True), private=self.private)
        self.assertEqual(len(self.messages), 2)                            # 重试成功
        self.assertIsNotNone(hc.load_state(self.private)['alerts']['engine']['last_sent'])

    def test_state_file_is_private_and_carries_the_heartbeat(self):
        hc.run_once(self.ctx('12:00:00'), self.sender(), private=self.private)
        path = self.private / hc.STATE_NAME
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(hc.load_state(self.private)['last_run_at'], at('12:00:00').isoformat())
        self.assertTrue(hc.load_state(self.private)['checks'])

    def test_no_send_mode_neither_sends_nor_touches_state(self):
        hc.run_once(self.failing_ctx(), self.sender(), private=self.private, deliver=False, persist=False)
        self.assertEqual(self.messages, [])
        self.assertFalse((self.private / hc.STATE_NAME).exists())

    def test_a_crashing_sender_cannot_take_the_checker_down(self):
        def boom(text):
            raise RuntimeError('network down')
        with self.assertRaises(RuntimeError):
            hc.run_once(self.failing_ctx(), boom, private=self.private)
        # send() 本身把异常都转成 (False, 原因)；直接注入的坏函数才会传上来——真实路径见下一条
        with patch('wecom_push.send_wecom_message', side_effect=RuntimeError('boom')):
            wecom_push.save_config(os.environ['CONFIG_PATH'], wecom_push.WEBHOOK_PREFIX + '?key=abc')
            ok, why = hc.send('x')
        self.assertFalse(ok)
        self.assertIn('boom', why)

    def test_corrupt_state_file_starts_fresh_instead_of_crashing(self):
        (self.private / hc.STATE_NAME).write_text('{broken')
        hc.run_once(self.ctx('12:00:00'), self.sender(), private=self.private)
        self.assertIn('checks', hc.load_state(self.private))


class SummaryTests(Base):
    def state(self, when, checks):
        hc.save_state({'last_run_at': at(when).isoformat(), 'checks': checks}, self.private)

    def test_deep_status(self):
        self.assertEqual(hc.deep_status(self.private, at('10:00:00')), (503, 'stale'))              # 从未运行
        self.state('10:00:00', [r('a', hc.OK)])
        self.assertEqual(hc.deep_status(self.private, at('10:10:00')), (200, 'ok'))
        self.assertEqual(hc.deep_status(self.private, at('10:25:00')), (503, 'stale'))              # 心跳停了
        self.state('10:00:00', [r('a', hc.CRIT)])
        self.assertEqual(hc.deep_status(self.private, at('10:01:00')), (503, 'critical'))
        self.state('10:00:00', [r('a', hc.WARN)])
        self.assertEqual(hc.deep_status(self.private, at('10:01:00')), (200, 'ok'))                 # warn 不算"关键"


class WebTests(Base):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), webapp.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        import llm_settings
        saved = dict(llm_settings._ORIGINALS)
        llm_settings._ORIGINALS.clear()
        self.addCleanup(lambda: (llm_settings._ORIGINALS.clear(), llm_settings._ORIGINALS.update(saved)))

    def get(self, path, auth=True):
        h = {'Host': '127.0.0.1:%d' % self.port}
        if auth:
            h['Authorization'] = AUTH
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        conn.request('GET', path, headers=h)
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        return resp.status, body

    def test_deep_health_needs_no_login_and_leaks_nothing(self):
        status, body = self.get('/health/deep', auth=False)
        self.assertEqual((status, body), (503, 'stale'))
        hc.save_state({'last_run_at': datetime.now(CST).isoformat(),
                       'checks': [r('engine', hc.CRIT, '盘中引擎已 7 分钟没有成功运行 sh600000 成本价 9.5')]}, self.private)
        status, body = self.get('/health/deep', auth=False)
        self.assertEqual((status, body), (503, 'critical'))
        hc.save_state({'last_run_at': datetime.now(CST).isoformat(), 'checks': [r('engine', hc.OK)]}, self.private)
        self.assertEqual(self.get('/health/deep', auth=False), (200, 'ok'))
        self.assertEqual(self.get('/health', auth=False), (200, 'ok'))

    def test_config_page_shows_the_health_panel_and_flags_undelivered_alerts(self):
        status, page = self.get('/')
        self.assertIn('系统健康', page)
        self.assertIn('尚未运行', page)
        hc.save_state({'last_run_at': datetime.now(CST).isoformat(),
                       'checks': [r('engine', hc.CRIT, '停了<script>x</script>', title='盘中引擎'), r('disk', hc.OK, '剩余 50%', title='磁盘空间')],
                       'last_delivery': {'ok': False, 'reason': '没有配置企业微信 webhook', 'at': datetime.now(CST).isoformat(), 'lines': 1}},
                      self.private)
        page = self.get('/')[1]
        self.assertIn('有严重问题', page)
        self.assertIn('盘中引擎', page)
        self.assertIn('未送达', page)
        self.assertNotIn('<script>x</script>', page)                       # 检查消息按文本转义
        hc.save_state({'last_run_at': (datetime.now(CST) - timedelta(minutes=40)).isoformat(), 'checks': []}, self.private)
        self.assertIn('没人在盯着系统了', self.get('/')[1])


class DeploymentFilesTests(unittest.TestCase):
    ROOT = Path(hc.__file__).parent

    def test_checker_does_not_depend_on_tests_or_locks(self):
        script = (self.ROOT / 'cron_health.sh').read_text()
        self.assertNotIn('unittest', script)
        self.assertNotIn('flock', script)
        self.assertIn('health_check.py run', script)

    def test_timer_lists_minutes_explicitly_in_shanghai_time(self):
        timer = (self.ROOT / 'systemd' / 'alpha-shadow-health.timer').read_text()
        self.assertIn('Asia/Shanghai', timer)
        self.assertIn(':00,05,10,15,20,25,30,35,40,45,50,55:00', timer)
        self.assertNotIn('/5', timer.split('OnCalendar=')[1].split('\n')[0])
        self.assertIn('cron_health.sh', (self.ROOT / 'systemd' / 'alpha-shadow-health.service').read_text())

    def test_external_heartbeat_workflow_hits_the_deep_endpoint_and_uses_no_secrets(self):
        wf = (self.ROOT.parent / '.github' / 'workflows' / 'health-ping.yml').read_text()
        self.assertIn('/health/deep', wf)
        self.assertNotIn('secrets.', wf)
        self.assertIn('schedule:', wf)


class CliTests(Base):
    def test_status_and_run_no_send(self):
        out = io.StringIO()
        with patch('sys.stdout', out), patch.object(hc, 'run_checks', return_value=[r('engine', hc.CRIT, '停了', '盘中引擎')]):
            self.assertEqual(hc.main(['run', '--no-send']), 0)
        self.assertIn('盘中引擎', out.getvalue())
        self.assertFalse((self.private / hc.STATE_NAME).exists())
        out = io.StringIO()
        with patch('sys.stdout', out):
            hc.main(['status'])
        self.assertIn('从未运行', out.getvalue())


if __name__ == '__main__':
    unittest.main()
