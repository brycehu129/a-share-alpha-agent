"""策略参数的"提议 + 人确认"队列。Python 3.9+，只用标准库。

**谁能做什么**

- 任何人/任何程序（将来包括 AI）都只能 `submit()` 一条**提议**——写进队列，不产生任何效果。
- 只有人在 /proposals 页面点"批准"（`approve()`）才会生效：追加一条**修订**，之后新冻结的
  计划带新规格、执行版本号变成 exec-0.2.rN。已冻结的计划、已有的验收记录一概不动。
- 风控红线和一切不在白名单（exec_spec.TUNABLE_PARAMS）里的名字，由**代码**拒收并留档，
  不靠提示词约束。被拒的尝试也会记下来——AI 试图碰红线本身就是值得你看到的信号。

**提议要过的关（全部由代码检查，approve() 时会对当前状态再查一遍）**

1. 参数在白名单内，且 track 存在；新值在绝对边界内；单次改动 ≤ step_cap；确实有变化。
2. 提议里写的旧值必须等于当前生效值——别的修订已经改过它就作废（stale），不能拿过期的比较批准。
3. 证据门槛：n ≥ 30、日期组 ≥ 15（与 review_pipeline.GATE、alpha_model.estimate 同一条线），
   且证据必须来自**当前**执行版本——改了参数之后，旧版本的样本不能为新版本背书。
   不达标的提议记为 insufficient，不可批准。
4. 调整后的规格必须仍然自洽（exec_spec.check_spec：如止盈必须大于止损）。

**存储**：`server/data/private/proposals.json`（0600、原子写、不进 git）。提议和修订在同一个
文件里一次写入，所以"批准"不会出现改了一半的状态。生效的参数**由修订记录重放得出**，没有第二份
可以互相矛盾的"当前值"。

**库代码不读这个文件**：alpha_engine.run() 只接收传进去的修订，由 `alpha_engine.__main__`
读取后传入。原因同 llm_settings：cron 先跑全量测试，库若自己读文件，服务器上一批准提议，
"没有修订"的测试就会变样。
"""
import contextlib
import fcntl
import json
import os
import tempfile
import threading
from datetime import datetime

import exec_spec
import portfolio_book
from collect_quotes import CST

MAX_RECORDS = 300
GATE = {'min_n': 30, 'min_cohorts': 15}      # 与 review_pipeline.GATE 相同；单独写一份是为了本模块不依赖它
RATIONALE_MAX = 500
_lock = threading.Lock()

PENDING, APPROVED, REJECTED, REFUSED, INSUFFICIENT, STALE = (
    'pending', 'approved', 'rejected', 'refused', 'insufficient', 'stale')
STATUS_LABEL = {PENDING: '待确认', APPROVED: '已批准', REJECTED: '已驳回', REFUSED: '被代码拒收',
                INSUFFICIENT: '证据不足', STALE: '已过期'}


class ProposalError(ValueError):
    """操作不合法（批准一条不存在/已处理/已过期/证据不足的提议等）。"""


def path(directory=None):
    return os.path.join(directory or portfolio_book.default_dir(), 'proposals.json')


@contextlib.contextmanager
def _txn(directory=None):
    """读-改-写整段互斥：线程锁 + 文件锁。提议由别的进程（将来的 AI 分析）提交，批准在 webapp 里，
    两边同时写的话没有文件锁就会丢更新。"""
    p = path(directory)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with _lock, open(p + '.lock', 'a') as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _empty():
    return {'seq': 0, 'proposals': [], 'revisions': []}


def load(directory=None):
    p = path(directory)
    if not os.path.exists(p):
        return _empty()
    try:
        with open(p, encoding='utf-8') as f:
            store = json.load(f)
        if not (isinstance(store, dict) and isinstance(store.get('proposals'), list)
                and isinstance(store.get('revisions'), list)):
            raise ValueError('shape')
    except (OSError, ValueError) as exc:
        # 读不了就停：这里静默当成"没有修订"，会让已批准的参数悄悄失效。
        raise ProposalError('提议存储文件无法读取（可能已损坏）：%s；未做任何修改' % type(exc).__name__) from exc
    store.setdefault('seq', len(store['proposals']))
    return store


def _save(store, directory=None):
    p = path(directory)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if len(store['proposals']) > MAX_RECORDS:
        # 只清理已终结的旧记录；待确认的和已批准的（修订引用它们）永远保留。
        keep = [x for x in store['proposals'] if x['status'] in (PENDING, APPROVED)]
        rest = [x for x in store['proposals'] if x['status'] not in (PENDING, APPROVED)]
        store['proposals'] = keep + rest[-max(0, MAX_RECORDS - len(keep)):]
        store['proposals'].sort(key=lambda x: x['seq'])
    handle, tmp = tempfile.mkstemp(dir=os.path.dirname(p), suffix='.tmp')
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as f:
            json.dump(store, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    os.chmod(p, 0o600)


# --- 当前生效的参数 -----------------------------------------------------------

def effective(store):
    """由修订记录重放得出 (修订号, {track: {参数名: 值}})。"""
    overrides = {}
    for rev in store['revisions']:
        overrides.setdefault(rev['track'], {})[rev['parameter']] = rev['new']
    return len(store['revisions']), overrides


def active(directory=None):
    """程序入口用：返回 (修订号, 覆盖表)。读不了存储就抛 ProposalError，由入口决定是否中止。"""
    return effective(load(directory))


def current_spec(store, track):
    """当前生效规格（按典型 ATR 解析，用来预览；真正的计划用各自股票的 ATR）。"""
    rev, overrides = effective(store)
    return exec_spec.build_spec(track, overrides.get(track), rev)


def current_template(store, track):
    """当前生效的规格模板（未解析：倍数、上下限等参数的真实当前值）。"""
    import copy
    _, overrides = effective(store)
    return exec_spec.apply_overrides(copy.deepcopy(exec_spec.SPECS[track]), overrides.get(track))


# --- 校验 --------------------------------------------------------------------

def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float('inf'), float('-inf')):
        return None
    return float(value)


def check_change(store, track, parameter, new):
    """返回 (问题说明或 None, 当前值)。红线/白名单/适用 track/边界/步长/自洽性。"""
    if parameter in exec_spec.RED_LINES:
        return '“%s”是风控红线，任何提议都不能改。' % parameter, None
    spec_def = exec_spec.TUNABLE_PARAMS.get(parameter)
    if spec_def is None:
        return '“%s”不在可调参数白名单内。' % parameter, None
    if track not in exec_spec.SPECS:
        return '未知 track：%s。' % track, None
    if track not in spec_def['tracks']:
        return '该参数在 %s track 上没有定义（不适用）。' % track, None
    current = exec_spec.get_param(current_template(store, track), parameter)
    if current is None:
        return '该参数在 %s track 上没有定义（不适用）。' % track, None
    value = _num(new)
    if value is None:
        return '新值必须是有限数字。', current
    if spec_def.get('integer') and value != int(value):
        return '该参数必须是整数。', current
    if not spec_def['floor'] <= value <= spec_def['ceiling']:
        return '新值 %s 超出绝对边界 [%s, %s]。' % (value, spec_def['floor'], spec_def['ceiling']), current
    if abs(value - current) < 1e-12:
        return '新值和当前值相同，不构成调整。', current
    if abs(value - current) > spec_def['step_cap'] + 1e-12:
        return '单次改动 %.4g 超过步长上限 %s。' % (abs(value - current), spec_def['step_cap']), current
    rev, overrides = effective(store)
    merged = dict(overrides.get(track, {}))
    merged[parameter] = int(value) if spec_def.get('integer') else value
    problems = exec_spec.check_spec(exec_spec.build_spec(track, merged, rev))
    if problems:
        return '调整后规格不自洽：' + '；'.join(problems), current
    return None, current


def check_evidence(evidence, required_version):
    """返回问题说明或 None。"""
    if not isinstance(evidence, dict):
        return '没有证据。'
    n, cohorts = _num(evidence.get('n')), _num(evidence.get('cohorts'))
    if n is None or cohorts is None:
        return '证据缺少样本数或日期组数。'
    if n < GATE['min_n'] or cohorts < GATE['min_cohorts']:
        return '样本不足：n=%d（需要≥%d），日期组=%d（需要≥%d）。' % (
            n, GATE['min_n'], cohorts, GATE['min_cohorts'])
    if evidence.get('execution_version') != required_version:
        return '证据来自执行版本 %s，而当前是 %s——旧版本的样本不能为新版本背书。' % (
            evidence.get('execution_version'), required_version)
    return None


def preview(store, proposal):
    """批准前给人看的效果：盈亏平衡胜率的变化（按典型 ATR 估算；每只股票的实际止损/止盈随自己的 ATR 而变）。"""
    rev, overrides = effective(store)
    merged = dict(overrides.get(proposal['track'], {}))
    merged[proposal['parameter']] = proposal['new']
    before = exec_spec.build_spec(proposal['track'], overrides.get(proposal['track']), rev)
    after = exec_spec.build_spec(proposal['track'], merged, rev)
    return {'breakeven_before': round(exec_spec.breakeven_win_rate(before['exit']), 1),
            'breakeven_after': round(exec_spec.breakeven_win_rate(after['exit']), 1)}


# --- 提交 / 批准 / 驳回 ---------------------------------------------------------

def _normalize(parameter, value):
    number = _num(value)
    if number is None:
        return None
    return int(number) if exec_spec.TUNABLE_PARAMS.get(parameter, {}).get('integer') and number == int(number) else number


def _clean_text(value, limit):
    return ' '.join(str(value or '').split())[:limit]


def _clean_evidence(evidence):
    if not isinstance(evidence, dict):
        return {}
    out = {k: evidence.get(k) for k in ('n', 'cohorts', 'execution_version') if k in evidence}
    out['summary'] = _clean_text(evidence.get('summary'), 300)
    return out


def submit(track, parameter, new, rationale, evidence, source='ai', directory=None, now=None):
    """登记一条提议，返回记录。**不会改变任何生效参数。**

    不合规的（红线/白名单/边界/步长）不抛异常，而是以 refused 记下来——被拒的尝试也是审计证据。
    同一个 (track, 参数) 的旧待确认提议自动作废，队列里同一个旋钮最多一条待确认。"""
    now = now or datetime.now(CST)
    with _txn(directory):
        store = load(directory)
        store['seq'] += 1
        rev, _ = effective(store)
        problem, current = check_change(store, track, parameter, new)
        status, reason = PENDING, None
        if problem:
            status, reason = REFUSED, problem
        else:
            required = exec_spec.execution_version(rev)
            reason = check_evidence(evidence, required)
            if reason:
                status = INSUFFICIENT
        record = {'id': 'p%04d' % store['seq'], 'seq': store['seq'], 'created_at': now.isoformat(),
                  'source': _clean_text(source, 20), 'track': _clean_text(track, 20),
                  'parameter': _clean_text(parameter, 40), 'old': current, 'new': _normalize(parameter, new),
                  'rationale': _clean_text(rationale, RATIONALE_MAX), 'evidence': _clean_evidence(evidence),
                  'base_revision': rev, 'status': status, 'reason': reason}
        if status == PENDING:
            for other in store['proposals']:
                if (other['status'] == PENDING and other['track'] == record['track']
                        and other['parameter'] == record['parameter']):
                    other.update(status=STALE, reason='被 %s 取代' % record['id'])
        store['proposals'].append(record)
        _save(store, directory)
        return record


def _find(store, proposal_id):
    for p in store['proposals']:
        if p['id'] == proposal_id:
            return p
    raise ProposalError('找不到提议 %s' % proposal_id)


def approve(proposal_id, note='', directory=None, now=None):
    """人工批准：重新用**当前**状态把所有关再过一遍，通过才追加修订。返回修订记录。"""
    now = now or datetime.now(CST)
    with _txn(directory):
        store = load(directory)
        p = _find(store, proposal_id)
        if p['status'] != PENDING:
            raise ProposalError('提议 %s 当前状态是“%s”，不能批准。' % (p['id'], STATUS_LABEL.get(p['status'], p['status'])))
        rev, _ = effective(store)
        if p['base_revision'] != rev:
            p.update(status=STALE, reason='提议基于修订 %d，而当前是 %d，参数环境已变。' % (p['base_revision'], rev))
            _save(store, directory)
            raise ProposalError('提议基于旧修订，已作废；请让它重新提交。')
        problem, current = check_change(store, p['track'], p['parameter'], p['new'])
        failed = REFUSED if problem else None
        if not problem and current != p['old']:
            problem, failed = '当前生效值已是 %s，与提议里的旧值 %s 不符。' % (current, p['old']), STALE
        if not problem:
            problem = check_evidence(p['evidence'], exec_spec.execution_version(rev))
            failed = INSUFFICIENT if problem else None
        if problem:
            p.update(status=failed, reason=problem)
            _save(store, directory)
            raise ProposalError('批准前复核未通过：' + problem)
        revision = {'revision': rev + 1, 'proposal_id': p['id'], 'approved_at': now.isoformat(),
                    'track': p['track'], 'parameter': p['parameter'], 'old': p['old'], 'new': p['new'],
                    'note': _clean_text(note, 200)}
        store['revisions'].append(revision)
        p.update(status=APPROVED, decided_at=now.isoformat(), decision_note=_clean_text(note, 200),
                 applied_revision=rev + 1)
        _save(store, directory)
        return revision


def reject(proposal_id, note='', directory=None, now=None):
    now = now or datetime.now(CST)
    with _txn(directory):
        store = load(directory)
        p = _find(store, proposal_id)
        if p['status'] != PENDING:
            raise ProposalError('提议 %s 当前状态是“%s”，不能驳回。' % (p['id'], STATUS_LABEL.get(p['status'], p['status'])))
        p.update(status=REJECTED, decided_at=now.isoformat(), decision_note=_clean_text(note, 200))
        _save(store, directory)
        return p


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='查看策略参数提议队列（批准/驳回只能在后台页面 /proposals 上做）')
    ap.add_argument('command', choices=['list', 'active'])
    a = ap.parse_args(argv)
    store = load()
    if a.command == 'active':
        rev, overrides = effective(store)
        print('修订号 %d（执行版本 %s）' % (rev, exec_spec.execution_version(rev)))
        print(json.dumps(overrides, ensure_ascii=False, indent=1) if overrides else '无覆盖：使用 exec-0.2 原始规格')
        return 0
    for p in store['proposals'][-30:]:
        print('%s [%s] %s.%s: %s → %s  %s' % (p['id'], STATUS_LABEL[p['status']], p['track'], p['parameter'],
                                            p['old'], p['new'], p.get('reason') or ''))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
