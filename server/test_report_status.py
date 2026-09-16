import unittest
from report_status import evaluate, SLOTS

class StatusTests(unittest.TestCase):
    def test_old_market_day_cannot_be_ready(self):
        agent={'status':'ready','screen':{'cutoff':'2026-09-15'}}
        self.assertEqual(evaluate(agent,'2026-09-16','success','open'),'partial')
        self.assertEqual(evaluate(agent,'2026-09-15','success','open'),'ready')
        self.assertEqual(evaluate(agent,'2026-09-15','failure','open'),'failed')
        self.assertEqual(evaluate(None,'2026-09-15','success','closed'),'closed')
        self.assertEqual(evaluate(agent,'2026-09-15','success','unknown'),'partial')
        self.assertEqual(SLOTS['40 0 * * 1-5'],'premarket')
