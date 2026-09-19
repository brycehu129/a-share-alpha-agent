import base64
import copy
import http.client
import json
import math
import os
import stat
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import alpha_engine
import exec_spec
import proposals as pr
import webapp
import webapp_views

EV = {'n': 40, 'cohorts': 20, 'execution_version': 'exec-0.2', 'summary': '突破止损被扫后又涨回的比例偏高'}
PASSWORD = 'pw-for-tests'
AUTH = 'Basic ' + base64.b64encode(('x:' + PASSWORD).encode()).decode()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def submit(self, param='exit.stop_pct', new=0.02, track='breakout', evidence=EV, **kw):
        return pr.submit(track, param, new, kw.pop('rationale', '理由'), evidence, directory=self.dir, **kw)

    def store(self):
        return pr.load(self.dir)


class SubmitTests(Base):
    def test_a_valid_proposal_is_only_queued_and_changes_nothing(self):
        p = self.submit()
        self.assertEqual((p['status'], p['old'], p['new']), ('pending', 0.025, 0.02))
        self.assertEqual(pr.active(self.dir), (0, {}))

    def test_every_red_line_is_refused_and_recorded(self):
        for name in sorted(exec_spec.RED_LINES):
            p = self.submit(param=name, new=1)
            self.assertEqual(p['status'], 'refused', name)
            self.assertIn('风控红线', p['reason'])
        self.assertEqual(pr.active(self.dir), (0, {}))
        self.assertEqual(len(self.store()['proposals']), len(exec_spec.RED_LINES))     # 被拒的尝试也留档

    def test_anything_outside_the_whitelist_is_refused_even_if_not_a_named_red_line(self):
        for name in ('exit.stop_floor', 'entry.kind', 'entry.window', 'exit.day1_close_rule', 'anything.else', '', 'exit'):
            self.assertEqual(self.submit(param=name, new=0.01)['status'], 'refused', name)

    def test_bounds_step_noop_and_unknown_track(self):
        self.assertIn('绝对边界', self.submit(new=0.5)['reason'])
        self.assertIn('步长', self.submit(new=0.035)['reason'])          # 0.025 → 0.035 超过 0.005
        self.assertIn('相同', self.submit(new=0.025)['reason'])
        self.assertIn('未知 track', self.submit(track='mystery')['reason'])

    def test_non_finite_and_non_numeric_values_are_refused(self):
        for bad in (float('nan'), math.inf, -math.inf, True, '0.02', None, [0.02]):
            p = self.submit(new=bad)
            self.assertEqual(p['status'], 'refused', repr(bad))
            self.assertIsNone(p['new'])

    def test_integer_parameter_must_be_an_integer(self):
        self.assertEqual(self.submit(param='exit.hold_sessions', new=2.5)['status'], 'refused')
        self.assertEqual(self.submit(param='exit.hold_sessions', new=2)['status'], 'pending')

    def test_parameter_not_defined_for_the_track_is_refused(self):
        p = self.submit(param='entry.min_pct', new=0.0025, track='pullback')      # 回调 track 没有确认下沿
        self.assertEqual(p['status'], 'refused')
        self.assertIn('没有定义', p['reason'])

    def test_result_must_remain_self_consistent(self):
        store = {'seq': 0, 'proposals': [], 'revisions': [
            {'track': 'breakout', 'parameter': 'exit.target_pct', 'new': 0.03}]}
        problem, _ = pr.check_change(store, 'breakout', 'exit.stop_pct', 0.03)      # 止盈 3% 不能不大于止损 3%
        self.assertIn('不自洽', problem)

    def test_a_newer_pending_proposal_for_the_same_knob_supersedes_the_older(self):
        a = self.submit(new=0.02)
        b = self.submit(new=0.0225)
        by_id = {p['id']: p for p in self.store()['proposals']}
        self.assertEqual((by_id[a['id']]['status'], by_id[b['id']]['status']), ('stale', 'pending'))
        self.assertIn(b['id'], by_id[a['id']]['reason'])

    def test_free_text_is_normalised_and_bounded(self):
        p = self.submit(rationale='第一行\n\n第二行\t' + 'x' * 900, source='ai\nfake')
        self.assertNotIn('\n', p['rationale'])
        self.assertLessEqual(len(p['rationale']), pr.RATIONALE_MAX)
        self.assertNotIn('\n', p['source'])


class EvidenceGateTests(Base):
    def test_insufficient_samples_are_marked_and_cannot_be_approved(self):
        for ev in ({**EV, 'n': 29}, {**EV, 'cohorts': 14}, {**EV, 'n': None}, {}, None, 'lots of evidence',
                   {**EV, 'n': True}):
            p = self.submit(evidence=ev)
            self.assertEqual(p['status'], 'insufficient', repr(ev))
            with self.assertRaises(pr.ProposalError):
                pr.approve(p['id'], directory=self.dir)
        self.assertEqual(pr.active(self.dir), (0, {}))

    def test_gate_boundary_is_inclusive(self):
        self.assertEqual(self.submit(evidence={**EV, 'n': 30, 'cohorts': 15})['status'], 'pending')

    def test_evidence_must_come_from_the_current_execution_version(self):
        p = self.submit(evidence={**EV, 'execution_version': 'exec-0.1'})
        self.assertEqual(p['status'], 'insufficient')
        self.assertIn('不能为新版本背书', p['reason'])

    def test_after_a_revision_old_version_evidence_no_longer_counts(self):
        p = self.submit()
        pr.approve(p['id'], directory=self.dir)
        again = self.submit(param='exit.target_pct', new=0.06, evidence=EV)       # 证据还是 exec-0.2 的
        self.assertEqual(again['status'], 'insufficient')
        ok = self.submit(param='exit.target_pct', new=0.06, evidence={**EV, 'execution_version': 'exec-0.2.r1'})
        self.assertEqual(ok['status'], 'pending')


class ApproveTests(Base):
    def test_approval_appends_a_revision_and_changes_the_effective_spec(self):
        p = self.submit()
        rev = pr.approve(p['id'], '同意', directory=self.dir)
        self.assertEqual((rev['revision'], rev['old'], rev['new']), (1, 0.025, 0.02))
        n, overrides = pr.active(self.dir)
        self.assertEqual((n, overrides), (1, {'breakout': {'exit.stop_pct': 0.02}}))
        self.assertEqual(pr.current_spec(self.store(), 'breakout')['exit']['stop_pct'], 0.02)
        self.assertEqual(pr.current_spec(self.store(), 'pullback')['exit']['stop_pct'], 0.03)     # 别的 track 不受影响
        stored = self.store()['proposals'][0]
        self.assertEqual((stored['status'], stored['applied_revision'], stored['decision_note']), ('approved', 1, '同意'))

    def test_a_proposal_can_only_be_decided_once(self):
        p = self.submit()
        pr.approve(p['id'], directory=self.dir)
        for fn in (pr.approve, pr.reject):
            with self.assertRaises(pr.ProposalError):
                fn(p['id'], directory=self.dir)
        self.assertEqual(pr.active(self.dir)[0], 1)

    def test_unknown_id(self):
        with self.assertRaises(pr.ProposalError):
            pr.approve('p9999', directory=self.dir)

    def test_a_proposal_made_before_another_revision_goes_stale_instead_of_applying(self):
        a = self.submit(param='exit.stop_pct', new=0.02)
        b = self.submit(param='exit.target_pct', new=0.06)
        pr.approve(b['id'], directory=self.dir)
        with self.assertRaises(pr.ProposalError) as ctx:
            pr.approve(a['id'], directory=self.dir)
        self.assertIn('旧修订', str(ctx.exception))
        self.assertEqual(pr.active(self.dir)[1], {'breakout': {'exit.target_pct': 0.06}})
        self.assertEqual({p['id']: p['status'] for p in self.store()['proposals']}[a['id']], 'stale')

    def test_approval_rechecks_evidence_against_the_stored_record(self):
        """提交时通过不等于批准时通过：存储被改动/证据被篡改，批准时必须重新过关。"""
        p = self.submit()
        store = self.store()
        store['proposals'][0]['evidence']['n'] = 3
        Path(pr.path(self.dir)).write_text(json.dumps(store))
        with self.assertRaises(pr.ProposalError):
            pr.approve(p['id'], directory=self.dir)
        self.assertEqual(pr.active(self.dir)[0], 0)
        self.assertEqual(self.store()['proposals'][0]['status'], 'insufficient')

    def test_approval_rechecks_bounds_and_red_lines(self):
        p = self.submit()
        store = self.store()
        store['proposals'][0]['new'] = 0.5                  # 手改成越界
        Path(pr.path(self.dir)).write_text(json.dumps(store))
        with self.assertRaises(pr.ProposalError):
            pr.approve(p['id'], directory=self.dir)
        q = self.submit(param='exit.target_pct', new=0.06)
        store = self.store()
        store['proposals'][-1]['parameter'] = 'capital'      # 手改成红线
        Path(pr.path(self.dir)).write_text(json.dumps(store))
        with self.assertRaises(pr.ProposalError):
            pr.approve(q['id'], directory=self.dir)
        self.assertEqual(pr.active(self.dir)[0], 0)

    def test_reject_leaves_everything_unchanged(self):
        p = self.submit()
        pr.reject(p['id'], '不同意', directory=self.dir)
        self.assertEqual(self.store()['proposals'][0]['status'], 'rejected')
        self.assertEqual(pr.active(self.dir), (0, {}))

    def test_successive_revisions_replay_in_order(self):
        pr.approve(self.submit()['id'], directory=self.dir)
        ev1 = {**EV, 'execution_version': 'exec-0.2.r1'}
        pr.approve(self.submit(param='exit.stop_pct', new=0.0175, evidence=ev1)['id'], directory=self.dir)
        self.assertEqual(pr.active(self.dir), (2, {'breakout': {'exit.stop_pct': 0.0175}}))
        self.assertEqual(exec_spec.execution_version(2), 'exec-0.2.r2')

    def test_preview_shows_the_breakeven_effect(self):
        p = self.submit()
        eff = pr.preview(self.store(), p)
        self.assertEqual(eff['breakeven_before'], 29.6)
        self.assertLess(eff['breakeven_after'], eff['breakeven_before'])


class StorageTests(Base):
    def test_permissions_and_no_leftover_temp_files(self):
        self.submit()
        self.assertEqual(stat.S_IMODE(os.stat(pr.path(self.dir)).st_mode), 0o600)
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith('.tmp')], [])

    def test_corrupt_store_stops_loudly_instead_of_meaning_no_revisions(self):
        Path(pr.path(self.dir)).write_text('{broken')
        for fn in (lambda: pr.active(self.dir), lambda: self.submit()):
            with self.assertRaises(pr.ProposalError):
                fn()
        Path(pr.path(self.dir)).write_text('[]')
        with self.assertRaises(pr.ProposalError):
            pr.active(self.dir)

    def test_concurrent_submissions_do_not_lose_updates(self):
        def work(i):
            pr.submit('breakout', 'exit.target_pct', 0.06, 'r%d' % i, EV, directory=self.dir)
        threads = [threading.Thread(target=work, args=(i,)) for i in range(20)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        store = self.store()
        self.assertEqual(len(store['proposals']), 20)
        self.assertEqual(len({p['id'] for p in store['proposals']}), 20)
        self.assertEqual(sum(p['status'] == 'pending' for p in store['proposals']), 1)

    def test_trimming_never_drops_pending_or_approved_records(self):
        first = self.submit()
        pr.approve(first['id'], directory=self.dir)
        with patch.object(pr, 'MAX_RECORDS', 5):
            for i in range(12):
                self.submit(param='exit.nope', new=1)                 # refused 记录堆积
            pending = self.submit(param='exit.target_pct', new=0.06, evidence={**EV, 'execution_version': 'exec-0.2.r1'})
        ids = {p['id'] for p in self.store()['proposals']}
        self.assertIn(first['id'], ids)
        self.assertIn(pending['id'], ids)
        self.assertLessEqual(len(ids), 6)
        self.assertEqual(pr.active(self.dir)[0], 1)                     # 修订不受影响


class SpecOverrideTests(unittest.TestCase):
    def test_overrides_apply_without_mutating_the_base_specs(self):
        before = copy.deepcopy(exec_spec.SPECS)
        spec = exec_spec.build_spec('breakout', {'exit.stop_pct': 0.02}, 3)
        self.assertEqual((spec['exit']['stop_pct'], spec['revision']), (0.02, 3))
        self.assertEqual(exec_spec.SPECS, before)
        self.assertEqual(exec_spec.build_spec('breakout')['exit']['stop_pct'], 0.025)
        self.assertEqual(exec_spec.build_spec('breakout')['revision'], 0)

    def test_unknown_override_is_rejected(self):
        with self.assertRaises(KeyError):
            exec_spec.build_spec('breakout', {'capital': 1})

    def test_no_red_line_is_tunable(self):
        self.assertFalse(exec_spec.RED_LINES & set(exec_spec.TUNABLE_PARAMS))

    def test_every_tunable_default_is_inside_its_own_bounds_and_spec_is_consistent(self):
        for track in exec_spec.SPECS:
            spec = exec_spec.build_spec(track)
            self.assertEqual(exec_spec.check_spec(spec), [])
            for name, d in exec_spec.TUNABLE_PARAMS.items():
                v = exec_spec.get_param(spec, name)
                if v is not None:
                    self.assertTrue(d['floor'] <= v <= d['ceiling'], (track, name, v))

    def test_execution_version_strings(self):
        self.assertEqual(exec_spec.execution_version(0), 'exec-0.2')
        self.assertEqual(exec_spec.execution_version(1), 'exec-0.2.r1')


class EngineWiringTests(Base):
    def test_run_tags_the_report_with_the_revision_it_was_given(self):
        empty = Path(tempfile.mkdtemp())
        r = alpha_engine.run(empty, '20260919000000-1', (2, {'breakout': {'exit.stop_pct': 0.02}}))
        self.assertEqual(r['execution_version'], 'exec-0.2.r2')
        self.assertEqual(alpha_engine.run(empty, '20260919000000-2')['execution_version'], 'exec-0.2')

    def test_frozen_forecast_fields_carry_the_approved_revision_only_for_its_track(self):
        rev = (1, {'breakout': {'exit.stop_pct': 0.02}})
        b, p = alpha_engine.exec_fields('breakout', rev), alpha_engine.exec_fields('pullback', rev)
        self.assertEqual((b['execution_version'], b['exec_spec']['revision'], b['exec_spec']['exit']['stop_pct']),
                         ('exec-0.2.r1', 1, 0.02))
        self.assertEqual((p['execution_version'], p['exec_spec']['exit']['stop_pct']), ('exec-0.2.r1', 0.03))
        base = alpha_engine.exec_fields('breakout')
        self.assertEqual((base['execution_version'], base['exec_spec']['revision'], base['exec_spec']['exit']['stop_pct']),
                         ('exec-0.2', 0, 0.025))

    def test_a_frozen_spec_is_a_copy_later_revisions_cannot_reach(self):
        f = alpha_engine.exec_fields('breakout', (1, {'breakout': {'exit.stop_pct': 0.02}}))
        alpha_engine.exec_fields('breakout', (2, {'breakout': {'exit.stop_pct': 0.0175}}))
        self.assertEqual(f['exec_spec']['exit']['stop_pct'], 0.02)

    def test_library_run_never_reads_the_proposals_file(self):
        """cron 先跑全量测试：库若自己读文件，服务器上一批准提议，'没有修订'的断言就变样。"""
        p = self.submit()
        pr.approve(p['id'], directory=self.dir)
        with patch.dict(os.environ, {'PRIVATE_DATA_DIR': self.dir}):
            r = alpha_engine.run(Path(tempfile.mkdtemp()), '20260919000000-3')
        self.assertEqual(r['execution_version'], 'exec-0.2')


class WebTests(Base):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {'PRIVATE_DATA_DIR': self.dir, 'ADMIN_PASSWORD': PASSWORD,
                                      'CONFIG_PATH': os.path.join(self.dir, 'c.json')})
        env.start()
        self.addCleanup(env.stop)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), webapp.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def request(self, method, path, form=None, headers=None, auth=True):
        h = {'Host': '127.0.0.1:%d' % self.port}
        if auth:
            h['Authorization'] = AUTH
        h.update(headers or {})
        body = urlencode(form or {}).encode() if method == 'POST' else None
        if body is not None:
            h['Content-Type'] = 'application/x-www-form-urlencoded'
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        text = resp.read().decode('utf-8')
        conn.close()
        return resp.status, text

    def test_page_requires_login_and_is_in_the_nav(self):
        self.assertEqual(self.request('GET', '/proposals', auth=False)[0], 401)
        self.assertEqual(self.request('POST', '/proposals/approve', {'id': 'p0001'}, auth=False)[0], 401)
        status, page = self.request('GET', '/proposals')
        self.assertEqual(status, 200)
        self.assertIn('href="/proposals"', page)
        self.assertIn('尚无修订', page)

    def test_pending_proposal_is_shown_and_can_be_approved_from_the_page(self):
        p = self.submit(rationale='理由：<script>alert(1)</script>')
        _, page = self.request('GET', '/proposals')
        self.assertIn(p['id'], page)
        self.assertNotIn('<script>alert(1)</script>', page)        # 提议方文字必须转义
        self.assertIn('&lt;script&gt;', page)
        self.assertIn('未经验证', page)
        self.assertIn('29.6% → 25.7%', page)
        status, page = self.request('POST', '/proposals/approve', {'id': p['id'], 'note': '好'})
        self.assertEqual(status, 200)
        self.assertIn('已批准', page)
        self.assertIn('exec-0.2.r1', page)
        self.assertEqual(pr.active(self.dir)[0], 1)

    def test_reject_from_the_page(self):
        p = self.submit()
        _, page = self.request('POST', '/proposals/reject', {'id': p['id']})
        self.assertIn('已驳回', page)
        self.assertEqual(pr.active(self.dir)[0], 0)

    def test_refused_and_insufficient_items_show_in_history_but_have_no_buttons(self):
        r = self.submit(param='capital', new=1)
        i = self.submit(evidence={**EV, 'n': 3}, track='pullback', param='exit.target_pct', new=0.04)
        _, page = self.request('GET', '/proposals')
        self.assertIn('被代码拒收', page)
        self.assertIn('证据不足', page)
        self.assertNotIn('name="id" value="%s"' % r['id'], page)
        self.assertNotIn('name="id" value="%s"' % i['id'], page)

    def test_approving_something_unapprovable_via_a_forged_post_is_refused(self):
        i = self.submit(evidence={**EV, 'n': 3})
        _, page = self.request('POST', '/proposals/approve', {'id': i['id']})
        self.assertIn('不能批准', page)
        self.assertEqual(pr.active(self.dir)[0], 0)

    def test_cross_site_post_cannot_approve(self):
        p = self.submit()
        status, _ = self.request('POST', '/proposals/approve', {'id': p['id']},
                                 headers={'Origin': 'https://evil.example'})
        self.assertEqual(status, 403)
        self.assertEqual(pr.active(self.dir)[0], 0)

    def test_corrupt_store_shows_an_error_page_not_a_500(self):
        Path(pr.path(self.dir)).write_text('{broken')
        status, page = self.request('GET', '/proposals')
        self.assertEqual(status, 200)
        self.assertIn('无法读取', page)


class ReviewPipelineDoesNotSelfApplyTests(unittest.TestCase):
    def test_review_suggests_but_never_writes_the_live_tuning_pointer(self):
        import review_pipeline as rp
        from tushare_sync import save
        h = Path(tempfile.mkdtemp())
        for sub in ('outcomes', 'predictions'):
            (h / sub).mkdir(parents=True)
        for i in range(120):
            weak = i % 3 == 0
            day = '2026-03-%02d' % (i % 28 + 1)
            fid = 'f%02d' % i
            save(h / 'predictions' / (fid + '.json'),
                 {'id': fid, 'symbol': 'sh600000', 'name': 'x', 'strategy_type': None,
                  'regime': '偏弱' if weak else '趋势', 'features': {}})
            save(h / 'outcomes' / ('o%02d.json' % i),
                 {'prediction_id': fid, 'horizon': 10, 'entry_day': day, 'end_day': day, 'win': not weak,
                  'excess_pp': -2.0 if weak else 3.0, 'mfe_pct': 1.0, 'mae_pct': -1.0})
        report = rp.run(h, '20260919000000-1')
        self.assertEqual(report['status'], 'proposed')
        self.assertTrue(report['proposals'])
        self.assertIsNone(report['applied'])
        self.assertFalse((h / 'tuning' / 'active.json').exists())
        self.assertIn('未生效', rp.render(report))


if __name__ == '__main__':
    unittest.main()
