"""deploy.sh 的回归测试：在沙箱里用真 git/cp/install/mv 跑（systemctl 和 pip3 换成记录调用的桩，
路径全部指向临时目录，绝不碰真实的 /etc 和 systemd）。

守的是一次真实事故：deploy.sh 的最后一步会替换**正在运行的**外层 wrapper。bash 边读边执行脚本，
替换成更长的内容后会从旧偏移读到新文件中间的半截文字当命令——`command not found`，退出码 127，
而这时前面的步骤其实都已经执行完了（代码已更新、新 timer 却没装上）。之前几次部署成功只是因为
文件内容没变，覆盖前后一模一样。
"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name('deploy.sh')
RUN = dict(capture_output=True, text=True, errors='replace')
TIMERS = ('daily', 'opening', 'postclose', 'intraday', 'sentinel-analyst', 'sentinel-reconcile')


def sh(cmd, cwd=None):
    return subprocess.run(cmd, shell=True, cwd=cwd, check=True, **RUN)


class Sandbox:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix='deploytest-'))
        origin, seed = self.root / 'origin.git', self.root / 'seed'
        self.opt = self.root / 'opt'
        sh('git init -q --bare -b master %s' % origin)
        (seed / 'server/systemd').mkdir(parents=True)
        (seed / 'server/deploy.sh').write_text(SCRIPT.read_text())
        for n in TIMERS:
            (seed / ('server/systemd/alpha-shadow-%s.service' % n)).write_text('[Service]\n')
            (seed / ('server/systemd/alpha-shadow-%s.timer' % n)).write_text('[Timer]\n')
        sh('git init -q -b master && git add -A && git -c user.name=t -c user.email=t@t commit -q -m seed '
           '&& git remote add origin %s && git push -q origin master' % origin, cwd=seed)
        sh('git clone -q %s %s' % (origin, self.opt))
        self.wrapper = self.opt / 'deploy.sh'                       # 服务器上的外层 wrapper（SSH 强制命令的目标）
        self.wrapper.write_text('#!/bin/bash\necho OLD-WRAPPER\n')
        self.wrapper.chmod(0o755)
        self.bin, self.calls = self.root / 'bin', self.root / 'calls.log'
        self.bin.mkdir()
        self.units, self.log, self.envf = self.root / 'units', self.root / 'deploy.log', self.root / 'env'
        self.units.mkdir()
        self.stub('systemctl')

    def stub(self, name, body=''):
        p = self.bin / name
        p.write_text('#!/bin/bash\necho "%s $*" >> %s\n%s' % (name, self.calls, body))
        p.chmod(0o755)

    def run(self, script=None, env_text='', pip_fail=False):
        self.calls.write_text('')
        self.stub('pip3', 'exit 1\n' if pip_fail else '')
        if env_text is None:
            self.envf.unlink(missing_ok=True) if hasattr(Path, 'unlink') else None
        else:
            self.envf.write_text(env_text)
        env = {**os.environ, 'PATH': '%s:%s' % (self.bin, os.environ['PATH']), 'DEPLOY_ROOT': str(self.opt),
               'UNIT_DIR': str(self.units), 'ENV_FILE': str(self.envf), 'DEPLOY_LOG': str(self.log)}
        r = subprocess.run(['bash', str(script or self.opt / 'server/deploy.sh')], env=env, **RUN)
        return r, self.calls.read_text().splitlines()

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class SelfReplacementTests(unittest.TestCase):
    def test_a_running_script_replaced_by_a_longer_file_is_the_bug_being_guarded_against(self):
        """复现线上的 127：这条测试证明"危险写法"确实会出事，别的测试才有意义。"""
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        new = d / 'new.sh'
        new.write_text('#!/bin/bash\n' + 'echo "很长很长很长很长的新版本内容，确保比旧的长出一大截"\n' * 6)
        w = d / 'w.sh'
        w.write_text('#!/bin/bash\nset -e\necho hi\ncp %s %s\necho AFTER\n' % (new, w))
        r = subprocess.run(['bash', str(w)], **RUN)
        # 症状取决于新文件里被读到的那半截文字：可能是 command not found(127)，也可能是语法错误(2)。
        # 共同点是：不是干净退出，而且覆盖之后的最后一行根本没执行。
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn('AFTER', r.stdout)

    # 注意：这里曾经有两条"新 deploy.sh 不得比旧 wrapper(1249/2710 字节)更长"的测试，那只是为了度过
    # 2026-09-19 那一次过渡（服务器上运行的还是会 cp 覆盖自己的旧 wrapper）。过渡已经完成，服务器上的
    # wrapper 现在就是 main+exit+install/mv 的新写法，deploy.sh 变长也不会再出事——保留那个尺寸约束
    # 反而是陷阱：有人给 deploy.sh 加一行，服务器上 cron 跑测试时就会失败，连带让日线流程中止。


class DeployBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.cleanup)

    def test_syncs_every_unit_and_enables_every_timer(self):
        r, calls = self.sb.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        enable = next(c for c in calls if c.startswith('systemctl enable --now'))
        self.assertEqual(enable.count('.timer'), len(TIMERS))              # 新增 timer 不必再改脚本
        self.assertEqual(len(list(self.sb.units.glob('*.timer'))), len(TIMERS))
        self.assertEqual(len(list(self.sb.units.glob('*.service'))), len(TIMERS))
        order = [c.split()[1] for c in calls if c.startswith('systemctl')]
        self.assertLess(order.index('restart'), order.index('daemon-reload'))
        self.assertLess(order.index('daemon-reload'), order.index('enable'))

    def test_wrapper_is_replaced_atomically_and_stays_executable(self):
        inode = self.sb.wrapper.stat().st_ino
        r, _ = self.sb.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.sb.wrapper.read_text(), SCRIPT.read_text())
        self.assertNotEqual(self.sb.wrapper.stat().st_ino, inode)             # 换了 inode，不是就地改写
        self.assertEqual(stat.S_IMODE(self.sb.wrapper.stat().st_mode), 0o755)
        self.assertFalse((self.sb.opt / 'deploy.sh.new').exists())

    def test_a_wrapper_that_is_already_the_new_version_can_replace_itself(self):
        self.sb.run()
        r, _ = self.sb.run(script=self.sb.wrapper)                            # 这一次运行的就是被替换的那个文件
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('command not found', r.stderr)

    def test_anthropic_package_is_installed_only_when_needed(self):
        cases = [(None, True), ('ANTHROPIC_API_KEY=x\n', True), ('OPENROUTER_API_KEY=sk-or-v1-abc\n', False),
                 ('OPENROUTER_API_KEY=sk-or-v1-abc\nLLM_PROVIDER=anthropic\n', True), ('OPENROUTER_API_KEY=\n', True)]
        for env_text, expect in cases:
            if env_text is None:
                self.sb.envf.unlink(missing_ok=True)
                self.sb.calls.write_text('')
                r, calls = self.sb.run(env_text=None)
            else:
                r, calls = self.sb.run(env_text=env_text)
            self.assertEqual(r.returncode, 0, (env_text, r.stderr))
            self.assertEqual(any(c.startswith('pip3') for c in calls), expect, env_text)

    def test_a_failed_pip_install_does_not_abort_the_deployment(self):
        r, calls = self.sb.run(pip_fail=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn('WARN', r.stdout)
        self.assertTrue(any('daemon-reload' in c for c in calls))

    def test_a_failing_step_stops_the_script_instead_of_carrying_on(self):
        self.sb.stub('systemctl', '[ "$1" = daemon-reload ] && exit 5 || true\n')
        r, calls = self.sb.run(env_text='OPENROUTER_API_KEY=sk-or-v1-abc\n')
        self.assertEqual(r.returncode, 5)
        self.assertFalse(any(c.startswith('systemctl enable') for c in calls))


class ScriptShapeTests(unittest.TestCase):
    def test_body_is_wrapped_in_a_function_and_ends_with_exit(self):
        """bash 会先把整个函数解析完，`exit` 之后不再读文件——即使文件在运行中被换掉也无所谓。"""
        text = SCRIPT.read_text()
        self.assertIn('main() {', text)
        self.assertTrue(text.rstrip().endswith('main\nexit'))
        self.assertIn('mv -f', text)
        self.assertNotRegex(text, r'\ncp [^\n]*deploy\.sh')                  # 不能再用 cp 直接覆盖 wrapper


if __name__ == '__main__':
    unittest.main()
