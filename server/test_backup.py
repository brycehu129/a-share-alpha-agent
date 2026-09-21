import base64
import http.client
import io
import json
import os
import stat
import tarfile
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import backup
import webapp

SECRET = 'sk-or-v1-supersecretkey1234567890'
NOW = datetime.fromisoformat('2026-09-20T17:30:00+08:00')
PASSWORD = 'pw-for-tests'
AUTH = 'Basic ' + base64.b64encode(('x:' + PASSWORD).encode()).decode()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / 'private'
        self.dest = self.tmp / 'backups'
        (self.root / 'intraday').mkdir(parents=True)
        (self.root / 'holdings.json').write_text('[{"symbol": "sh600000", "shares": 100}]')
        (self.root / 'proposals.json').write_text('{"seq": 1}')
        (self.root / 'intraday' / 'ledger-exec-0.2.json').write_text('{"equity": 101250.0}')
        (self.root / 'llm_settings.json').write_text(json.dumps({'api_key': SECRET}))
        (self.root / 'proposals.json.lock').write_text('')
        (self.root / 'x.tmp').write_text('half written')
        env = patch.dict(os.environ, {'PRIVATE_DATA_DIR': str(self.root), 'BACKUP_DIR': str(self.dest),
                                      'ADMIN_PASSWORD': PASSWORD, 'CONFIG_PATH': str(self.tmp / 'config.json')})
        env.start()
        self.addCleanup(env.stop)
        import llm_settings                          # 配置页会调用 llm_settings.apply()，别把进程内状态漏给别的测试
        saved = dict(llm_settings._ORIGINALS)
        llm_settings._ORIGINALS.clear()
        self.addCleanup(lambda: (llm_settings._ORIGINALS.clear(), llm_settings._ORIGINALS.update(saved)))

    def snap(self, when=NOW, **kw):
        return backup.snapshot(self.root, self.dest, now=when, **kw)

    def rewrite(self, archive, mutate):
        """读出归档成员 → 让 mutate 改 {名: 字节} → 重新打包（保持 gzip tar 格式），用来构造被篡改/恶意的归档。"""
        with tarfile.open(archive, 'r:gz') as tar:
            members = {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
        mutate(members)
        with tarfile.open(archive, 'w:gz') as tar:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))


class CollectTests(Base):
    def test_includes_data_and_excludes_credentials_temp_files_locks_and_links(self):
        (self.root / 'link.json').symlink_to(self.root / 'holdings.json')
        names = [rel for rel, _ in backup.collect(self.root)]
        self.assertEqual(names, ['holdings.json', 'intraday/ledger-exec-0.2.json', 'proposals.json'])

    def test_backup_directory_inside_the_root_is_not_archived_into_itself(self):
        inside = self.root / 'backups'
        inside.mkdir()
        (inside / 'alpha-shadow-private-20260101-000000.tar.gz').write_bytes(b'old')
        with patch.dict(os.environ, {'BACKUP_DIR': str(inside)}):
            self.assertNotIn('backups/alpha-shadow-private-20260101-000000.tar.gz', [r for r, _ in backup.collect(self.root)])


class SnapshotTests(Base):
    def test_roundtrip_restores_identical_files(self):
        path, manifest = self.snap()
        self.assertEqual(len(manifest['files']), 3)
        target = self.tmp / 'restored'
        self.assertEqual(backup.restore(path, target), 3)
        for rel, src in backup.collect(self.root):
            self.assertEqual((target / rel).read_bytes(), src.read_bytes())

    def test_credentials_never_enter_the_archive(self):
        path, _ = self.snap()
        with tarfile.open(path, 'r:gz') as tar:
            blob = b''.join(tar.extractfile(m).read() for m in tar.getmembers() if m.isfile())
        self.assertNotIn(SECRET.encode(), blob)
        self.assertNotIn(b'llm_settings', blob.replace(b'llm_settings.json', b''))       # manifest 的 excluded 列表里提到名字是允许的

    def test_permissions(self):
        path, _ = self.snap()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.dest).st_mode), 0o700)

    def test_rotation_keeps_only_the_newest(self):
        for i in range(5):
            self.snap(NOW + timedelta(days=i), keep=3)
        names = [p.name for p in backup.list_snapshots(self.dest)]
        self.assertEqual(len(names), 3)
        self.assertEqual(names[-1], 'alpha-shadow-private-20260924-173000.tar.gz')

    def test_old_snapshots_are_untouched_when_the_new_one_fails_verification(self):
        for i in range(3):
            self.snap(NOW + timedelta(days=i), keep=3)
        before = backup.list_snapshots(self.dest)
        with patch.object(backup, 'verify', return_value=(False, ['坏了'], None)):
            with self.assertRaises(backup.BackupError):
                self.snap(NOW + timedelta(days=3), keep=3)
        self.assertEqual(backup.list_snapshots(self.dest), before)
        self.assertEqual(list(self.dest.glob('*.partial')), [])
        self.assertIn('回读校验失败', backup.read_status(self.dest)['last_error'])

    def test_an_empty_or_missing_root_never_produces_an_empty_snapshot(self):
        empty = self.tmp / 'empty'
        empty.mkdir()
        for bad in (empty, self.tmp / 'nope'):
            with self.assertRaises(backup.BackupError):
                backup.snapshot(bad, self.dest, now=NOW)
        self.assertEqual(backup.list_snapshots(self.dest), [])

    def test_file_vanishing_mid_snapshot_does_not_fail_the_whole_thing(self):
        real = Path.read_bytes

        def flaky(path):
            if path.name == 'proposals.json':
                raise FileNotFoundError
            return real(path)
        with patch.object(Path, 'read_bytes', flaky):
            _, manifest = self.snap()
        self.assertEqual(len(manifest['files']), 2)


class VerifyTests(Base):
    def test_detects_tampered_content(self):
        path, _ = self.snap()
        self.rewrite(path, lambda m: m.__setitem__('files/holdings.json', b'[]'))
        ok, problems, _ = backup.verify(path)
        self.assertFalse(ok)
        self.assertIn('holdings.json', problems[0])

    def test_detects_missing_and_extra_members(self):
        path, _ = self.snap()
        self.rewrite(path, lambda m: m.pop('files/proposals.json'))
        self.assertIn('缺少文件', backup.verify(path)[1][0])
        path2, _ = self.snap(NOW + timedelta(days=1))
        self.rewrite(path2, lambda m: m.__setitem__('files/extra.json', b'x'))
        self.assertIn('多余成员', backup.verify(path2)[1][0])

    def test_rejects_traversal_absolute_and_stray_members(self):
        for name in ('files/../../evil', '/etc/evil', 'evil', 'files/../evil'):
            path, _ = self.snap(NOW + timedelta(seconds=len(name)))
            self.rewrite(path, lambda m, n=name: m.__setitem__(n, b'x'))
            self.assertFalse(backup.verify(path)[0], name)

    def test_symlink_member_is_rejected(self):
        path, _ = self.snap()
        with tarfile.open(path, 'a') if False else open(os.devnull) as _:
            pass
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w:gz') as tar:
            info = tarfile.TarInfo('files/link')
            info.type, info.linkname = tarfile.SYMTYPE, '/etc/passwd'
            tar.addfile(info)
        Path(path).write_bytes(raw.getvalue())
        self.assertFalse(backup.verify(path)[0])

    def test_garbage_and_missing_files_fail_cleanly(self):
        junk = self.tmp / 'junk.tar.gz'
        junk.write_bytes(b'not a tar')
        self.assertFalse(backup.verify(junk)[0])
        self.assertFalse(backup.verify(self.tmp / 'missing.tar.gz')[0])


class DefenceInDepthTests(Base):
    """路径防护有两层（成员检查 + 解包边界检查），一层失效另一层能兜住——所以要各自单独测，否则一层坏了没人发现。"""

    def test_member_check_rejects_unsafe_names_and_types(self):
        def member(name, kind=tarfile.REGTYPE):
            info = tarfile.TarInfo(name)
            info.type = kind
            return info
        for bad in ('/etc/passwd', 'files/../x', '../x', 'other/x', 'files', 'x'):
            self.assertFalse(backup._safe_member(member(bad)), bad)
        self.assertFalse(backup._safe_member(member('files/link', tarfile.SYMTYPE)))
        self.assertFalse(backup._safe_member(member('files/dir', tarfile.DIRTYPE)))
        self.assertTrue(backup._safe_member(member('files/a/b.json')))
        self.assertTrue(backup._safe_member(member('manifest.json')))

    def test_restore_boundary_check_holds_even_if_verification_is_fooled(self):
        path, manifest = self.snap()
        self.rewrite(path, lambda m: m.__setitem__('files/../outside.txt', b'x'))       # 成员真的存在，解包本来会成功
        evil = dict(manifest, files=[{'path': '../outside.txt', 'size': 1, 'sha256': 'x'}])
        with patch.object(backup, 'verify', return_value=(True, [], evil)):
            with self.assertRaises(backup.BackupError) as ctx:
                backup.restore(path, self.tmp / 'live')
        self.assertIn('越界', str(ctx.exception))
        self.assertFalse((self.tmp / 'outside.txt').exists())


class RestoreTests(Base):
    def test_refuses_to_overwrite_different_files_unless_forced(self):
        path, _ = self.snap()
        target = self.tmp / 'live'
        (target).mkdir()
        (target / 'holdings.json').write_text('[]')
        with self.assertRaises(backup.BackupError) as ctx:
            backup.restore(path, target)
        self.assertIn('--force', str(ctx.exception))
        self.assertEqual((target / 'holdings.json').read_text(), '[]')                # 一个字节都没动
        self.assertFalse((target / 'proposals.json').exists())                         # 也没有半截恢复
        backup.restore(path, target, force=True)
        self.assertIn('sh600000', (target / 'holdings.json').read_text())

    def test_identical_existing_files_are_not_a_conflict(self):
        path, _ = self.snap()
        target = self.tmp / 'live'
        backup.restore(path, target)
        self.assertEqual(backup.restore(path, target), 3)

    def test_tampered_archive_is_not_restored_at_all(self):
        path, _ = self.snap()
        self.rewrite(path, lambda m: m.__setitem__('files/holdings.json', b'[]'))
        target = self.tmp / 'live'
        with self.assertRaises(backup.BackupError):
            backup.restore(path, target)
        self.assertFalse(target.exists())

    def test_malicious_path_cannot_write_outside_the_target(self):
        path, _ = self.snap()
        outside = self.tmp / 'outside.txt'

        def evil(m):
            manifest = json.loads(m['manifest.json'])
            import hashlib
            manifest['files'].append({'path': '../outside.txt', 'size': 1, 'sha256': hashlib.sha256(b'x').hexdigest()})
            m['manifest.json'] = json.dumps(manifest).encode()
            m['files/../outside.txt'] = b'x'
        self.rewrite(path, evil)
        with self.assertRaises(backup.BackupError):
            backup.restore(path, self.tmp / 'live')
        self.assertFalse(outside.exists())


class HealthTests(Base):
    def test_states(self):
        self.assertEqual(backup.health(self.dest, NOW)[0], 'none')
        self.snap()
        self.assertEqual(backup.health(self.dest, NOW + timedelta(hours=2))[0], 'ok')
        level, text = backup.health(self.dest, NOW + timedelta(hours=40))
        self.assertEqual(level, 'warn')
        self.assertIn('没有新的成功快照', text)

    def test_failure_after_a_success_is_flagged_even_though_an_old_snapshot_exists(self):
        self.snap()
        with self.assertRaises(backup.BackupError):
            backup.snapshot(self.tmp / 'nope', self.dest, now=NOW + timedelta(days=1))
        level, text = backup.health(self.dest, NOW + timedelta(days=1, hours=1))
        self.assertEqual(level, 'warn')
        self.assertIn('最近一次失败', text)
        self.snap(NOW + timedelta(days=2))
        self.assertEqual(backup.health(self.dest, NOW + timedelta(days=2, hours=1))[0], 'ok')


class CliTests(Base):
    def run_cli(self, *args):
        out = io.StringIO()
        with patch('sys.stdout', out), patch('sys.stderr', io.StringIO()):
            code = backup.main(list(args))
        return code, out.getvalue()

    def test_snapshot_verify_list_restore(self):
        self.assertEqual(self.run_cli('snapshot')[0], 0)
        code, out = self.run_cli('verify')
        self.assertEqual(code, 0)
        self.assertIn('校验通过', out)
        self.assertIn('alpha-shadow-private-', self.run_cli('list')[1])
        target = self.tmp / 'r'
        self.assertEqual(self.run_cli('restore', str(backup.list_snapshots(self.dest)[-1]), '--to', str(target))[0], 0)
        self.assertTrue((target / 'holdings.json').exists())

    def test_failed_snapshot_exits_nonzero_and_tries_to_notify(self):
        with patch.dict(os.environ, {'PRIVATE_DATA_DIR': str(self.tmp / 'nope')}), \
                patch.object(backup, 'notify_failure') as notify:
            self.assertEqual(self.run_cli('snapshot')[0], 1)
        notify.assert_called_once()

    def test_notify_never_raises(self):
        with patch('wecom_push.send_wecom_message', side_effect=RuntimeError('boom')):
            backup.notify_failure('x')

    def test_the_backup_job_does_not_depend_on_the_test_suite(self):
        """别的定时入口都先跑全量测试；备份不行：代码有 bug 的那天恰恰最需要一份能回退的快照。"""
        script = (Path(backup.__file__).parent / 'cron_backup.sh').read_text()
        self.assertNotIn('unittest', script)
        self.assertIn('backup.py snapshot', script)
        service = (Path(backup.__file__).parent / 'systemd' / 'alpha-shadow-backup.service').read_text()
        timer = (Path(backup.__file__).parent / 'systemd' / 'alpha-shadow-backup.timer').read_text()
        self.assertIn('cron_backup.sh', service)
        self.assertIn('Persistent=true', timer)
        self.assertIn('Asia/Shanghai', timer)


class WebTests(Base):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), webapp.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def request(self, method, path, headers=None, auth=True):
        h = {'Host': '127.0.0.1:%d' % self.port}
        if auth:
            h['Authorization'] = AUTH
        h.update(headers or {})
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        conn.request(method, path, body=b'' if method == 'POST' else None, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data, resp

    def test_download_is_a_valid_archive_without_credentials(self):
        status, data, resp = self.request('POST', '/api/backup/download')
        self.assertEqual(status, 200)
        self.assertEqual(resp.getheader('Content-Type'), 'application/gzip')
        self.assertIn('attachment', resp.getheader('Content-Disposition'))
        self.assertEqual(resp.getheader('Cache-Control'), 'no-store')
        out = self.tmp / 'dl.tar.gz'
        out.write_bytes(data)
        ok, problems, manifest = backup.verify(out)
        self.assertTrue(ok, problems)
        self.assertEqual(len(manifest['files']), 3)
        with tarfile.open(out, 'r:gz') as tar:
            self.assertNotIn(SECRET.encode(), b''.join(tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()))

    def test_download_requires_login_and_rejects_cross_site(self):
        self.assertEqual(self.request('POST', '/api/backup/download', auth=False)[0], 401)
        self.assertEqual(self.request('POST', '/api/backup/download', headers={'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('POST', '/api/backup/download', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)

    def test_a_get_cannot_trigger_a_download(self):
        status, data, resp = self.request('GET', '/api/backup/download')
        self.assertEqual(status, 404)

    def test_oversize_data_is_refused_with_an_explanation(self):
        with patch.object(backup, 'MAX_DOWNLOAD_BYTES', 10):
            status, data, resp = self.request('POST', '/api/backup/download')
        self.assertEqual(status, 413)
        self.assertIn('超过网页下载上限', data.decode())
        self.assertNotEqual(resp.getheader('Content-Type'), 'application/gzip')

    def test_empty_private_dir_is_refused_not_downloaded(self):
        with patch.dict(os.environ, {'PRIVATE_DATA_DIR': str(self.tmp / 'nothing')}):
            status, data, resp = self.request('POST', '/api/backup/download')
        self.assertEqual(status, 404)
        self.assertIn('没有任何文件可备份', data.decode())

    def test_settings_api_reports_backup_health(self):
        import json
        settings = lambda: json.loads(self.request('GET', '/api/settings')[1].decode())
        first = settings()
        self.assertEqual(first['backup']['level'], 'none')
        self.assertIn('尚未运行', first['backup']['text'])
        self.assertNotIn(SECRET, json.dumps(first, ensure_ascii=False))
        self.snap()
        second = settings()
        self.assertEqual(second['backup']['level'], 'ok')
        self.assertIn('最近成功快照', second['backup']['text'])
        with patch.object(backup, 'STALE_HOURS', -100000):
            self.assertEqual(settings()['backup']['level'], 'warn')


if __name__ == '__main__':
    unittest.main()
