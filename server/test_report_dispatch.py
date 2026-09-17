import unittest
from datetime import datetime
from collect_quotes import CST
from report_dispatch import choose
from expire_plans import expire


class RecoveryTests(unittest.TestCase):
    def now(self, clock):
        return datetime.fromisoformat('2026-09-17T'+clock).replace(tzinfo=CST)

    def test_delayed_prepare_becomes_report(self):
        self.assertEqual(choose(self.now('09:19:00'), {}), ('premarket','premarket'))
        self.assertEqual(choose(self.now('07:15:00'), {}), ('prepare','prepare'))

    def test_checkpoint_and_final_phase(self):
        state={'checkpoints':{'premarket':{'report_date':'2026-09-17','status':'ready'},
                              'close':{'report_date':'2026-09-17','status':'ready'}}}
        self.assertIsNone(choose(self.now('09:10:00'), state))
        self.assertEqual(choose(self.now('10:17:00'), state), ('premarket','premarket_after_open'))
        state['checkpoints']['premarket_after_open']={'report_date':'2026-09-17','status':'ready'}
        self.assertIsNone(choose(self.now('10:17:00'), state))
        self.assertEqual(choose(self.now('18:21:00'), state), ('close','close_final'))
        state['checkpoints']['premarket']['status']='failed'
        self.assertEqual(choose(self.now('09:10:00'), state), ('premarket','premarket'))
        self.assertIsNone(choose(self.now('01:00:00'), {}))

    def test_expiry_is_idempotent_and_never_trades(self):
        state={'positions':[], 'attempted':[], 'trades':[], 'cash':100000}
        fs=[{'id':'a','symbol':'sh600000','paper_eligible':True,'eligible_from':'2026-09-17'},
            {'id':'b','symbol':'sh600001','paper_eligible':True,'eligible_from':'2026-09-18'}]
        dates=['2026-09-16','2026-09-17','2026-09-18']
        expire(state,fs,dates,self.now('09:34:59'))
        self.assertEqual(state['trades'],[])
        expire(state,fs,dates,self.now('09:35:00'))
        expire(state,fs,dates,self.now('10:35:00'))
        self.assertEqual(state['attempted'],['a'])
        self.assertEqual([t['side'] for t in state['trades']],['skipped'])
        self.assertEqual(state['cash'],100000)
        self.assertEqual(state['positions'],[])
