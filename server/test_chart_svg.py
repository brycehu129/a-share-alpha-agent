import re
import unittest
import xml.etree.ElementTree as ET

import chart_svg as cs


def minute(prices, start='0930'):
    bars = []
    h, m = int(start[:2]), int(start[2:])
    for i, p in enumerate(prices):
        mm = h * 60 + m + i
        if mm > 11 * 60 + 30 and mm < 13 * 60:
            mm += 90
        bars.append({'t': '%02d%02d' % (mm // 60, mm % 60), 'price': p, 'vwap': round(p * 0.999, 4)})
    return {'bars': bars}


class SlotTests(unittest.TestCase):
    def test_lunch_break_takes_no_room(self):
        self.assertEqual(cs.slot('0930'), 0)
        self.assertEqual(cs.slot('1130'), 120)
        self.assertEqual(cs.slot('1300'), 121)            # 紧接着上午收盘，午休不占位置
        self.assertEqual(cs.slot('1500'), 241)


class RenderTests(unittest.TestCase):
    def svg(self, prices=(10.0, 10.2, 10.1, 10.4), levels=None, **kw):
        return cs.render('sz000001', kw.pop('name', '测试'), minute(prices), levels or {}, **kw)

    def test_is_well_formed_xml(self):
        ET.fromstring(self.svg(levels={'昨收': 10.0, '成本价': 10.1}))

    def test_draws_price_and_vwap_lines(self):
        s = self.svg()
        self.assertEqual(s.count('class="price"'), 1)
        self.assertEqual(s.count('class="vwap"'), 1)

    def test_partial_day_is_not_stretched_to_fill_the_axis(self):
        """还没走到的时段留白，而不是把已有数据拉伸铺满。"""
        pts = re.search(r'class="price" points="([^"]+)"', self.svg(prices=[10.0] * 5)).group(1).split()
        xs = [float(p.split(',')[0]) for p in pts]
        self.assertLess(xs[-1], cs.PAD_L + (cs.W - cs.PAD_L - cs.PAD_R) * 0.05)

    def test_your_own_levels_are_drawn_with_distinct_styles(self):
        s = self.svg(levels={'成本价': 10.1, '你的止损价': 9.9, '你的目标价': 10.5})
        for cls in ('lvl-cost', 'lvl-stop', 'lvl-target'):
            self.assertIn(cls, s)

    def test_far_away_levels_are_listed_not_used_to_squash_the_chart(self):
        s = self.svg(levels={'涨停价': 99.0, '昨收': 10.0})
        self.assertIn('在图外', s)
        self.assertIn('涨停价 99.00', s)
        self.assertNotIn('涨停价 99.00</text>\n<polyline', s)           # 不作为图内刻度线

    def test_names_are_escaped(self):
        s = self.svg(name='<script>alert(1)</script>')
        self.assertNotIn('<script>alert', s)
        self.assertIn('&lt;script&gt;', s)
        ET.fromstring(s)

    def test_single_bar_and_flat_prices_do_not_divide_by_zero(self):
        ET.fromstring(self.svg(prices=[10.0]))
        ET.fromstring(self.svg(prices=[10.0] * 30))

    def test_no_nan_or_inf_in_output(self):
        s = self.svg(prices=[10.0] * 30, levels={'昨收': 10.0})
        self.assertNotRegex(s, r'nan|inf')

    def test_missing_vwap_still_renders(self):
        m = minute([10.0, 10.1])
        for b in m['bars']:
            b['vwap'] = None
        ET.fromstring(cs.render('sz000001', '测试', m, {}))

    def test_the_vwap_line_is_never_hidden_off_the_edge_of_the_chart(self):
        """均价线是日内最重要的多空分界。早先 y 轴范围只用价格算，开盘第一分钟均价线落在范围外，
        整条线没画出来。"""
        m = minute([10.0])
        m['bars'][0]['vwap'] = 10.9                        # 离价格很远
        s = cs.render('sz000001', '测试', m, {})
        pts = re.search(r'class="vwap" points="([^"]+)"', s).group(1).split(',')
        y = float(pts[1])
        self.assertGreaterEqual(y, cs.PAD_T)
        self.assertLessEqual(y, cs.H - cs.PAD_B)

    def test_out_of_range_note_wraps_and_does_not_collide_with_the_time_axis(self):
        levels = {'MA20': 99.0, 'MA60': 98.0, '20日最高收盘': 97.0, '20日最低收盘': 96.0, '涨停价': 95.0, '跌停价': 94.0}
        s = self.svg(levels=levels)
        axis_y = cs.H - cs.PAD_B + 16
        left_texts = [float(y) for y in re.findall(r'<text class="txt" x="58" y="([\d.]+)">', s)]
        note_ys = [y for y in left_texts if y > cs.H - cs.PAD_B]          # 只看图下方那块区域
        self.assertGreaterEqual(len(note_ys), 2)                            # 内容多，确实折行了
        self.assertTrue(all(y > axis_y + 8 for y in note_ys), (note_ys, axis_y))   # 都在时间轴标签下方
        self.assertTrue(all(y < cs.H for y in note_ys))                     # 且没掉出画布

    def test_no_prediction_line_is_ever_drawn(self):
        """图上只画事实：只有价格和均价线两条折线，没有第三条。"""
        self.assertEqual(self.svg(levels={'昨收': 10.0}).count('<polyline'), 2)

    def test_level_labels_do_not_overlap(self):
        s = self.svg(levels={'MA5': 10.201, 'MA20': 10.203, '昨收': 10.202})
        ys = sorted(float(y) for y in re.findall(r'<text class="txt" x="\d+" y="([\d.]+)">(?:MA5|MA20|昨收) ', s))
        self.assertTrue(all(b - a >= 10.9 for a, b in zip(ys, ys[1:])), ys)


if __name__ == '__main__':
    unittest.main()
