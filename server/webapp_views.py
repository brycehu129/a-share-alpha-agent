"""后台新增页面：持仓/自选股管理、盘后分析查看。只用标准库。

从 webapp.py 拆出来单纯是为了别让那个文件继续膨胀；样式令牌(STYLE)和导航(nav)
在这里集中定义，webapp.py 的原配置页也复用同一份，避免两个页面渐渐长歪。

盘后分析跑一次要一到三分钟（AI 调用是大头），所以「立即生成」放到后台线程里跑，
页面用 meta refresh 轮询状态——不然浏览器会一直转圈甚至超时断开，用户还以为挂了。
"""
import html
import re
import threading
from datetime import datetime, timedelta

from collect_quotes import CST

STYLE = """
:root{
  --bg:#f2f5f4; --surface:#ffffff; --surface-2:#e8edec; --border:#d6dedb;
  --ink:#16221d; --ink-2:#48584f; --ink-3:#7c8b83; --accent:#2f5fae; --accent-ink:#fff;
  --status-ready-bg:#dcebf6; --status-ready-fg:#1f4c78;
  --status-wait-bg:#eceef0; --status-wait-fg:#57626b;
  --up:#c0392b; --down:#178a4c;
  --shadow: 0 1px 2px rgba(22,34,29,.06), 0 8px 24px -12px rgba(22,34,29,.18);
}
*{box-sizing:border-box;}
body{font-family:"IBM Plex Sans","PingFang SC","Microsoft YaHei",-apple-system,sans-serif;
  max-width:860px;margin:0 auto;padding:28px 20px 48px;background:var(--bg);color:var(--ink);
  line-height:1.6;}
h1{font-size:24px;font-weight:600;margin:0 0 14px;}
h2{font-size:18px;font-weight:600;margin:26px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--border);}
h3{font-size:15px;font-weight:600;margin:20px 0 6px;}
.nav-pills{display:inline-flex;gap:2px;padding:3px;background:var(--surface-2);border-radius:999px;
  border:1px solid var(--border);margin-bottom:18px;flex-wrap:wrap;}
.nav-pills a{display:inline-flex;align-items:center;padding:5px 14px;border-radius:999px;
  font-size:12.5px;font-weight:600;color:var(--ink-2);text-decoration:none;}
.nav-pills a:hover{color:var(--ink);}
.nav-pills a.active{background:var(--accent);color:var(--accent-ink);box-shadow:var(--shadow);}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:14px;
  padding:18px 18px 20px;box-shadow:var(--shadow);margin-bottom:16px;}
.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;
  font-size:12.5px;font-weight:600;}
.pill.status-ready{background:var(--status-ready-bg);color:var(--status-ready-fg);}
.pill.status-wait{background:var(--status-wait-bg);color:var(--status-wait-fg);}
.notice{font-size:13.5px;color:#0a7d32;background:#e8f5ec;border-radius:8px;padding:8px 12px;margin:0 0 14px;}
.notice.error{color:#8a1c1c;background:#fbeaea;}
label{display:block;font-size:12.5px;font-weight:600;color:var(--ink-2);margin-bottom:6px;}
input[type=text],input[type=number],select{width:100%;padding:9px 10px;font-size:13px;
  border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--ink);}
input:focus,select:focus{outline:2px solid var(--accent);outline-offset:1px;}
button{padding:8px 16px;margin-top:10px;margin-right:8px;cursor:pointer;border:1px solid var(--border);
  border-radius:999px;background:var(--surface);color:var(--ink);font-size:13px;font-weight:600;min-height:36px;}
button:hover{color:var(--accent);border-color:var(--accent);}
button.danger{padding:4px 10px;min-height:0;font-size:12px;margin:0;}
button.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);}
table{width:100%;border-collapse:collapse;font-size:13px;margin:10px 0;}
th,td{padding:7px 8px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top;}
th{font-size:12px;color:var(--ink-2);font-weight:600;background:var(--surface-2);}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;}
.up{color:var(--up);} .down{color:var(--down);}
.report{background:var(--surface);border:1px solid var(--border);border-radius:14px;
  padding:4px 20px 20px;box-shadow:var(--shadow);}
.report blockquote{margin:8px 0;padding:8px 12px;background:var(--surface-2);border-radius:8px;
  font-size:13px;color:var(--ink-2);}
.footer-note{color:var(--ink-3);font-size:11.5px;margin-top:24px;}
.muted{color:var(--ink-3);font-size:12px;}
@media (max-width:600px){ body{padding:16px 12px 40px;} table{font-size:12px;} }
"""

NAV_ITEMS = [('/dashboard', '看板'), ('/sentinel', '哨兵'), ('/proposals', '提议'), ('/postclose', '盘后分析'), ('/book', '持仓与自选'), ('/', '推送配置')]


def nav(active):
    return '<nav class="nav-pills">' + ''.join(
        '<a href="%s"%s>%s</a>' % (href, ' class="active"' if href == active else '', label)
        for href, label in NAV_ITEMS) + '</nav>'


def page(title, active, body, refresh=None):
    meta = '<meta http-equiv="refresh" content="%d">' % refresh if refresh else ''
    return ('<!doctype html>\n<html lang="zh"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '%s<title>%s</title><style>%s</style></head><body>%s%s</body></html>'
            % (meta, html.escape(title), STYLE, nav(active), body))


# --- 极简 Markdown 渲染 -------------------------------------------------
# 只支持盘后报告实际会用到的语法（标题/表格/列表/加粗/斜体）。先整体转义再套格式，
# 所以报告里就算出现 <script> 之类的内容也只会被当成文本显示。

def _inline(text):
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'(?<![\w*])_([^_]+)_(?![\w*])', r'<em>\1</em>', text)
    return text


def markdown_to_html(text):
    out, table, in_list = [], [], False

    def flush_table():
        if not table:
            return
        cells = lambda line: [c.strip() for c in line.strip().strip('|').split('|')]
        separators = [i for i, r in enumerate(table) if re.fullmatch(r'[\s|:-]+', r)]
        # 对齐方式取自 Markdown 的分隔行（`---:` = 右对齐），这是标准语义；
        # 早先按表头文字猜会把"指数"这种以"数"结尾的词误判成数字列。
        aligned = set()
        if separators:
            aligned = {i for i, c in enumerate(cells(table[separators[0]])) if c.endswith(':')}
        header = table[0]
        rows = [r for i, r in enumerate(table) if i and i not in separators]
        cls = lambda i: ' class="num"' if i in aligned else ''
        head = ''.join('<th%s>%s</th>' % (cls(i), _inline(c)) for i, c in enumerate(cells(header)))
        body = ''.join('<tr>' + ''.join('<td%s>%s</td>' % (cls(i), _inline(c))
                                        for i, c in enumerate(cells(r))) + '</tr>'
                       for r in rows)
        out.append('<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>' % (head, body))
        table.clear()

    def flush_list():
        nonlocal in_list
        if in_list:
            out.append('</ul>')
            in_list = False

    for raw in html.escape(text).split('\n'):
        line = raw.rstrip()
        if line.startswith('|'):
            flush_list()
            table.append(line)
            continue
        flush_table()
        if not line:
            flush_list()
            continue
        heading = re.match(r'(#{1,4})\s+(.*)', line)
        if heading:
            flush_list()
            level = min(len(heading.group(1)) + 0, 4)
            out.append('<h%d>%s</h%d>' % (level, _inline(heading.group(2)), level))
            continue
        if line.startswith('- '):
            if not in_list:
                out.append('<ul>')
                in_list = True
            out.append('<li>%s</li>' % _inline(line[2:]))
            continue
        flush_list()
        if line.startswith('⚠') or line.startswith('_'):
            out.append('<blockquote>%s</blockquote>' % _inline(line))
        else:
            out.append('<p>%s</p>' % _inline(line))
    flush_table()
    flush_list()
    return '\n'.join(out)


# --- 持仓 / 自选股管理页 ------------------------------------------------

def _declared(h):
    """把你声明过的信息压成一行：只列出你真的填了的，没填的不显示（也就不会触发对应提醒）。"""
    import portfolio_book
    parts = []
    if h.get('hold_type'):
        parts.append(portfolio_book.HOLD_TYPE_LABEL.get(h['hold_type'], h['hold_type']))
    if h.get('stop_price'):
        parts.append('止损 %s' % h['stop_price'])
    if h.get('target_price'):
        parts.append('目标 %s' % h['target_price'])
    if h.get('t_base_shares'):
        parts.append('做T底仓 %d' % h['t_base_shares'])
    if h.get('note'):
        parts.append(h['note'])
    return ' · '.join(parts)


def render_book_page(holdings, watchlist, quotes=None, message='', error=False):
    import portfolio_book
    quotes = quotes or {}
    note = ('<p class="notice%s">%s</p>' % (' error' if error else '', html.escape(message))
            if message else '')

    rows = []
    total_cost = total_value = 0.0
    for h in holdings:
        q = quotes.get(h['symbol'])
        last = float(q['last']) if q else None
        cost = h['cost_price'] * h['shares']
        total_cost += cost
        value = last * h['shares'] if last else None
        if value:
            total_value += value
        pct = (last / h['cost_price'] - 1) * 100 if last else None
        cls = '' if pct is None else ('up' if pct > 0 else 'down' if pct < 0 else '')
        rows.append(
            '<tr><td><strong>%s</strong><br><span class="muted">%s</span></td>'
            '<td class="num">%d</td><td class="num">%s</td><td class="num">%s</td>'
            '<td class="num %s">%s</td><td>%s</td>'
            '<td><form method="post" action="/book/holding/remove" style="margin:0">'
            '<input type="hidden" name="symbol" value="%s">'
            '<button class="danger" type="submit">删除</button></form></td></tr>'
            % (html.escape(h.get('name') or h['symbol']), h['symbol'], h['shares'],
               h['cost_price'], ('%.2f' % last) if last else '—',
               cls, ('%+.2f%%' % pct) if pct is not None else '—',
               html.escape(_declared(h)), html.escape(h['symbol'])))
    summary = ''
    if total_cost and total_value:
        pnl = total_value - total_cost
        summary = ('<p>持仓总成本 %.2f，当前市值 %.2f，浮动盈亏 <span class="%s">%+.2f（%+.2f%%）</span>。'
                   '<span class="muted">按最近一次实时报价估算，不含费用。</span></p>'
                   % (total_cost, total_value, 'up' if pnl > 0 else 'down', pnl,
                      pnl / total_cost * 100))

    watch_rows = []
    for w in watchlist:
        q = quotes.get(w['symbol'])
        change = float(q['change_pct']) if q else None
        cls = '' if change is None else ('up' if change > 0 else 'down' if change < 0 else '')
        watch_rows.append(
            '<tr><td><strong>%s</strong><br><span class="muted">%s</span></td>'
            '<td>%s</td><td class="num">%s</td><td class="num %s">%s</td><td>%s</td>'
            '<td><form method="post" action="/book/watch/remove" style="margin:0">'
            '<input type="hidden" name="symbol" value="%s">'
            '<button class="danger" type="submit">删除</button></form></td></tr>'
            % (html.escape(w.get('name') or w['symbol']), w['symbol'],
               portfolio_book.INTENT_LABEL.get(w.get('intent'), w.get('intent', '')),
               ('%.2f' % float(q['last'])) if q else '—',
               cls, ('%+.2f%%' % change) if change is not None else '—',
               html.escape(w.get('note') or ''), html.escape(w['symbol'])))

    intent_options = ''.join('<option value="%s">%s</option>' % (k, v)
                             for k, v in portfolio_book.INTENT_LABEL.items())

    body = f"""<h1>持仓与自选股</h1>
{note}
<div class="panel">
  <h2 style="margin-top:0">我的持仓（{len(holdings)}）</h2>
  {summary}
  <table><thead><tr><th>股票</th><th class="num">数量</th><th class="num">成本价</th>
    <th class="num">现价</th><th class="num">浮动</th><th>备注</th><th></th></tr></thead>
    <tbody>{''.join(rows) or '<tr><td colspan="7" class="muted">还没有录入持仓。</td></tr>'}</tbody></table>
  <form method="post" action="/book/holding">
    <div class="grid">
      <div><label>股票代码</label><input type="text" name="symbol" placeholder="600519 或 sh600519" required></div>
      <div><label>名称（可选）</label><input type="text" name="name" placeholder="贵州茅台"></div>
      <div><label>持仓数量（股）</label><input type="number" name="shares" step="100" min="100" required></div>
      <div><label>成本价</label><input type="text" name="cost_price" placeholder="1300.00" required></div>
      <div><label>建仓日期（可选）</label><input type="text" name="opened_on" placeholder="2026-09-01"></div>
      <div><label>备注（可选）</label><input type="text" name="note"></div>
      <div><label>持有类型（可选）</label><select name="hold_type">
        <option value="">不声明</option><option value="short">短线</option><option value="swing">波段</option>
        <option value="long">长期</option><option value="trapped">套牢待解</option></select></div>
      <div><label>止损价（可选）</label><input type="text" name="stop_price" placeholder="不填就不提醒止损"></div>
      <div><label>目标价（可选）</label><input type="text" name="target_price" placeholder="不填就不提醒目标"></div>
      <div><label>做T底仓（股，可选）</label><input type="text" name="t_base_shares" placeholder="0 = 不做T"></div>
    </div>
    <p class="muted">系统不知道你为什么买，所以止损/目标/做T底仓必须由你自己声明；不填的那类提醒就不触发。</p>
    <button class="primary" type="submit">保存持仓</button>
    <span class="muted">同一只股票再次保存即覆盖。</span>
  </form>
</div>

<div class="panel">
  <h2 style="margin-top:0">自选股（{len(watchlist)}）</h2>
  <table><thead><tr><th>股票</th><th>类型</th><th class="num">现价</th>
    <th class="num">涨跌</th><th>备注</th><th></th></tr></thead>
    <tbody>{''.join(watch_rows) or '<tr><td colspan="6" class="muted">还没有自选股。</td></tr>'}</tbody></table>
  <form method="post" action="/book/watch">
    <div class="grid">
      <div><label>股票代码</label><input type="text" name="symbol" placeholder="000001" required></div>
      <div><label>名称（可选）</label><input type="text" name="name"></div>
      <div><label>关注类型</label><select name="intent">{intent_options}</select></div>
      <div><label>备注（可选）</label><input type="text" name="note"></div>
      <div><label>买入区间下沿（可选）</label><input type="text" name="buy_low"></div>
      <div><label>买入区间上沿（可选）</label><input type="text" name="buy_high"></div>
    </div>
    <button class="primary" type="submit">加入自选</button>
  </form>
</div>
<p class="footer-note">持仓数据只保存在这台服务器本地，不会进入公开的 Git 仓库，也不会发送给券商。
系统永远不会自动下单。</p>"""
    return page('持仓与自选股', '/book', body)


# --- 盘后分析页 ---------------------------------------------------------

_run_lock = threading.Lock()
_run_state = {'running': False, 'started_at': None, 'finished_at': None,
              'error': None, 'last_run_id': None}
THROTTLE_SECONDS = 300


def run_state():
    with _run_lock:
        return dict(_run_state)


def start_run(history, run_id=None, push_enabled=True):
    """启动后台生成。返回 (是否已启动, 说明)。

    限流：AI 调用要花钱也要花时间，连点两下按钮没有意义。"""
    import postclose_report
    now = datetime.now(CST)
    with _run_lock:
        if _run_state['running']:
            return False, '已有一次分析正在生成中，请稍候。'
        finished = _run_state['finished_at']
        if finished and now - finished < timedelta(seconds=THROTTLE_SECONDS):
            wait = THROTTLE_SECONDS - int((now - finished).total_seconds())
            return False, '距上次生成不足 %d 秒，请 %d 秒后再试。' % (THROTTLE_SECONDS, wait)
        _run_state.update(running=True, started_at=now, finished_at=None, error=None)

    def worker():
        error = None
        run = run_id or datetime.now(CST).strftime('%Y%m%d%H%M%S') + '-1'
        try:
            report = postclose_report.build(history, run)
            postclose_report.save_report(report)
            if push_enabled and report['status'] != 'empty':
                result = postclose_report.push(report)
                if not result['sent']:
                    error = '报告已生成，但推送未发送：' + str(result.get('reason'))
        except Exception as exc:  # 后台线程：任何异常都要变成页面上能看到的状态
            error = '%s: %s' % (type(exc).__name__, exc)
            run = None
        with _run_lock:
            _run_state.update(running=False, finished_at=datetime.now(CST),
                              error=error, last_run_id=run)

    threading.Thread(target=worker, daemon=True).start()
    return True, '已开始生成，约需 1–3 分钟。'


def render_postclose_page(report, state, message='', error=False):
    note = ('<p class="notice%s">%s</p>' % (' error' if error else '', html.escape(message))
            if message else '')
    refresh = 15 if state['running'] else None
    if state['running']:
        status = ('<span class="pill status-wait">正在生成…</span> '
                  '<span class="muted">开始于 %s，本页每 15 秒自动刷新。</span>'
                  % state['started_at'].strftime('%H:%M:%S'))
    elif state['error']:
        status = '<span class="pill status-wait">上次生成有问题</span> <span class="muted">%s</span>' \
                 % html.escape(state['error'])
    else:
        status = '<span class="pill status-ready">空闲</span>'

    if report is None:
        content = ('<div class="panel"><p class="muted">还没有生成过盘后分析。'
                   '点上面的按钮生成一份，或者等工作日 16:30 的定时任务。</p></div>')
    else:
        meta = report.get('ai_meta') or {}
        cost = ''
        if meta.get('status') == 'ok':
            cost = ('<span class="muted">%s · 输入 %s / 输出 %s tokens</span>'
                    % (meta.get('model'), meta.get('input_tokens'), meta.get('output_tokens')))
        content = ('<div class="report">%s</div><p class="footer-note">报告编号 %s %s</p>'
                   % (markdown_to_html(_report_markdown(report)),
                      html.escape(report['id']), cost))

    body = f"""<h1>盘后分析</h1>
{note}
<div class="panel">
  <p>{status}</p>
  <form method="post" action="/postclose/run">
    <button class="primary" type="submit">立即生成</button>
    <button type="submit" name="no_push" value="1">生成但不推送</button>
  </form>
  <p class="muted">定时任务：工作日 16:30（北京时间）自动生成并推送到企业微信。</p>
</div>
{content}"""
    return page('盘后分析', '/postclose', body, refresh=refresh)


def _report_markdown(report):
    import postclose_report
    return postclose_report.render(report)


# --- 策略参数提议页 -----------------------------------------------------------

PROPOSAL_TRACK_LABEL = {'breakout': '突破', 'pullback': '回调反弹'}


def _fmt_param(name, value):
    if value is None:
        return '—'
    if name == 'exit.hold_sessions':
        return '%d 个交易日' % value
    if name.endswith('_mult'):
        return '%.2f×' % value
    if name == 'exit.target_r':
        return 'R=%.2f' % value
    if name == 'exit.breakeven_arm_frac':
        return '止盈幅度的 %.0f%%' % (value * 100)
    return '%.2f%%' % (value * 100)


def render_proposals_page(store, message='', error=False):
    """store 来自 proposals.load()。所有提议方给的文字（理由、证据摘要）都当不可信文本转义。"""
    import exec_spec
    import proposals as pr
    rev, _ = pr.effective(store)
    note = ('<p class="notice%s">%s</p>' % (' error' if error else '', html.escape(message)) if message else '')

    spec_rows = []
    for track in ('breakout', 'pullback'):
        current = pr.current_template(store, track)
        base = exec_spec.SPECS[track]
        for name, d in exec_spec.TUNABLE_PARAMS.items():
            if track not in d['tracks']:
                continue
            value = exec_spec.get_param(current, name)
            if value is None:
                continue
            changed = value != exec_spec.get_param(base, name)
            spec_rows.append(
                '<tr><td>%s</td><td>%s</td><td class="num"><strong>%s</strong>%s</td><td class="num muted">%s ~ %s</td>'
                '<td class="num muted">±%s</td></tr>'
                % (PROPOSAL_TRACK_LABEL[track], html.escape(d['label']), _fmt_param(name, value),
                   ' <span class="muted">（原 %s）</span>' % _fmt_param(name, exec_spec.get_param(base, name)) if changed else '',
                   _fmt_param(name, d['floor']), _fmt_param(name, d['ceiling']), _fmt_param(name, d['step_cap'])))
    breakevens = ' · '.join('%s %.1f%%' % (PROPOSAL_TRACK_LABEL[t], exec_spec.breakeven_win_rate(pr.current_spec(store, t)['exit']))
                            for t in ('breakout', 'pullback'))

    pending = [p for p in store['proposals'] if p['status'] == pr.PENDING]
    cards = []
    for p in reversed(pending):
        ev = p['evidence'] or {}
        eff = pr.preview(store, p)
        cards.append(
            '<div class="panel"><p><strong>%s</strong> · %s · %s：<strong>%s → %s</strong> '
            '<span class="muted">（%s，%s 提交）</span></p>'
            '<p class="muted">盈亏平衡胜率 %.1f%% → %.1f%%（按典型 ATR 估算；越低，需要的胜率越低）</p>'
            '<p>提议方的理由（<em>未经验证的陈述，不是结论</em>）：%s</p>'
            '<p class="muted">证据：n=%s，日期组=%s，来自执行版本 %s。%s</p>'
            '<form method="post" action="/proposals/approve" style="display:inline">'
            '<input type="hidden" name="id" value="%s"><input type="text" name="note" placeholder="备注（可选）">'
            '<button type="submit">批准</button></form> '
            '<form method="post" action="/proposals/reject" style="display:inline">'
            '<input type="hidden" name="id" value="%s"><button class="danger" type="submit">驳回</button></form></div>'
            % (html.escape(p['id']), PROPOSAL_TRACK_LABEL.get(p['track'], html.escape(p['track'])),
               html.escape(exec_spec.TUNABLE_PARAMS[p['parameter']]['label']),
               _fmt_param(p['parameter'], p['old']), _fmt_param(p['parameter'], p['new']),
               html.escape(p['source']), html.escape(p['created_at'][:16].replace('T', ' ')),
               eff['breakeven_before'], eff['breakeven_after'], html.escape(p['rationale'] or '（未填）'),
               html.escape(str(ev.get('n'))), html.escape(str(ev.get('cohorts'))),
               html.escape(str(ev.get('execution_version'))), html.escape(ev.get('summary') or ''),
               html.escape(p['id']), html.escape(p['id'])))
    if not cards:
        cards = ['<div class="panel"><p class="muted">没有待确认的提议。参数样本要满足 n≥30、日期组≥15 才有资格被提议；'
                 '前瞻验收要等计划冻结后走完最长持有期，预计数周后才会攒够。</p></div>']

    done = [p for p in reversed(store['proposals']) if p['status'] != pr.PENDING][:30]
    history = ''.join(
        '<tr><td>%s</td><td>%s</td><td>%s.%s</td><td class="num">%s → %s</td><td>%s</td><td class="muted">%s</td></tr>'
        % (html.escape(p['id']), pr.STATUS_LABEL.get(p['status'], html.escape(p['status'])),
           html.escape(p['track']), html.escape(p['parameter']), html.escape(str(p['old'])), html.escape(str(p['new'])),
           html.escape(p['source']), html.escape(p.get('reason') or p.get('decision_note') or ''))
        for p in done) or '<tr><td colspan="6" class="muted">暂无</td></tr>'
    revisions = ''.join(
        '<tr><td>r%d</td><td>%s</td><td>%s.%s</td><td class="num">%s → %s</td><td class="muted">%s</td></tr>'
        % (r['revision'], html.escape(r['approved_at'][:16].replace('T', ' ')), html.escape(r['track']),
           html.escape(r['parameter']), html.escape(str(r['old'])), html.escape(str(r['new'])),
           html.escape(r.get('note') or ''))
        for r in reversed(store['revisions'])) or '<tr><td colspan="5" class="muted">尚无修订：使用 exec-0.3 原始规格</td></tr>'

    body = (
        '<h1>策略参数提议</h1>' + note +
        '<p class="muted">AI 或规则只能在这里<strong>提议</strong>；你批准后才生效。风控红线（本金、持仓数、单只上限、单笔风险、回撤线、成本假设）'
        '不可提议，代码直接拒收。批准后从下一次盘前选股（工作日 08:40）起新冻结的计划用新规格，执行版本变为 %s；'
        '已冻结的计划和已有验收记录不变，新旧样本分开统计。</p>'
        '<div class="panel"><h2>当前生效规格 · 修订 %d（%s）</h2><table><tr><th>track</th><th>参数</th><th>当前值</th>'
        '<th>允许范围</th><th>单次步长</th></tr>%s</table><p class="muted">盈亏平衡胜率（按典型 ATR %.1f%% 估算，每只股票的实际止损/止盈随自己的 ATR 而变）：%s</p></div>'
        '<h2>待确认（%d）</h2>%s'
        '<div class="panel"><h2>处理记录</h2><table><tr><th>编号</th><th>状态</th><th>参数</th><th>改动</th><th>来源</th><th>说明</th></tr>%s</table></div>'
        '<div class="panel"><h2>已批准的修订</h2><table><tr><th>修订</th><th>时间</th><th>参数</th><th>改动</th><th>备注</th></tr>%s</table></div>'
        % (html.escape(exec_spec.execution_version(rev + 1)), rev, html.escape(exec_spec.execution_version(rev)),
           ''.join(spec_rows), exec_spec.NOMINAL_ATR_PCT * 100, breakevens, len(pending), ''.join(cards), history, revisions))
    return page('策略参数提议', '/proposals', body)
