#!/usr/bin/env python3
"""Read-only Tencent quote collector. Python 3.9+, standard library only.

Run: python3 collect_quotes.py --repeat 3 --interval 10
Data is local to this server; this does not update the Dashboard or place orders.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

CST = timezone(timedelta(hours=8))
DEFAULT_SYMBOLS = "sh000001,sz399001,sh000300,sh000852,sz000001"
MAX_BYTES = 1_000_000


def parse_quotes(raw, symbols, fetched_at):
    text = raw.decode("gb18030", errors="strict")
    records = {}
    for symbol, body in re.findall(r'v_((?:sh|sz)\d{6})="([^"\r\n]*)"\s*;', text):
        if symbol not in symbols:
            continue
        if symbol in records:
            raise ValueError("响应中股票代码重复: " + symbol)
        fields = body.split("~")
        if len(fields) < 31 or fields[2] != symbol[2:] or not fields[1]:
            raise ValueError("行情字段不完整或代码不匹配: " + symbol)
        values = []
        for offset in (3, 4, 5):
            try:
                number = Decimal(fields[offset])
            except InvalidOperation as exc:
                raise ValueError("价格字段异常: " + symbol) from exc
            if not number.is_finite() or number < 0:
                raise ValueError("价格字段异常: " + symbol)
            values.append(number)
        last, previous, opening = values
        if last <= 0 or previous <= 0:
            raise ValueError("最新价或昨收无有效值: " + symbol)
        if not re.fullmatch(r"\d{14}", fields[30]):
            raise ValueError("行情时间字段异常: " + symbol)
        quote_at = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=CST)
        age = (fetched_at - quote_at).total_seconds()
        if age < -300:
            raise ValueError("行情时间超前超过5分钟，请检查服务器时钟: " + symbol)
        records[symbol] = {
            "symbol": symbol, "name": fields[1], "last": str(last),
            "previous_close": str(previous), "open": str(opening),
            "change_pct": str(((last / previous - 1) * 100).quantize(Decimal("0.01"))),
            "quote_at": quote_at.isoformat(), "age_seconds": round(age),
            "raw_fields": fields,
        }
    missing = set(symbols) - records.keys()
    if missing:
        raise ValueError("响应缺少行情: " + ",".join(sorted(missing)))
    return [records[s] for s in symbols]


def open_database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=20)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS collection_runs (
            id TEXT PRIMARY KEY, fetched_at TEXT NOT NULL,
            source_url TEXT NOT NULL, status TEXT NOT NULL,
            error TEXT, response_sha256 TEXT
        );
        CREATE TABLE IF NOT EXISTS quote_snapshots (
            run_id TEXT NOT NULL REFERENCES collection_runs(id),
            symbol TEXT NOT NULL, name TEXT NOT NULL,
            last TEXT NOT NULL, previous_close TEXT NOT NULL,
            open TEXT NOT NULL, quote_at TEXT NOT NULL,
            raw_fields_json TEXT NOT NULL,
            PRIMARY KEY(run_id, symbol)
        );
        CREATE INDEX IF NOT EXISTS idx_quotes_symbol_time
            ON quote_snapshots(symbol, quote_at);
    """)
    return db


def collect(db, symbols, timeout):
    run_id = str(uuid.uuid4())
    url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
    fetched_at = datetime.now(CST)
    response_hash = None
    try:
        request = Request(url, headers={"User-Agent": "AlphaResearchCollector/0.1"})
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise ValueError("HTTP 状态异常: " + str(response.status))
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("响应超过大小限制")
        response_hash = hashlib.sha256(raw).hexdigest()
        fetched_at = datetime.now(CST)
        quotes = parse_quotes(raw, symbols, fetched_at)
        with db:
            db.execute("INSERT INTO collection_runs VALUES(?,?,?,?,?,?)",
                       (run_id, fetched_at.isoformat(), url, "success", None, response_hash))
            db.executemany("INSERT INTO quote_snapshots VALUES(?,?,?,?,?,?,?,?)", [
                (run_id, q["symbol"], q["name"], q["last"], q["previous_close"],
                 q["open"], q["quote_at"], json.dumps(q["raw_fields"], ensure_ascii=False))
                for q in quotes
            ])
        print("\n采集成功并已保存 | 北京时间 " + fetched_at.isoformat(), flush=True)
        for q in quotes:
            print(f'{q["symbol"]} {q["name"]}  最新 {q["last"]}  '
                  f'涨跌 {q["change_pct"]}%  行情时间 {q["quote_at"]}', flush=True)
        if any(q["age_seconds"] > 900 for q in quotes):
            print("提示：部分行情距采集时间超过15分钟；可能已收盘、休市或数据延迟，不能当作当前实时价。", flush=True)
        print("说明：采集成功不代表当天开市；尚未接入交易日历、个股筛选或网页同步。", flush=True)
        return True
    except (URLError, TimeoutError, OSError, ValueError, ArithmeticError) as exc:
        message = f"{type(exc).__name__}: {exc}"
        with db:
            db.execute("INSERT INTO collection_runs VALUES(?,?,?,?,?,?)",
                       (run_id, fetched_at.isoformat(), url, "failed", message[:1000], response_hash))
        print("采集失败，未写入行情快照：" + message, file=sys.stderr, flush=True)
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    parser.add_argument("--db", type=Path, default=Path(__file__).resolve().parent / "alpha_quotes.sqlite3")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--status", action="store_true", help="只读取已有数据库统计，不请求行情")
    args = parser.parse_args()
    symbols = args.symbols.split(",")
    if not 1 <= len(symbols) <= 20 or len(set(symbols)) != len(symbols) or any(
            not re.fullmatch(r"(?:sh|sz)\d{6}", s) for s in symbols):
        parser.error("仅支持1–20个不重复的sh/sz六位代码，以英文逗号分隔")
    if not 1 <= args.repeat <= 10 or args.interval < 5 or not 1 <= args.timeout <= 60:
        parser.error("repeat须为1–10；interval至少5秒；timeout须为1–60秒")
    if args.status and not args.db.is_file():
        parser.error("数据库尚不存在，请先运行一次采集")
    db = sqlite3.connect(f"{args.db.resolve().as_uri()}?mode=ro", uri=True) if args.status else open_database(args.db)
    try:
        if args.status:
            for status, count in db.execute("SELECT status, count(*) FROM collection_runs GROUP BY status"):
                print(f"{status}: {count}次")
            print("行情快照总数:", db.execute("SELECT count(*) FROM quote_snapshots").fetchone()[0])
            print("最近一次:", db.execute("SELECT fetched_at, status, error FROM collection_runs ORDER BY fetched_at DESC LIMIT 1").fetchone())
            return 0
        ok = 0
        for i in range(args.repeat):
            if i:
                time.sleep(args.interval)
            ok += collect(db, symbols, args.timeout)
        print(f"\n本次成功 {ok}/{args.repeat}；数据库：{args.db.resolve()}")
        return 0 if ok == args.repeat else 1
    finally:
        db.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (sqlite3.Error, OSError) as exc:
        print(f"本地存储错误：{exc}", file=sys.stderr)
        sys.exit(2)
