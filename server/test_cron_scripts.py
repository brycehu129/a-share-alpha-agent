"""定时入口脚本的静态回归检查（不真的执行它们）。"""
import os
import re
import unittest
from pathlib import Path

HERE = Path(__file__).parent


class DailyAgentScriptTests(unittest.TestCase):
    def test_report_pipeline_gets_a_fresh_results_dir_every_run(self):
        """report_pipeline 拒绝复用已存在的 results/collection.sqlite3。服务器上默认的 ./results 是常驻目录，
        第一次成功之后每一轮都因此失败，而这一步是 warn_optional——看板的指数快照就这样静默停在了上周五。"""
        text = (HERE / 'cron_daily_agent.sh').read_text()
        line = next(l for l in text.splitlines() if 'report_pipeline.py' in l and 'python3' in l)
        self.assertIn('--results "$RESULTS_DIR"', line)
        self.assertRegex(text, r'RESULTS_DIR="\$\(mktemp -d\)"')
        self.assertIn('rm -rf "$RESULTS_DIR"', text)


class ReviewUnitTests(unittest.TestCase):
    def test_review_timer_runs_the_review_script_twice_after_the_close(self):
        timer = (HERE / 'systemd' / 'alpha-shadow-review.timer').read_text()
        self.assertEqual(re.findall(r'OnCalendar=(.+)', timer),
                         ['Mon..Fri 16:30:00 Asia/Shanghai', 'Mon..Fri 17:30:00 Asia/Shanghai'])
        service = (HERE / 'systemd' / 'alpha-shadow-review.service').read_text()
        self.assertIn('cron_review.sh', service)
        self.assertIn('EnvironmentFile=/etc/alpha-shadow.env', service)

    def test_review_script_is_executable_and_never_writes_git(self):
        path = HERE / 'cron_review.sh'
        self.assertTrue(os.access(path, os.X_OK))
        text = path.read_text()
        self.assertIn('set -euo pipefail', text)
        self.assertNotIn('commit_and_push', text)          # 公开行情、落本地文件，不进 market-data 分支
        self.assertIn('market_review.py', text)
        self.assertIn('watch_outcome.py', text)
        self.assertIn('next_day_watch.py', text)
        # 结算必须在生成当日新名单之前跑，否则读不到"结算前"的 pools 原始数据
        self.assertLess(text.index('watch_outcome.py'), text.index('next_day_watch.py'))


if __name__ == '__main__':
    unittest.main()
