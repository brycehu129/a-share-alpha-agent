import copy
import tempfile
import unittest
from datetime import datetime, timedelta, date
from pathlib import Path
from alpha_engine import eligible_from, immutable, resolve
from alpha_model import features, estimate, label, screen, POLICY, percentiles
from alpha_portfolio import initial, advance, fee
from collect_quotes import CST


def bars(n=70, start='2026-01-05', growth=0.001):
    day = date.fromisoformat(start)
    rows = []
    while len(rows) < n:
        if day.weekday() < 5:
            p = 10*(1+growth)**len(rows)
            rows.append({'date': day.isoformat(), 'open': str(p), 'close': str(p),
                         'high': str(p*1.02), 'low': str(p*0.98), 'volume_raw': '100000'})
        day += timedelta(days=1)
    return rows


class AlphaTests(unittest.TestCase):
    def test_feature_no_future_prices(self):
        b = bars()
        altered = copy.deepcopy(b)
        altered[-1]['close'] = '100000'
        self.assertEqual(features(b,b,b[30]['date']), features(altered,altered,b[30]['date']))

    def test_missing_session_rejected(self):
        b = bars()
        with self.assertRaises(ValueError):
            features(b[:10]+b[11:], b, b[25]['date'])

    def test_sample_gate_and_purge(self):
        sample = {'end_day':'2026-03-01', 'entry_day':'2026-02-01', 'win':True,
                  'bucket':'80+', 'regime':'趋势', 'return_pct':2}
        self.assertIsNone(estimate([sample]*100, '2026-04-01')['probability'])
        self.assertEqual(estimate([sample]*100,'2026-03-01')['n'],0)
        samples = [{**sample,'entry_day':f'2026-02-{i//2+1:02}'} for i in range(30)]
        self.assertIsNotNone(estimate(samples,'2026-04-01')['probability'])

    def test_rank_ties(self):
        self.assertEqual(percentiles({'a':1,'b':1}), {'a':50,'b':50})

    def test_no_early_acceptance(self):
        b = bars(12)
        self.assertIsNone(label(b,b,b[2]['date'],10))
        self.assertIsNotNone(label(b,b,b[1]['date'],10))

    def test_immutable_conflict(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'f.json'
            immutable(p, {'x':1})
            immutable(p, {'x':1})
            with self.assertRaises(ValueError):
                immutable(p, {'x':2})

    def test_late_forecast(self):
        self.assertEqual(eligible_from(datetime(2026,9,16,9,21,tzinfo=CST)), '2026-09-17')
        self.assertEqual(eligible_from(datetime(2026,9,16,9,0,tzinfo=CST)), '2026-09-16')

    def setup_account(self):
        b = bars(24, growth=0)
        forecast = {'id':'p1','symbol':'sh600000','name':'样本', 'created_at':b[0]['date']+'T16:00:00+08:00',
                    'eligible_from':b[1]['date'],'as_of':b[0]['date'],'paper_eligible':True,'reference_price':10}
        return b, forecast, initial(b[0]['date'],10)

    def test_t_plus_one_and_costs_and_idempotence(self):
        b, f, state = self.setup_account()
        r = advance(state,[f],{'sh600000':b},{'sh600000':b},b,b[1]['date'])
        self.assertEqual(len(r['positions']),1)
        self.assertLess(r['equity'],100000)
        self.assertGreaterEqual(r['cash'],0)
        self.assertEqual(r,advance(r,[f],{'sh600000':b},{'sh600000':b},b,b[1]['date']))
        final = advance(r,[f],{'sh600000':b},{'sh600000':b},b,b[-1]['date'])
        sells = [t for t in final['trades'] if t['side']=='sell']
        self.assertEqual(len(sells),1)
        self.assertGreater(sells[0]['date'],b[1]['date'])
        self.assertLess(final['cash'],100000)

    def test_gap_and_limit_skip(self):
        b,f,state = self.setup_account()
        b[1].update(open='11',close='11',high='11',low='11')
        r = advance(state,[f],{'sh600000':b},{'sh600000':b},b,b[1]['date'])
        self.assertEqual(len(r['positions']),0)
        self.assertEqual(r['trades'][0]['side'],'skipped')

    def test_missing_price_freezes_nav(self):
        b,f,state = self.setup_account()
        r = advance(state,[f],{'sh600000':b},{'sh600000':b},b,b[1]['date'])
        failed = advance(r,[f],{'sh600000':b[:2]},{'sh600000':b},b,b[2]['date'])
        self.assertEqual(failed['last_date'],b[1]['date'])
        self.assertEqual(failed['valuation_status'],'blocked')
        self.assertEqual(failed['equity'],r['equity'])

    def test_split_blocks_instead_of_fake_loss(self):
        b,f,state = self.setup_account()
        r = advance(state,[f],{'sh600000':b},{'sh600000':b},b,b[1]['date'])
        adjusted = copy.deepcopy(b)
        adjusted[1]['close']='5'
        failed = advance(r,[f],{'sh600000':b},{'sh600000':adjusted},b,b[2]['date'])
        self.assertEqual(failed['valuation_status'],'blocked')

    def test_max_positions(self):
        b,f,state = self.setup_account()
        fs = [{**f,'id':'p'+str(i),'symbol':'sh60000'+str(i)} for i in range(5)]
        data = {x['symbol']:b for x in fs}
        r = advance(state,fs,data,data,b,b[1]['date'])
        self.assertEqual(len(r['positions']),3)
        self.assertGreaterEqual(r['cash'],0)

    def test_screen_excludes_st_and_missing(self):
        b=bars()
        stocks=[{'ts_code':'600000.SH','name':'ST样本','list_date':'20000101','industry':'银行','list_status':'L'},
                {'ts_code':'600001.SH','name':'样本','list_date':'20000101','industry':'银行','list_status':'L'}]
        r=screen(stocks,{},b,b[-1]['date'])
        self.assertEqual(r['valid'],0)
        self.assertFalse(r['complete'])
        self.assertEqual(len(r['excluded']),2)

    def test_drawdown_pauses_new_entries(self):
        b,f,state=self.setup_account()
        r=advance(state,[f],{'sh600000':b},{'sh600000':b},b,b[1]['date'])
        # Same raw/adjusted move is a real price loss, not a corporate adjustment.
        b[2].update(open='10',close='2',low='2')
        r=advance(r,[f],{'sh600000':b},{'sh600000':b},b,b[2]['date'])
        self.assertTrue(r['paused'])
        self.assertGreater(r['max_drawdown_pct'],20)


if __name__ == '__main__':
    unittest.main()
