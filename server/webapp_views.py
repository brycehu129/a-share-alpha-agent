"""盘后分析的后台支撑：极简 Markdown → HTML 渲染，以及「立即生成」的后台线程状态。只用标准库。

页面本身已迁到前端（web/，Vue 单页应用），这里不再有 HTML 页面/样式/导航；
接口在 api_pages.py（/api/postclose）。

盘后分析跑一次要一到三分钟（AI 调用是大头），所以「立即生成」放到后台线程里跑，
前端在「运行中」时轮询 /api/postclose——不然浏览器会一直转圈甚至超时断开，用户还以为挂了。
"""
import html
import re
import threading
from datetime import datetime, timedelta

from collect_quotes import CST

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


# --- 「立即生成」的后台线程 ---------------------------------------------

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
