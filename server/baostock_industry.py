"""Isolated BaoStock network call; parent enforces a process timeout."""
import contextlib
import io
import json
import sys
from importlib.metadata import version


def main():
    logs = io.StringIO()
    with contextlib.redirect_stdout(logs):
        import baostock as bs
        login = bs.login()
        if login.error_code != '0':
            raise ValueError('BaoStock login: ' + login.error_msg)
        try:
            result = bs.query_stock_industry()
            if result.error_code != '0':
                raise ValueError('BaoStock industry: ' + result.error_msg)
            rows = []
            while result.next():
                rows.append(dict(zip(result.fields, result.get_row_data())))
                if len(rows) > 10000:
                    raise ValueError('BaoStock row bound exceeded')
            if result.error_code != '0':
                raise ValueError('BaoStock incomplete query: ' + result.error_msg)
        finally:
            bs.logout()
    print(json.dumps({'provider_version': version('baostock'), 'rows': rows}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        sys.exit(1)
