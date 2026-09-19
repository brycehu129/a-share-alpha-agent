"""分时图 SVG。标准库生成，不引绘图依赖。

**图上只画事实，不画预测线。** 今日已发生的分时走势、均价线（VWAP）、昨收/今开/日内高低、
均线、20日高低点、涨跌停价，以及**你自己的**成本价和止损位。AI 的情景价位不画在图上——
预测线的说服力远超它的信息量，画上去你会信它。AI 挂了这张图仍然完整有用。

横轴是固定的交易时段（09:30–11:30、13:00–15:00 共 242 分钟，午休不占位置），所以
上午盘中看到的图和收盘后看到的图对得上；还没走到的时段留白，而不是把已有数据拉伸铺满。
"""
import html

W, H = 800, 380
# 右边距要放得下"20日最低收盘 1246.52"这种中文标签加价格（约 140px），否则会被画布边缘截断。
PAD_L, PAD_R, PAD_T, PAD_B = 58, 150, 40, 64
SLOTS = 242


def slot(hhmm):
    h, m = int(hhmm[:2]), int(hhmm[2:])
    minutes = h * 60 + m
    if minutes <= 11 * 60 + 30:
        return minutes - (9 * 60 + 30)
    return 121 + (minutes - 13 * 60)


LEVEL_STYLE = {
    '昨收': ('lvl-neutral', '4 3'), '今开': ('lvl-neutral', '2 3'), '日内最高': ('lvl-neutral', '1 3'),
    '日内最低': ('lvl-neutral', '1 3'), 'MA5': ('lvl-ma', '5 3'), 'MA20': ('lvl-ma', '5 3'), 'MA60': ('lvl-ma', '5 3'),
    '20日最高收盘': ('lvl-neutral', '1 4'), '20日最低收盘': ('lvl-neutral', '1 4'),
    '涨停价': ('lvl-limit', '6 3'), '跌停价': ('lvl-limit', '6 3'),
    '成本价': ('lvl-cost', '7 3'), '你的止损价': ('lvl-stop', '7 3'), '你的目标价': ('lvl-target', '7 3'),
}

CSS = """
.bg{fill:#fff}.frame{stroke:#d6dedb;fill:none}.txt{fill:#48584f;font:11px system-ui,sans-serif}
.title{fill:#16221d;font:600 13px system-ui,sans-serif}.price{stroke:#2f5fae;fill:none;stroke-width:1.6}
.vwap{stroke:#c98a12;fill:none;stroke-width:1.3}.dot{fill:#2f5fae}
.lvl-neutral{stroke:#8a9891}.lvl-ma{stroke:#7b5ea7}.lvl-limit{stroke:#b9b9b9}
.lvl-cost{stroke:#1f4c78;stroke-width:1.6}.lvl-stop{stroke:#c0392b;stroke-width:1.6}.lvl-target{stroke:#178a4c;stroke-width:1.6}
.lvl{fill:none;stroke-width:1}.grid{stroke:#eef2f0}
@media (prefers-color-scheme:dark){.bg{fill:#151b18}.frame{stroke:#33403a}.txt{fill:#b6c3bc}.title{fill:#e8eeeb}
.grid{stroke:#232d28}.price{stroke:#6ea0e6}.dot{fill:#6ea0e6}.vwap{stroke:#e3b04b}.lvl-limit{stroke:#66716b}}
"""


def render(symbol, name, minute, levels, last=None, subtitle=''):
    """minute: minute_data 的解析结果；levels: {名称: 价格}。返回 SVG 字符串。"""
    bars = minute['bars']
    prices = [b['price'] for b in bars]
    vwaps = [(b['t'], b['vwap']) for b in bars if b.get('vwap')]
    last = last if last is not None else prices[-1]
    # 均价线也参与 y 轴范围：它是日内最重要的多空分界，不能因为它离价格远就被当成"图外"藏起来
    # （早先只用价格算范围，开盘第一分钟均价线落在范围外，整条线没画出来）。
    lo, hi = min(prices + [v for _, v in vwaps]), max(prices + [v for _, v in vwaps])
    span = max(hi - lo, hi * 0.005)
    y_lo, y_hi = lo - span * 0.5, hi + span * 0.5
    shown, hidden = {}, {}
    for k, v in levels.items():
        (shown if y_lo <= v <= y_hi else hidden)[k] = v
    if shown:
        y_lo, y_hi = min(y_lo, min(shown.values()) - span * 0.05), max(y_hi, max(shown.values()) + span * 0.05)

    pw, ph = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    X = lambda s: PAD_L + pw * s / (SLOTS - 1)
    Y = lambda p: PAD_T + ph * (1 - (p - y_lo) / (y_hi - y_lo))
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="100%%" role="img" '
           'aria-label="%s 分时图">' % (W, H, html.escape('%s %s' % (name, symbol), quote=True)),
           '<style>%s</style>' % CSS, '<rect class="bg" width="%d" height="%d"/>' % (W, H),
           '<text class="title" x="%d" y="17">%s %s</text>' % (PAD_L, html.escape(name or symbol), html.escape(symbol)),
           '<text class="txt" x="%d" y="17" text-anchor="end">%s</text>' % (W - PAD_R, html.escape(subtitle))]

    for i in range(5):                                            # 横向网格 + 价格刻度
        p = y_lo + (y_hi - y_lo) * i / 4
        out.append('<line class="grid" x1="%d" x2="%d" y1="%.1f" y2="%.1f"/>' % (PAD_L, W - PAD_R, Y(p), Y(p)))
        out.append('<text class="txt" x="%d" y="%.1f" text-anchor="end">%.2f</text>' % (PAD_L - 6, Y(p) + 4, p))
    for hhmm, label in (('0930', '09:30'), ('1030', '10:30'), ('1130', '11:30'), ('1400', '14:00'), ('1500', '15:00')):
        out.append('<text class="txt" x="%.1f" y="%d" text-anchor="middle">%s</text>' % (X(slot(hhmm)), H - PAD_B + 16, label))
    out.append('<line class="grid" x1="%.1f" x2="%.1f" y1="%d" y2="%d"/>' % (X(121), X(121), PAD_T, H - PAD_B))
    out.append('<rect class="frame" x="%d" y="%d" width="%d" height="%d"/>' % (PAD_L, PAD_T, pw, ph))

    used = []
    for name_, price in sorted(shown.items(), key=lambda kv: -kv[1]):
        cls, dash = LEVEL_STYLE.get(name_, ('lvl-neutral', '3 3'))
        y = Y(price)
        out.append('<line class="lvl %s" stroke-dasharray="%s" x1="%d" x2="%d" y1="%.1f" y2="%.1f"/>'
                   % (cls, dash, PAD_L, W - PAD_R, y, y))
        while any(abs(y - u) < 11 for u in used):                 # 标签避让，别叠在一起
            y += 11
        used.append(y)
        out.append('<text class="txt" x="%d" y="%.1f">%s %.2f</text>' % (W - PAD_R + 4, y + 4, html.escape(name_), price))

    pts = ' '.join('%.1f,%.1f' % (X(slot(b['t'])), Y(b['price'])) for b in bars)
    out.append('<polyline class="price" points="%s"/>' % pts)
    if vwaps:
        out.append('<polyline class="vwap" points="%s"/>' % ' '.join('%.1f,%.1f' % (X(slot(t)), Y(v)) for t, v in vwaps))
        out.append('<text class="txt" x="%d" y="%d">— 价格　<tspan fill="#c98a12">— 均价线</tspan></text>' % (PAD_L, PAD_T - 6))
    out.append('<circle class="dot" cx="%.1f" cy="%.1f" r="3"/>' % (X(slot(bars[-1]['t'])), Y(last)))
    if hidden:
        # 图外的价位列在图下方，自动折行（每行约 48 个字符）而不是一行写到被右边界截断。
        line, lines = '在图外：', []
        for k, v in hidden.items():
            piece = '%s %.2f　' % (k, v)
            if len(line) + len(piece) > 48:
                lines.append(line)
                line = ''
            line += piece
        lines.append(line)
        for i, text in enumerate(lines[:3]):
            out.append('<text class="txt" x="%d" y="%d">%s</text>' % (PAD_L, H - PAD_B + 34 + i * 13, html.escape(text.rstrip())))
    out.append('</svg>')
    return '\n'.join(out)
