import unittest

import add_advisor as aa

H = {'symbol': 'sh600519', 'cost_price': 10.0, 'shares': 1000, 'stop_price': 9.5, 'stop_source': 'cost',
    'levels': {'stop_pct': 0.05, 'atr_pct': 0.05, 'nominal': False}}


def quote(last=9.6):
    return {'last': str(last)}


def limits(**kw):
    return {'at_limit_up': False, 'to_limit_up_pct': 5.0, **kw}


def triage(bucket='pullback'):
    return {'bucket': bucket, 'reasons': []}


ACCOUNT = {'equity_base': 100000.0, 'cash': 50000.0}


class VetoTests(unittest.TestCase):
    def evaluate(self, **kw):
        base = dict(h=H, q=quote(), facts={}, limits=limits(), triage=triage(), holding_action='hold',
                   account=ACCOUNT, flow={'main_30m': 0}, buy_setup_ok=True)
        base.update(kw)
        return aa.evaluate(**base)

    def test_a_sell_signal_in_place_vetoes_the_add(self):
        r = self.evaluate(holding_action='exit')
        self.assertFalse(r['ok'])
        self.assertIn('彻底卖出', r['veto'])
        r = self.evaluate(holding_action='reduce')
        self.assertIn('可暂时卖出', r['veto'])

    def test_broken_triage_vetoes_the_add(self):
        r = self.evaluate(triage=triage('broken'))
        self.assertFalse(r['ok'])
        self.assertIn('趋势已坏', r['veto'])

    def test_no_triage_verdict_vetoes_the_add(self):
        r = self.evaluate(triage=triage(None))
        self.assertIn('分诊不出结果', r['veto'])

    def test_market_pause_vetoes_the_add(self):
        r = self.evaluate(market_pause=True)
        self.assertIn('暂停线', r['veto'])

    def test_a_crashing_market_or_sector_vetoes_the_add(self):
        self.assertIn('大盘大跌', self.evaluate(market=-2.0)['veto'])
        self.assertIn('板块整体杀跌', self.evaluate(sector=-3.0)['veto'])

    def test_near_or_at_the_limit_up_vetoes_the_add(self):
        self.assertIn('涨停', self.evaluate(limits=limits(at_limit_up=True))['veto'])
        self.assertIn('涨停', self.evaluate(limits=limits(to_limit_up_pct=0.5))['veto'])

    def test_too_many_prior_adds_vetoes_the_add(self):
        r = self.evaluate(add_state={'add_count': 2})
        self.assertIn('上限', r['veto'])

    def test_too_soon_after_the_last_add_vetoes_the_add(self):
        r = self.evaluate(days_since_last_add=1)
        self.assertIn('交易日', r['veto'])
        self.assertTrue(self.evaluate(days_since_last_add=3)['ok'])

    def test_price_too_close_to_the_last_buy_price_vetoes_the_add(self):
        # ATR 5%，需要跌幅 ≥5% 才够；现价 9.6 相对上次买入价 10.0 只跌了 4%。
        r = self.evaluate(last_buy_price=10.0)
        self.assertIn('更便宜', r['veto'])
        self.assertTrue(self.evaluate(last_buy_price=10.5)['ok'])   # 相对 10.5 跌了 8.6%，够了

    def test_missing_account_vetoes_the_add(self):
        r = self.evaluate(account=None)
        self.assertIn('未填写账户资金', r['veto'])

    def test_missing_flow_vetoes_the_add(self):
        r = self.evaluate(flow=None)
        self.assertIn('资金流数据取不到', r['veto'])


class SupportTests(unittest.TestCase):
    def evaluate(self, **kw):
        base = dict(h=H, q=quote(), facts={}, limits=limits(), triage=triage(), holding_action='hold',
                   account=ACCOUNT, flow={'main_30m': 0}, buy_setup_ok=True)
        base.update(kw)
        return aa.evaluate(**base)

    def test_range_bucket_is_also_supported(self):
        self.assertTrue(self.evaluate(triage=triage('range'))['ok'])

    def test_healthy_bucket_is_not_a_reason_to_add(self):
        r = self.evaluate(triage=triage('healthy'))
        self.assertFalse(r['ok'])
        self.assertIn('不是回调也不是区间震荡', r['support'])

    def test_failing_the_pullback_price_gates_blocks_the_add(self):
        r = self.evaluate(buy_setup_ok=False)
        self.assertFalse(r['ok'])
        self.assertIn('价格门槛', r['support'])

    def test_a_clear_outflow_blocks_the_add(self):
        r = self.evaluate(flow={'main_30m': -5e6})
        self.assertFalse(r['ok'])
        self.assertIn('净流出', r['support'])

    def test_a_tiny_outflow_is_not_treated_as_a_real_veto(self):
        r = self.evaluate(flow={'main_30m': -100})
        self.assertTrue(r['ok'])


class SizingTests(unittest.TestCase):
    def test_wider_volatility_shrinks_rather_than_grows_the_add_size(self):
        """反马丁格尔的回归测试：波动率更大（止损幅度更宽）意味着每股风险更高，同样的风险预算下
        能补的股数应该更少（或干脆算不出来），不是"越猛的票敢买得越多"。"""
        calm = {**H, 'levels': {'stop_pct': 0.05, 'atr_pct': 0.05, 'nominal': False}}
        wild = {**H, 'levels': {'stop_pct': 0.08, 'atr_pct': 0.08, 'nominal': False}}
        a = aa.evaluate(h=calm, q=quote(9.6), facts={}, limits=limits(), triage=triage(),
                        holding_action='hold', account=ACCOUNT, flow={'main_30m': 0}, buy_setup_ok=True)
        b = aa.evaluate(h=wild, q=quote(9.6), facts={}, limits=limits(), triage=triage(),
                        holding_action='hold', account=ACCOUNT, flow={'main_30m': 0}, buy_setup_ok=True)
        self.assertTrue(a['ok'])
        if b['ok']:
            self.assertLessEqual(b['add_shares'], a['add_shares'])

    def test_preview_shows_the_lowered_cost_and_the_moved_stop(self):
        r = aa.evaluate(h=H, q=quote(9.6), facts={}, limits=limits(), triage=triage(), holding_action='hold',
                        account=ACCOUNT, flow={'main_30m': 0}, buy_setup_ok=True)
        self.assertTrue(r['ok'])
        self.assertGreater(r['add_shares'], 0)
        self.assertLess(r['preview']['new_cost'], r['preview']['old_cost'])
        lines = aa.preview_text(r['preview'])
        self.assertEqual(len(lines), 3)
        self.assertIn('摊低的是成本线，不是风险', lines[2])

    def test_add_size_is_a_multiple_of_the_lot_and_within_cash(self):
        tiny_cash = {'equity_base': 100000.0, 'cash': 500.0}
        r = aa.evaluate(h=H, q=quote(9.6), facts={}, limits=limits(), triage=triage(), holding_action='hold',
                        account=tiny_cash, flow={'main_30m': 0}, buy_setup_ok=True)
        if r['ok']:
            self.assertEqual(r['add_shares'] % 100, 0)
            self.assertLessEqual(r['add_shares'] * 9.6, 500.0)

    def test_no_room_under_the_risk_budget_is_a_veto_not_a_zero_share_suggestion(self):
        almost_maxed = {**H, 'shares': 29000}   # 已经接近单只 30% 上限（29000*10=290000 已经超过 100000*30%）
        r = aa.evaluate(h=almost_maxed, q=quote(9.6), facts={}, limits=limits(), triage=triage(),
                        holding_action='hold', account=ACCOUNT, flow={'main_30m': 0}, buy_setup_ok=True)
        self.assertFalse(r['ok'])
        self.assertIsNone(r['add_shares'])
        self.assertIsNotNone(r['veto'])


if __name__ == '__main__':
    unittest.main()
