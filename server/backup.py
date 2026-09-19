"""私有数据的备份与恢复。Python 3.9+，只用标准库。

`server/data/private/` 里有**只此一份**的东西：持仓与自选、exec-0.2 虚拟账本、情景账本与对账、
提议队列与修订、盘后报告。它们不进 git（仓库公开、且盘中每分钟在变），所以机器出问题就全没了。

**两层保护，各防各的事，别混为一谈：**

1. **本机每日快照**（systemd 定时器，`alpha-shadow-backup.timer`）：`backups/` 下滚动保留最近
   14 份 tar.gz。防的是**损坏和误操作**——多个进程都在写这些文件，一个 bug、一次误删、一个写坏
   的账本，都能从昨天的快照找回来。**防不了机器本身丢失**（快照和数据在同一台机器上）。
2. **手动下载**（后台"推送配置"页的"下载备份"按钮）：把同样的归档下载到**你自己的电脑**，
   这才是异地备份。我没有任何别处的凭据可以替你自动异地存，所以这一步要你自己隔一段时间点一下。

**不备份凭据**：OpenRouter key（`llm_settings.json`）和企业微信 webhook（`webapp_config.json`
在私有目录之外）都不进归档——归档可能被下载到你的电脑上，凭据泄露的代价比"丢了重新填"大得多。
恢复后需要重新填这两样。

每份归档带一个 manifest（每个文件的 sha256 和大小）；**做完立刻回读校验**，校验不过就不算成功、
不清理旧快照。快照读文件是"一次读进内存、哈希和写入用同一份字节"，所以哈希一定等于归档里的内容，
即使文件正好在被别的进程替换（它们都是原子替换，读到的要么是旧的要么是新的完整文件）。

恢复（`restore`）默认**拒绝覆盖**已存在且内容不同的文件；解包时逐个成员检查路径（不允许绝对路径、
`..`、链接和非普通文件），归档来自哪里都不能借此写到目标目录之外。
"""
import argparse
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

import portfolio_book
from collect_quotes import CST

KEEP = 14
MANIFEST = 'manifest.json'
NAME_RE = re.compile(r'alpha-shadow-private-\d{8}-\d{6}\.tar\.gz')
STATUS_FILE = 'status.json'
STALE_HOURS = 36                    # 每天一份；超过一天半没有新的成功快照就该报警
MAX_DOWNLOAD_BYTES = 100_000_000
EXCLUDE_NAMES = {'llm_settings.json', 'webapp_config.json'}
EXCLUDE_SUFFIXES = ('.tmp', '.lock')


class BackupError(RuntimeError):
    pass


def root_dir():
    return Path(portfolio_book.default_dir())


def backup_dir():
    return Path(os.environ.get('BACKUP_DIR') or (root_dir().parent / 'backups'))


def collect(root):
    """归档里应该有的文件：[(相对路径, 绝对路径)]。不含凭据、临时文件、锁、符号链接，
    也不含备份目录自己（万一它被配在私有目录里面）。"""
    root = Path(root)
    skip = backup_dir().resolve()
    found = []
    if not root.is_dir():
        return found
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not path.is_file():
            continue
        if path.name in EXCLUDE_NAMES or path.name.endswith(EXCLUDE_SUFFIXES):
            continue
        if skip == path.resolve() or skip in path.resolve().parents:
            continue
        found.append((path.relative_to(root).as_posix(), path))
    return found


def total_size(root):
    total = 0
    for _, path in collect(root):
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def build_archive(root, out, now=None):
    """把私有目录写成 tar.gz 到文件对象 out，返回 manifest。manifest 是第一个成员。"""
    now = now or datetime.now(CST)
    entries = []
    blobs = []
    for rel, path in collect(root):
        try:
            data = path.read_bytes()
        except OSError:
            continue                              # 读的瞬间被删了：不算这份快照的内容，也不该让整份失败
        entries.append({'path': rel, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)})
        blobs.append(data)
    manifest = {'format': 1, 'created_at': now.isoformat(), 'root_name': Path(root).name, 'files': entries,
                'excluded': sorted(EXCLUDE_NAMES) + ['*' + s for s in EXCLUDE_SUFFIXES]}
    body = json.dumps(manifest, ensure_ascii=False, indent=1).encode('utf-8')
    mtime = int(now.timestamp())
    with tarfile.open(fileobj=out, mode='w:gz') as tar:
        for name, data in [(MANIFEST, body)] + [('files/' + e['path'], b) for e, b in zip(entries, blobs)]:
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), mtime, 0o600
            tar.addfile(info, io.BytesIO(data))
    return manifest


def _safe_member(member):
    """归档成员路径必须是相对的、不含 ..、是普通文件。"""
    name = member.name
    parts = Path(name).parts
    return (member.isfile() and not name.startswith('/') and '..' not in parts
            and (name == MANIFEST or (len(parts) > 1 and parts[0] == 'files')))


def verify(archive):
    """回读校验：manifest 里每个文件都在、大小和 sha256 都对，且没有 manifest 之外的多余成员。
    返回 (ok, 问题列表, manifest或None)。"""
    problems, manifest = [], None
    try:
        with tarfile.open(archive, 'r:gz') as tar:
            members = tar.getmembers()
            for m in members:
                if not _safe_member(m):
                    problems.append('不安全或非法的成员：%r' % m.name)
            if problems:
                return False, problems, None
            by_name = {m.name: m for m in members}
            if MANIFEST not in by_name:
                return False, ['缺少 manifest'], None
            manifest = json.loads(tar.extractfile(by_name[MANIFEST]).read().decode('utf-8'))
            expected = {'files/' + e['path']: e for e in manifest['files']}
            for name, e in expected.items():
                if name not in by_name:
                    problems.append('缺少文件 ' + e['path'])
                    continue
                data = tar.extractfile(by_name[name]).read()
                if len(data) != e['size'] or hashlib.sha256(data).hexdigest() != e['sha256']:
                    problems.append('内容与 manifest 不符：' + e['path'])
            for name in by_name:
                if name != MANIFEST and name not in expected:
                    problems.append('manifest 之外的多余成员：' + name)
    except (OSError, tarfile.TarError, ValueError, KeyError, EOFError) as exc:
        return False, ['归档无法读取：%s' % type(exc).__name__], None
    return not problems, problems, manifest


# --- 快照（本机、滚动） -------------------------------------------------------------

def _atomic_bytes(path, data):
    handle, tmp = tempfile.mkstemp(dir=str(path.parent), suffix='.tmp')
    try:
        with os.fdopen(handle, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def list_snapshots(directory=None):
    d = Path(directory or backup_dir())
    return sorted(p for p in d.glob('alpha-shadow-private-*.tar.gz') if NAME_RE.fullmatch(p.name)) if d.is_dir() else []


def read_status(directory=None):
    p = Path(directory or backup_dir()) / STATUS_FILE
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _write_status(directory, **fields):
    status = read_status(directory)
    status.update(fields)
    _atomic_bytes(Path(directory) / STATUS_FILE, json.dumps(status, ensure_ascii=False, indent=1).encode('utf-8'))


def snapshot(root=None, directory=None, keep=KEEP, now=None):
    """做一份快照 → 回读校验 → 通过才清理旧的。返回 (归档路径, manifest)。失败抛 BackupError，
    且记下失败原因（页面据此报警）。旧快照在新快照校验通过之前一份都不动。"""
    root, directory = Path(root or root_dir()), Path(directory or backup_dir())
    now = now or datetime.now(CST)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    name = 'alpha-shadow-private-%s.tar.gz' % now.strftime('%Y%m%d-%H%M%S')
    final = directory / name
    for stale in directory.glob('*.partial'):         # 上次中途失败留下的半成品
        stale.unlink()
    try:
        if not root.is_dir():
            raise BackupError('私有数据目录不存在：%s' % root)
        buf = io.BytesIO()
        manifest = build_archive(root, buf, now)
        if not manifest['files']:
            raise BackupError('私有目录里没有任何可备份的文件（%s）——目录配错了？不生成空快照。' % root)
        tmp = directory / (name + '.partial')
        tmp.write_bytes(buf.getvalue())
        os.chmod(tmp, 0o600)
        ok, problems, _ = verify(tmp)
        if not ok:
            tmp.unlink()
            raise BackupError('快照写完后回读校验失败：' + '；'.join(problems[:3]))
        os.replace(tmp, final)
    except BackupError as exc:
        _write_status(directory, last_error=str(exc), last_error_at=now.isoformat())
        raise
    except OSError as exc:
        for stale in directory.glob('*.partial'):
            stale.unlink()
        _write_status_safely(directory, 'OSError: %s' % exc, now)
        raise BackupError('写快照失败：%s' % exc) from exc
    for old in list_snapshots(directory)[:-keep]:
        old.unlink()
    _write_status(directory, last_success_at=now.isoformat(), last_file=name, last_error=None, last_error_at=None,
                  files=len(manifest['files']), bytes=final.stat().st_size)
    return final, manifest


def _write_status_safely(directory, text, now):
    try:
        _write_status(directory, last_error=text, last_error_at=now.isoformat())
    except OSError:
        pass


def health(directory=None, now=None):
    """页面用：(状态 ok/warn/none, 说明)。"""
    now = now or datetime.now(CST)
    status = read_status(directory)
    if not status.get('last_success_at') and not status.get('last_error'):
        return 'none', '本机快照尚未运行过（部署后第一次定时器触发在每天 17:30）。'
    parts, level = [], 'ok'
    if status.get('last_success_at'):
        age = (now - datetime.fromisoformat(status['last_success_at'])).total_seconds() / 3600
        parts.append('最近成功快照 %s（%d 个文件，%.1f MB）' % (
            status['last_success_at'][:16].replace('T', ' '), status.get('files', 0), status.get('bytes', 0) / 1e6))
        if age > STALE_HOURS:
            level = 'warn'
            parts.append('已 %.0f 小时没有新的成功快照，请检查定时器' % age)
    else:
        level = 'warn'
    if status.get('last_error'):
        level = 'warn'
        parts.append('最近一次失败（%s）：%s' % ((status.get('last_error_at') or '')[:16].replace('T', ' '), status['last_error']))
    return level, '；'.join(parts)


# --- 恢复 --------------------------------------------------------------------

def restore(archive, target, force=False):
    """把归档解包到 target。先整体校验；默认不覆盖已存在且内容不同的文件。返回写入的文件数。"""
    ok, problems, manifest = verify(archive)
    if not ok:
        raise BackupError('归档校验失败，不恢复：' + '；'.join(problems[:3]))
    target = Path(target)
    conflicts = []
    with tarfile.open(archive, 'r:gz') as tar:
        for e in manifest['files']:
            dest = target / e['path']
            if dest.exists() and not force and hashlib.sha256(dest.read_bytes()).hexdigest() != e['sha256']:
                conflicts.append(e['path'])
        if conflicts:
            raise BackupError('目标已有内容不同的文件，默认不覆盖（确认后加 --force）：' + '、'.join(conflicts[:5]))
        written = 0
        for e in manifest['files']:
            dest = (target / e['path']).resolve()
            if target.resolve() not in dest.parents:
                raise BackupError('路径越界，已中止：' + e['path'])
            dest.parent.mkdir(parents=True, exist_ok=True)
            _atomic_bytes(dest, tar.extractfile('files/' + e['path']).read())
            written += 1
    return written


def notify_failure(text):
    """失败时尽力推一条企业微信；推不出去也不能掩盖原本的失败。"""
    try:
        from wecom_push import load_config, send_wecom_message
        config_path = os.environ.get('CONFIG_PATH') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'webapp_config.json')
        url = load_config(config_path).get('webhook_url')
        if url:
            send_wecom_message(url, '[Alpha Shadow] 私有数据备份失败：' + text[:300])
    except Exception:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('snapshot', help='做一份本机快照（回读校验通过才清理旧的）')
    p = sub.add_parser('verify', help='校验一份归档（默认最新一份）')
    p.add_argument('archive', nargs='?')
    sub.add_parser('list', help='列出本机快照和最近状态')
    p = sub.add_parser('restore', help='把归档恢复到目录')
    p.add_argument('archive')
    p.add_argument('--to', required=True, help='目标目录（通常是 server/data/private）')
    p.add_argument('--force', action='store_true', help='覆盖已存在且内容不同的文件')
    a = ap.parse_args(argv)
    try:
        if a.cmd == 'snapshot':
            path, manifest = snapshot()
            print('快照完成：%s（%d 个文件，%.1f MB，已回读校验）' % (path, len(manifest['files']), path.stat().st_size / 1e6))
        elif a.cmd == 'verify':
            target = a.archive or (list_snapshots() or [None])[-1]
            if not target:
                raise BackupError('没有可校验的快照')
            ok, problems, manifest = verify(target)
            print(('校验通过：%s（%d 个文件）' % (target, len(manifest['files']))) if ok else '校验失败：' + '；'.join(problems))
            return 0 if ok else 1
        elif a.cmd == 'list':
            for p in list_snapshots():
                print('%s  %.1f MB' % (p.name, p.stat().st_size / 1e6))
            print(health()[1])
        else:
            print('已恢复 %d 个文件到 %s' % (restore(a.archive, a.to, a.force), a.to))
    except BackupError as exc:
        print('失败：%s' % exc, file=sys.stderr)
        if a.cmd == 'snapshot':
            notify_failure(str(exc))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
