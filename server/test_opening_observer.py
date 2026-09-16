import unittest
from datetime import datetime
from opening_observer import validate_quote,first_session
from collect_quotes import CST
from alpha_portfolio import initial, advance

class ObservedTests(unittest.TestCase):
    def test_only_observed_fields_are_used(self):
        now=datetime(2026,9,17,9,31,tzinfo=CST)
        row={'ts_code':'000001.SZ','trade_time':'2026-09-17 09:30:55','close':10,'pre_close':10,'vol':100}
        self.assertEqual(validate_quote(row,'000001.SZ',now)[0],10)
        self.assertEqual(validate_quote({**row,'high':99999,'low':-1,'open':0},'000001.SZ',now)[0],10)
        for patch in ({'trade_time':'2026-09-16 15:00:00'},{'trade_time':'2026-09-17 09:30:00'},{'trade_time':'2026-09-17 09:31:01'},{'vol':0},{'close':11},{'ts_code':'other'}):
            with self.assertRaises(ValueError):validate_quote({**row,**patch},'000001.SZ',now)
        with self.assertRaises(ValueError):validate_quote(row,'000001.SZ',datetime(2026,9,17,9,35,tzinfo=CST))
    def test_first_eligible_session_skips_closed_days(self):
        self.assertEqual(first_session({'eligible_from':'2026-09-19'},['2026-09-18','2026-09-21']),'2026-09-21')
    def test_close_processing_never_backfills_entry(self):
        bars=[{'date':d,'open':'10','high':'11','low':'9','close':'10','volume_raw':'100'} for d in ('2026-09-16','2026-09-17')]
        forecast={'id':'p','symbol':'sz000001','name':'测试','created_at':'2026-09-16T18:00:00+08:00','eligible_from':'2026-09-17','as_of':'2026-09-16','reference_price':10,'paper_eligible':True}
        state=advance(initial('2026-09-16',10),[forecast],{'sz000001':bars},{'sz000001':bars},bars,'2026-09-17',execute=False)
        self.assertEqual(state['trades'],[])
        self.assertEqual(state['cash'],100000)
        self.assertEqual(state['last_date'],'2026-09-17')
