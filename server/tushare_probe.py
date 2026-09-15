"""Bounded authenticated probes. No token or request body is persisted."""
import argparse
import hashlib
import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

ENDPOINT = 'https://api.tushare.pro'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def classify(body, token):
    code = body.get('code')
    message = str(body.get('msg') or '').replace(token, '[REDACTED]')[:500]
    if code != 0:
        status = 'permission_denied' if code == 2002 or '权限' in message else 'api_error'
        if 'token' in message.lower() or '凭证' in message:
            status = 'authentication_error'
        if '每分钟' in message or '频次' in message:
            status = 'rate_limited'
        return {'status': status, 'code': code, 'message': message, 'fields': [], 'items': []}
    data = body.get('data') or {}
    fields, items = data.get('fields'), data.get('items')
    if (not isinstance(fields, list) or not isinstance(items, list)
            or not all(isinstance(f, str) for f in fields)
            or any(not isinstance(row, list) or len(row) != len(fields) for row in items)):
        raise ValueError('invalid response schema')
    cleaned = json.loads(json.dumps({'fields': fields, 'items': items}, ensure_ascii=False).replace(token, '[REDACTED]'))
    return {'status': 'success' if items else 'empty', 'code': 0, 'message': '', **cleaned}


def probe(api, params, fields, token):
    item = {'api': api, 'params': params, 'requested_fields': fields, 'endpoint': ENDPOINT,
            'fetched_at': datetime.now(timezone.utc).isoformat()}
    try:
        body = json.dumps({'api_name': api, 'params': params, 'fields': fields, 'token': token}).encode()
        req = Request(ENDPOINT, data=body, headers={'Content-Type': 'application/json'}, method='POST')
        with build_opener(NoRedirect).open(req, timeout=20) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError('response too large')
        item.update(classify(json.loads(raw), token))
    except HTTPError as exc:
        item.update(status='http_error', code=exc.code, message='HTTP request failed; no downgrade or redirect', fields=[], items=[])
    except (URLError, TimeoutError, OSError) as exc:
        item.update(status='connection_error', message=type(exc).__name__, fields=[], items=[])
    except (ValueError, TypeError, KeyError) as exc:
        item.update(status='invalid_response', message=type(exc).__name__, fields=[], items=[])
    return item


def render(report):
    safe = lambda x: html.escape(str(x)).replace('|', '&#124;').replace('\n', ' ')
    lines = ['# Tushare 连接与权限实测', '', f'编号：{report["id"]} · 时间：{report["generated_at"]}', '',
             '用户自报积分：120；本测试不读取账户积分。工作流完成表示测试已保存，不表示所有接口有权限。', '',
             '| 接口 | 结果 | 返回条数 | 服务端说明 |', '|---|---|---:|---|']
    for r in report['results']:
        lines.append(f'| {r["api"]} | {r["status"]} | {len(r["items"])} | {safe(r["message"])} |')
    lines += ['', 'success=有数据；empty=请求成功但无数据；permission_denied=权限不足；authentication_error=凭证异常；rate_limited=频率限制；http_error/connection_error=连接未通，不能推断积分权限。', '',
              '这里只对限定股票或日期做接口探测，不是完整行业、日历或日线采集。返回样本保存在同编号 JSON；未写入正式评分。', '',
              '仅向官方HTTPS地址提交凭证，禁止跳转和降级HTTP；Token及请求正文不写入日志或仓库。', '']
    return '\n'.join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--history', type=Path, required=True)
    p.add_argument('--run-id', required=True)
    args = p.parse_args()
    if not re.fullmatch(r'\d+-\d+', args.run_id):
        raise ValueError('invalid run ID')
    token = os.environ.get('TUSHARE_TOKEN', '').strip()
    if not token:
        print('TUSHARE_TOKEN is missing or empty; no requests sent')
        return 1
    now = datetime.now(timezone(timedelta(hours=8)))
    end = (now.date() - timedelta(days=1)).strftime('%Y%m%d')
    start = (now.date() - timedelta(days=30)).strftime('%Y%m%d')
    probes = [
        ('daily', {'ts_code': '000001.SZ', 'start_date': start, 'end_date': end}, 'ts_code,trade_date,open,high,low,close,vol,amount'),
        ('trade_cal', {'exchange': 'SSE', 'start_date': start, 'end_date': now.date().strftime('%Y%m%d')}, 'exchange,cal_date,is_open,pretrade_date'),
        ('stock_basic', {'ts_code': '000001.SZ'}, 'ts_code,name,industry,list_date,list_status'),
        ('adj_factor', {'ts_code': '000001.SZ', 'start_date': start, 'end_date': end}, 'ts_code,trade_date,adj_factor'),
        ('index_member_all', {'ts_code': '000001.SZ'}, 'ts_code,l1_name,l2_name,l3_name,in_date,out_date'),
        ('suspend_d', {'ts_code': '000001.SZ', 'start_date': start, 'end_date': end}, 'ts_code,trade_date,suspend_type'),
        ('stk_limit', {'ts_code': '000001.SZ', 'start_date': start, 'end_date': end}, 'ts_code,trade_date,up_limit,down_limit'),
    ]
    root = args.history / 'tushare'
    root.mkdir(exist_ok=True)
    path = root / (args.run_id + '.json')
    md = path.with_suffix('.md')
    if path.exists() or md.exists():
        raise ValueError('cannot overwrite prior probe')
    report = {'id': args.run_id, 'generated_at': now.isoformat(), 'results': []}
    for api, params, fields in probes:
        item = probe(api, params, fields, token)
        report['results'].append(item)
        print(api + ': ' + item['status'] + ', rows=' + str(len(item['items'])), flush=True)
        time.sleep(2)
    canonical = json.dumps(report, ensure_ascii=False, sort_keys=True).encode()
    with path.open('x') as f:
        json.dump({'payload': report, 'sha256': hashlib.sha256(canonical).hexdigest()}, f, ensure_ascii=False, indent=2)
    with md.open('x') as f:
        f.write(render(report))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        Path(os.environ['GITHUB_STEP_SUMMARY']).write_text(render(report))
    (root / 'README.md').write_text('# Tushare 权限测试历史\n\n' + '\n'.join(
        f'- [{x.stem}]({x.name})' for x in sorted(root.glob('*.md'), reverse=True) if x.name != 'README.md') + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
