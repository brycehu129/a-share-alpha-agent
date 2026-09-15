#!/usr/bin/env python3
"""Persist sourced market snapshots and readable reports; no model predictions."""
import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from collect_quotes import CST, collect, open_database
from daily_data import collect_datasets, dataset_markdown
from universe_data import collect_universe, universe_markdown

SYMBOLS = ['sh000001', 'sz399001', 'sh000300', 'sh000852', 'sz399006', 'sh000688']
LIMITATIONS = [
    '范围仅为6个指数，不是全市场个股或板块扫描。',
    '交易日历尚未接入，不能仅凭星期或行情时间认定当天开市。',
    '成交额、市场宽度、板块强度、公告与新闻尚未纳入本报告。',
    '本报告为程序按行情生成的事实摘要，未调用大模型；不输出选股、胜率或交易指令。',
    '尚无真实虚拟交易记录，净值、Alpha、回撤和胜率均不计算。',
]


def canonical(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(data):
    return hashlib.sha256(canonical(data)).hexdigest()


def read_history(root):
    entries = []
    for path in sorted((root / 'records').glob('*.json')):
        envelope = json.loads(path.read_text(encoding='utf-8'))
        payload = envelope['payload']
        if envelope['sha256'] != digest(payload) or path.stem != payload['id']:
            raise ValueError('历史记录校验失败，停止写入: ' + path.name)
        entries.append(payload)
    return sorted(entries, key=lambda item: (item['generated_at'], item['id']))


def markdown(report):
    def safe(value):
        return html.escape(str(value)).replace('|', '&#124;').replace('\n', ' ')
    status = '采集成功' if report['status'] == 'success' else '本次采集失败'
    lines = [f'# A股市场快照 · {status}', '',
             f'采集时间（北京时间）：{report["generated_at"]}', '',
             f'报告编号：`{report["id"]}` · 报告程序 {report["version"]}', '',
             f'已恢复历史报告：{report["previous_count"]} 份；本报告不代表新的交易日样本。', '',
             '## 本次结论', '', report['summary'], '',
             '## 行情与时间', '',
             '| 指数 | 代码 | 最新点位 | 昨收 | 涨跌幅 | 行情时间（北京时间） |',
             '|---|---|---:|---:|---:|---|']
    for q in report['quotes']:
        lines.append('| ' + ' | '.join(safe(q[k]) for k in
                     ['name', 'symbol', 'last', 'previous_close', 'change_pct', 'quote_at']) + ' |')
    if not report['quotes']:
        lines.extend(['', '本次没有可用行情，未回填历史报价。'])
    lines += ['', '## 数据质量', ''] + ['- ' + safe(w) for w in report['warnings']]
    lines += ['', '## 当前边界', ''] + ['- ' + safe(w) for w in report['limitations']]
    lines += ['', '## 来源与可追溯性', '',
              f'- 数据源：[腾讯行情]({report["source_url"]})',
              '- 原始字段、响应摘要与行情时间保存在同编号 JSON 中。',
              '- 涨跌幅按最新点位与昨收计算；并非策略收益。',
              '- 相同行情时间的重复采集只增加报告数，不增加独立交易日数。', '']
    return ('\n'.join(lines) + (dataset_markdown(report['datasets']) if 'datasets' in report else '')
            + (universe_markdown(report['universe']) if 'universe' in report else ''))


def build_report(db, record_id, history):
    db.row_factory = sqlite3.Row
    run = db.execute('SELECT * FROM collection_runs ORDER BY fetched_at DESC LIMIT 1').fetchone()
    if run is None:
        raise ValueError('没有本次采集记录')
    quotes = []
    fetched = datetime.fromisoformat(run['fetched_at'])
    for row in db.execute('SELECT * FROM quote_snapshots WHERE run_id=? ORDER BY symbol', (run['id'],)):
        q = dict(row)
        q.pop('run_id')
        q['raw_fields'] = json.loads(q.pop('raw_fields_json'))
        q['change_pct'] = str(((Decimal(q['last']) / Decimal(q['previous_close']) - 1) * 100).quantize(Decimal('0.01'))) + '%'
        q['age_seconds'] = round((fetched - datetime.fromisoformat(q['quote_at'])).total_seconds())
        quotes.append(q)
    complete = run['status'] == 'success' and {q['symbol'] for q in quotes} == set(SYMBOLS)
    warnings = ['未核验交易日历；行情时间以数据源字段为准，不等同于交易所最终收盘确认。']
    if any(q['age_seconds'] > 900 for q in quotes):
        warnings.append('至少一项报价距采集时间超过15分钟。收盘后或休市时可能正常，但不应视为盘中实时价。')
    dates = sorted({q['quote_at'][:10] for q in quotes})
    if len(dates) > 1:
        warnings.append('指数行情日期不一致，不用于同日横向比较。')
    if any(date != fetched.date().isoformat() for date in dates):
        warnings.append('报价日期并非采集当天，不作为今日行情。')
    if not complete:
        warnings.append('采集错误: ' + (run['error'] or '响应不完整'))
        summary = '本次行情不可用，暂停市场判断；保留原有历史，不生成候选股票。'
    elif len(dates) != 1:
        summary = '数据日期不齐，仅展示各自报价；暂停横向比较。'
    else:
        changes = [Decimal(q['change_pct'][:-1]) for q in quotes]
        up, down = sum(c > 0 for c in changes), sum(c < 0 for c in changes)
        summary = f'源数据日期 {dates[0]}：所监测6个指数中，{up}个上涨、{down}个下跌、{6-up-down}个持平。仅描述指数相对昨收表现，不推断全市场赚钱效应。'
    return {
        'schema_version': 1, 'version': 'snapshot-0.2', 'id': record_id,
        'generated_at': run['fetched_at'], 'status': 'success' if complete else 'failed',
        'previous_count': len(history), 'previous_id': history[-1]['id'] if history else None,
        'source_url': run['source_url'], 'response_sha256': run['response_sha256'],
        'quote_dates': dates, 'quotes': quotes, 'summary': summary, 'warnings': warnings,
        'limitations': LIMITATIONS, 'predictions': [], 'model_called': False,
    }


def persist_report(root, report):
    records, reports = root / 'records', root / 'reports'
    records.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    json_path = records / (report['id'] + '.json')
    md_path = reports / (report['id'] + '.md')
    if json_path.exists() or md_path.exists():
        raise ValueError('报告ID已存在；禁止覆盖历史记录')
    md = markdown(report)
    with json_path.open('x', encoding='utf-8') as stream:
        json.dump({'sha256': digest(report), 'payload': report}, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    with md_path.open('x', encoding='utf-8') as stream:
        stream.write(md)
    entries = read_history(root)
    lines = ['# A股 Alpha Agent · 报告历史', '',
             '当前阶段：指数快照、日线样本及沪深批量行情；无模型选股、无交易、尚未连接 Dashboard。', '',
             f'累计报告：{len(entries)} 份。', '',
             '历史文件按报告编号追加保存，最新目录可更新。Git 管理员仍可修改仓库，因此这不是防篡改审计存储。', '',
             '| 采集时间（北京时间） | 状态 | 报告 |', '|---|---|---|']
    for item in sorted(entries, key=lambda x: x['generated_at'], reverse=True):
        lines.append(f'| {item["generated_at"]} | {item["status"]} | [{item["id"]}](reports/{item["id"]}.md) |')
    (root / 'README.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return md


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--results', type=Path, default=Path('results'))
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'\d+-\d+', args.run_id):
        parser.error('run-id must be the numeric GitHub run ID and attempt separated by a hyphen')
    history = read_history(args.history)
    if args.verify_only:
        matches = [r for r in history if r['id'] == args.run_id]
        if len(matches) != 1:
            raise ValueError('新运行机器未能从仓库恢复本次报告')
        r = matches[0]
        restored_md = (args.history / 'reports' / (args.run_id + '.md')).read_text(encoding='utf-8')
        if restored_md != markdown(r):
            raise ValueError('已保存报告与原始记录不一致')
        if len(history) != r['previous_count'] + 1:
            raise ValueError('历史报告数不符合追加预期')
        print(f'PASS: fresh runner restored {len(history)} reports; prior={r["previous_count"]}; current={r["id"]}')
        print(r['summary'])
        if (r['status'] != 'success' or r.get('datasets', {}).get('status', 'success') != 'success'
                or ('universe' in r and (not r['universe']['list_verified']
                                        or r['universe']['coverage_pct'] < 90))):
            return 1
        summary_path = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary_path:
            with open(summary_path, 'a', encoding='utf-8') as stream:
                stream.write('历史已推送并在全新运行机器恢复验证成功。\n\n' + restored_md)
        return 0
    if any(r['id'] == args.run_id for r in history):
        raise ValueError('重复报告ID，停止；重跑请使用新 attempt')
    args.results.mkdir(parents=True, exist_ok=True)
    db_path = args.results / 'collection.sqlite3'
    if db_path.exists():
        raise ValueError('本次临时数据库已经存在，停止以避免混入旧数据')
    db = open_database(db_path)
    try:
        collect(db, SYMBOLS, 20)
        report = build_report(db, args.run_id, history)
    finally:
        db.close()
    report['datasets'] = collect_datasets()
    report['universe'] = collect_universe()
    report['version'] = 'snapshot-0.4'
    report['limitations'] = [
        '6指数快照、固定5只日线样本及源清单内沪深批量报价；具体覆盖率见下文，不是候选股。',
        '历史观察交易日已保存，官方未来休市日历尚未核验。',
        '板块、新闻、评分、模型预测、虚拟交易及Dashboard接入尚未完成。',
    ]
    md = persist_report(args.history, report)
    (args.results / 'report.md').write_text(md, encoding='utf-8')
    print(f'已恢复 {len(history)} 份历史；生成 {report["id"]}；状态 {report["status"]}')
    print(report['summary'])
    # Failed captures are committed too; verification turns the workflow red afterward.
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
        print(f'报告生成/校验失败: {exc}', file=sys.stderr)
        sys.exit(1)
