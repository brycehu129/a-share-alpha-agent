import unittest
from decision_view import decisions

class DecisionTests(unittest.TestCase):
    def fixture(self):
        return {'candidates':[],'forecasts':[{'id':'p','symbol':'sh600000','name':'测试','created_at':'2026-09-16T18:00:00+08:00','paper_eligible':True,'reference_price':10}],
                'portfolio':{'positions':[],'trades':[],'valuation_status':'current','paused':False}}
    def test_ledger_has_precedence_over_plan(self):
        a=self.fixture()
        self.assertEqual(decisions(a)[0]['state'],'waiting_buy')
        a['portfolio']['trades']=[{'prediction_id':'p','side':'skipped','reason':'跳空'}]
        self.assertEqual(decisions(a)[0]['state'],'skipped')
        a['portfolio']['trades']=[{'prediction_id':'p','side':'sell','reason':'止盈'}]
        self.assertEqual(decisions(a)[0]['state'],'exited')
        a['portfolio']['positions']=[{'id':'p','symbol':'sh600000','name':'测试','entry_price':10,'shares':100,'exit_signal':'收盘触发止损'}]
        d=decisions(a)[0]
        self.assertEqual(d['state'],'pending_sell')
        self.assertEqual(d['stop_price'],9.4)
        self.assertEqual(d['target_price'],11.2)
    def test_legacy_reason_not_invented(self):
        a=self.fixture();a['forecasts'][0]['paper_eligible']=False
        self.assertIn('旧记录',decisions(a)[0]['reasons'][0])
