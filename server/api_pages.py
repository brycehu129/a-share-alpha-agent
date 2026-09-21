"""各页面的 JSON 接口：看板、持仓与自选、策略参数提议、盘后分析、哨兵。

设置页（推送/大模型/备份/健康）的接口在 api.py。数据层全部复用现有模块，这里只做「取数 → 整形」。
导入本模块即完成路由注册（api.get / api.post 装饰器）。
"""
from api import ApiError, get, history_dir, post, text
import llm_settings

# --- 看板 ---------------------------------------------------------------------------------


def _with_live_market(data, force=False):
    """给看板补上实时统计（指数、两市成交额、资金流向、涨跌家数）。这些直接取自行情源，**不读**
    GitHub 上那份日级快照——快照会因为批处理没跑而停在几天前（曾经停在上周五）。
    快照里的 market.quotes 被实时指数覆盖；实时取不到就保留快照，前端靠 live.errors 提示。
    不改动 dashboard_page 缓存里的原 dict；任何问题都退回原数据，市场行情页不能因此整页失败。"""
    try:
        import market_review
        live = market_review.market_stats(force=force)
    except Exception:
        return data
    out = dict(data or {})
    out["live"] = live
    indices = live.get("indices")
    if indices:
        market = dict(out.get("market") or {})
        market["quotes"] = indices["quotes"]
        market["quotes_source"] = "live"
        out["market"] = market
    return out


@get("/api/dashboard")
def api_dashboard(query):
    import dashboard_page
    data, fetched_at, stale, error = dashboard_page.get_dashboard_data(force_refresh=query.get("refresh") == "1")
    if query.get("live") != "0" and (isinstance(data, dict) or data is None):     # 候选池页不需要实时统计
        with_live = _with_live_market(data, force=query.get("refresh") == "1")
        if with_live and with_live.get("live"):     # 实时统计取不到就保持原样（含 GitHub 也失败时的 None）
            data = with_live
    return {"data": data, "fetched_at": dashboard_page.iso_cst(fetched_at) if fetched_at else None,
            "stale": stale, "error": error}


@get("/api/market/review")
def api_market_review(query):
    """涨停/跌停/炸板/昨日涨停/强势池 + 龙虎榜 + 次日关注。各块各带自己的日期和失败原因。"""
    import market_review
    return {"review": market_review.current_review()}


@get("/api/market/stock")
def api_market_stock(query):
    """个股详情抽屉：行情快照、日 K + 均线、公司资料/概念、龙虎榜席位、它在复盘里的位置。"""
    import market_review
    import stock_detail
    symbol = (query.get("symbol") or "").strip().lower()
    try:
        return {"detail": stock_detail.detail(symbol, market_review.current_review(), force=query.get("refresh") == "1")}
    except market_review.ReviewError as exc:
        raise ApiError(str(exc))


# --- 持仓与自选 ---------------------------------------------------------------------------
#
# 录入方式照券商来：自选股用「代码/名称/拼音」搜索加入；持仓不能凭空录入，只能从自选股「买入」、
# 从持仓「卖出」，成本价来自买入价。每一行的「系统结论」（具备买入信号 / 可做T / 可暂时卖出 /
# 彻底卖出）由规则算出（book_verdict.py），不由用户声明。

def _strings(body):
    """账本的校验函数按「表单字符串」写的；JSON 里的数字/null 先统一成字符串。"""
    return {k: "" if v is None else str(v) for k, v in body.items()}


NO_ALERTS = {"count": 0, "urgent": 0}


def _alert_counts():
    """每行"告警 N"按钮的角标。只读磁盘；任何问题都当没有——持仓页不能被告警文件拖垮。"""
    try:
        import sentinel_view
        return sentinel_view.alert_counts()
    except Exception:
        return {}


def _row_verdicts(holdings, watchlist, quotes):
    """每只股票的系统结论。只读本地日线 + 已取到的报价，不再联网。
    单只算不出来就只影响那一行（给一个说明文案），绝不让整页失败。"""
    import book_levels
    import book_verdict
    import intraday_engine
    import live_check
    import t_context
    history = history_dir()
    flow_dir = intraday_engine.data_dir()
    pause = book_verdict.market_pause(history) if watchlist else None
    out = {}
    for kind, rows in (("holding", holdings), ("watch", watchlist)):
        for row in rows:
            q = quotes.get(row["symbol"])
            if not q:
                continue
            try:
                bars = live_check.load_series(history, row["symbol"])[0]
                facts, _ = live_check.price_facts(bars, q) if bars else ({}, [])
                if (facts.get("adjustment_drift_pct") or 0) > live_check.ADJUST_TOLERANCE_PCT:
                    facts = {}
                limits = live_check.limit_facts(q)
                if kind == "holding":
                    enriched = book_levels.enrich(row, bars, q.get("quote_date"))
                    # 做T 的环境（大盘/板块/资金流）懒取：只有价格位置满足时才联网，平时页面不多花一个请求。
                    env_fn = (lambda sym=row["symbol"], qd=q.get("quote_date"): t_context.build_env(
                        sym, quotes, qd,
                        lambda s_, d_: t_context.sector_change(history, s_, d_, cache_dir=flow_dir),
                        lambda s_, d_: t_context.flow_facts(flow_dir, s_, d_)))
                    out[("holding", row["symbol"])] = book_verdict.holding_verdict(enriched, q, facts, limits, env_fn)
                else:
                    out[("watch", row["symbol"])] = book_verdict.watch_verdict(row, q, facts, limits, pause)
            except Exception as exc:     # 单只失败不拖垮整页；原因写进结论里，别静默
                out[(kind, row["symbol"])] = {"action": "nodata", "label": "数据不足",
                                              "reasons": ["计算失败：%s" % type(exc).__name__]}
    return out


@get("/api/book")
def api_book(query):
    """持仓/自选页顺带展示现价和系统结论。取价失败不影响账本本身：行情只是锦上添花，
    不能因为网络问题让人连自己录的持仓都看不到；但也不能静默，失败原因放进 trouble。"""
    import portfolio_book
    trades = portfolio_book.load("trades")
    holdings = portfolio_book.with_sellable(portfolio_book.load("holdings"), trades)
    watchlist = portfolio_book.load("watchlist")
    quotes, trouble, session = {}, "", None
    symbols = sorted({r["symbol"] for r in holdings} | {r["symbol"] for r in watchlist})
    if symbols:
        try:
            import live_quote
            import t_context
            # 顺带取三个大盘指数（同一批请求）：做T 判断要看大盘。
            snapshot = live_quote.snapshot(symbols + list(t_context.INDEX_SYMBOLS))
            session = live_quote.SESSION_LABEL.get(snapshot.get("session"))
            quotes = {q["symbol"]: q for q in snapshot["quotes"]}
            failed = [f for f in snapshot["failures"] if f["symbol"] not in t_context.INDEX_SYMBOLS]
            if failed:
                trouble = "%d 只股票未取得行情，现价显示为“—”：%s" % (len(failed), ",".join(f["symbol"] for f in failed[:5]))
        except (OSError, ValueError) as exc:
            trouble = "行情获取失败，只显示账本数据：%s" % exc

    alerts = _alert_counts()
    verdicts = _row_verdicts(holdings, watchlist, quotes)
    held = {h["symbol"]: h for h in holdings}
    rows, total_cost, total_value, today_pnl = [], 0.0, 0.0, 0.0
    for h in holdings:
        q = quotes.get(h["symbol"])
        last = float(q["last"]) if q else None
        total_cost += h["cost_price"] * h["shares"]
        if last:
            total_value += last * h["shares"]
            today_pnl += (last - float(q["previous_close"])) * h["shares"]
        rows.append({"symbol": h["symbol"], "name": h.get("name") or "", "shares": h["shares"],
                     "sellable": h["sellable_shares"], "cost_price": h["cost_price"], "opened_on": h.get("opened_on"),
                     "last": last, "change_pct": float(q["change_pct"]) if q else None,
                     "market_value": last * h["shares"] if last else None,
                     "pnl": (last - h["cost_price"]) * h["shares"] if last else None,
                     "pct": (last / h["cost_price"] - 1) * 100 if last else None,
                     "limit_up": q.get("limit_up") if q else None, "limit_down": q.get("limit_down") if q else None,
                     "verdict": verdicts.get(("holding", h["symbol"])), "alerts": alerts.get(h["symbol"], NO_ALERTS)})
    summary = None
    if total_cost and total_value:
        pnl = total_value - total_cost
        summary = {"cost": total_cost, "value": total_value, "pnl": pnl, "pnl_pct": pnl / total_cost * 100,
                   "today_pnl": today_pnl}

    watch_rows = []
    for w in watchlist:
        q = quotes.get(w["symbol"])
        watch_rows.append({"symbol": w["symbol"], "name": w.get("name") or "", "added_on": w.get("added_on"),
                           "last": float(q["last"]) if q else None,
                           "change_pct": float(q["change_pct"]) if q else None, "note": w.get("note") or "",
                           "held_shares": held[w["symbol"]]["shares"] if w["symbol"] in held else 0,
                           "limit_up": q.get("limit_up") if q else None, "limit_down": q.get("limit_down") if q else None,
                           "verdict": verdicts.get(("watch", w["symbol"])),
                           "alerts": alerts.get(w["symbol"], NO_ALERTS)})
    return {"holdings": rows, "watchlist": watch_rows, "summary": summary, "trouble": trouble, "session": session,
            "trades": portfolio_book.recent_trades(50)}


def _book_action(fn, message):
    import portfolio_book
    try:
        fn()
    except portfolio_book.BookError as exc:
        raise ApiError(str(exc))
    except OSError as exc:
        raise ApiError("写入账本失败：%s" % exc, 500)
    return {"message": message}


@get("/api/book/rules")
def api_book_rules(query):
    """页面上「判断规则」抽屉的内容。数字全部取自代码里在用的常量，改了阈值说明自动跟着变。"""
    import book_verdict
    return book_verdict.rules_doc()


@get("/api/book/search")
def api_book_search(query):
    """加入自选的搜索框：代码/名称/拼音首字母 → 沪深A股候选。"""
    import stock_search
    try:
        return {"results": stock_search.search(query.get("q"))}
    except stock_search.SearchError as exc:
        raise ApiError(str(exc), 502)


@get("/api/book/lookup")
def api_book_lookup(query):
    """选中一只股票后带出名称、现价、涨跌、涨跌停价，以及它是否已在自选/已持有。
    行情取不到时 quote 为空但名称照常返回——名称来自搜索接口，不依赖行情。"""
    import live_quote
    import portfolio_book
    import stock_search
    try:
        symbol = portfolio_book.normalize_symbol(query.get("symbol"))
    except portfolio_book.BookError as exc:
        raise ApiError(str(exc))
    try:
        info = stock_search.resolve(symbol)
    except stock_search.SearchError:
        info = None
    quote, warning = None, ""
    try:
        snap = live_quote.snapshot([symbol])
        quote = snap["quotes"][0] if snap["quotes"] else None
    except (OSError, ValueError):
        pass
    if info is None and quote is None:
        raise ApiError("没有找到 %s：只支持沪深A股（不含北交所、ETF、指数）。" % symbol)
    if quote is None:
        warning = "暂时取不到行情，名称来自搜索结果。"
    held = next((h for h in portfolio_book.load("holdings") if h["symbol"] == symbol), None)
    return {"symbol": symbol, "name": (quote or {}).get("name") or (info or {}).get("name") or "",
            "last": float(quote["last"]) if quote else None,
            "change_pct": float(quote["change_pct"]) if quote else None,
            "previous_close": float(quote["previous_close"]) if quote else None,
            "limit_up": quote.get("limit_up") if quote else None,
            "limit_down": quote.get("limit_down") if quote else None,
            "in_watch": any(w["symbol"] == symbol for w in portfolio_book.load("watchlist")),
            "held_shares": held["shares"] if held else 0, "lot": portfolio_book.lot_size(symbol),
            "warning": warning}


@post("/api/book/watch")
def api_book_watch(body):
    """加入自选。名称不用填：前端从搜索结果带上；没带就服务端再查一次，查无此股直接拒绝。"""
    import portfolio_book
    import stock_search
    form = _strings(body)
    if not form.get("name"):
        try:
            symbol = portfolio_book.normalize_symbol(form.get("symbol"))
        except portfolio_book.BookError as exc:
            raise ApiError(str(exc))
        try:
            info = stock_search.resolve(symbol)
        except stock_search.SearchError:
            info = {"name": ""}                        # 搜索接口暂时不通：先加进去，名称之后可补
        if info is None:
            raise ApiError("没有找到 %s：只支持沪深A股（不含北交所、ETF、指数）。" % symbol)
        form["name"] = info["name"]
    return _book_action(lambda: portfolio_book.add_watch(form), "已加入自选。")


@post("/api/book/buy")
def api_book_buy(body):
    import portfolio_book
    return _book_action(lambda: portfolio_book.buy(_strings(body)), "买入已记录，持仓与成本价已更新。")


@post("/api/book/sell")
def api_book_sell(body):
    import portfolio_book
    return _book_action(lambda: portfolio_book.sell(_strings(body)), "卖出已记录，持仓已更新。")


@post("/api/book/holding/remove")
def api_book_holding_remove(body):
    """纠错用：删掉一条持仓记录（不产生成交流水）。正常减仓/清仓请用卖出。"""
    import portfolio_book
    return _book_action(lambda: portfolio_book.remove("holdings", text(body, "symbol")), "已从持仓中删除。")


@post("/api/book/watch/remove")
def api_book_watch_remove(body):
    import portfolio_book
    return _book_action(lambda: portfolio_book.remove("watchlist", text(body, "symbol")), "已从自选中删除。")


# --- 策略参数提议 -------------------------------------------------------------------------

PROPOSAL_TRACK_LABEL = {"breakout": "突破", "pullback": "回调反弹"}


def _fmt_param(name, value):
    if value is None:
        return "—"
    if name == "exit.hold_sessions":
        return "%d 个交易日" % value
    if name.endswith("_mult"):
        return "%.2f×" % value
    if name == "exit.target_r":
        return "R=%.2f" % value
    if name == "exit.breakeven_arm_frac":
        return "止盈幅度的 %.0f%%" % (value * 100)
    return "%.2f%%" % (value * 100)


def _at(iso):
    return str(iso or "")[:16].replace("T", " ")


@get("/api/proposals")
def api_proposals(query):
    """提议方（AI 或规则）给的文字一律当不可信文本：这里原样交出，前端只用文本插值渲染，不用 v-html。"""
    import exec_spec
    import proposals as pr
    try:
        store = pr.load()
    except pr.ProposalError as exc:
        raise ApiError(str(exc), 500)
    rev, _ = pr.effective(store)

    spec_rows = []
    for track in ("breakout", "pullback"):
        current = pr.current_template(store, track)
        base = exec_spec.SPECS[track]
        for name, d in exec_spec.TUNABLE_PARAMS.items():
            if track not in d["tracks"]:
                continue
            value = exec_spec.get_param(current, name)
            if value is None:
                continue
            base_value = exec_spec.get_param(base, name)
            spec_rows.append({
                "track": PROPOSAL_TRACK_LABEL[track], "label": d["label"], "value": _fmt_param(name, value),
                "changed": value != base_value, "base": _fmt_param(name, base_value),
                "range": "%s ~ %s" % (_fmt_param(name, d["floor"]), _fmt_param(name, d["ceiling"])),
                "step": "±%s" % _fmt_param(name, d["step_cap"])})
    breakevens = [{"track": PROPOSAL_TRACK_LABEL[t],
                   "value": exec_spec.breakeven_win_rate(pr.current_spec(store, t)["exit"])}
                  for t in ("breakout", "pullback")]

    pending = []
    for p in reversed([p for p in store["proposals"] if p["status"] == pr.PENDING]):
        ev = p["evidence"] or {}
        eff = pr.preview(store, p)
        pending.append({
            "id": p["id"], "track": PROPOSAL_TRACK_LABEL.get(p["track"], p["track"]),
            "label": exec_spec.TUNABLE_PARAMS[p["parameter"]]["label"],
            "old": _fmt_param(p["parameter"], p["old"]), "new": _fmt_param(p["parameter"], p["new"]),
            "source": p["source"], "created_at": _at(p["created_at"]),
            "breakeven_before": eff["breakeven_before"], "breakeven_after": eff["breakeven_after"],
            "rationale": p["rationale"] or "", "evidence_n": ev.get("n"), "evidence_cohorts": ev.get("cohorts"),
            "evidence_version": ev.get("execution_version"), "evidence_summary": ev.get("summary") or ""})

    history = [{"id": p["id"], "status": pr.STATUS_LABEL.get(p["status"], p["status"]),
                "target": "%s.%s" % (p["track"], p["parameter"]), "change": "%s → %s" % (p["old"], p["new"]),
                "source": p["source"], "note": p.get("reason") or p.get("decision_note") or ""}
               for p in [p for p in reversed(store["proposals"]) if p["status"] != pr.PENDING][:30]]
    revisions = [{"revision": "r%d" % r["revision"], "at": _at(r["approved_at"]),
                  "target": "%s.%s" % (r["track"], r["parameter"]), "change": "%s → %s" % (r["old"], r["new"]),
                  "note": r.get("note") or ""} for r in reversed(store["revisions"])]
    return {"revision": rev, "exec_version": exec_spec.execution_version(rev),
            "next_exec_version": exec_spec.execution_version(rev + 1),
            "nominal_atr_pct": exec_spec.NOMINAL_ATR_PCT * 100, "spec_rows": spec_rows,
            "breakevens": breakevens, "pending": pending, "history": history, "revisions": revisions}


@post("/api/proposals/approve")
def api_proposal_approve(body):
    import exec_spec
    import proposals
    try:
        rev = proposals.approve(text(body, "id"), text(body, "note"))
    except proposals.ProposalError as exc:
        raise ApiError(str(exc))
    except OSError as exc:
        raise ApiError("写入失败：%s" % type(exc).__name__, 500)
    return {"message": "已批准 %s：%s.%s %s → %s。从下一次日线流程起新冻结的计划生效（执行版本 %s）。" % (
        rev["proposal_id"], rev["track"], rev["parameter"], rev["old"], rev["new"],
        exec_spec.execution_version(rev["revision"]))}


@post("/api/proposals/reject")
def api_proposal_reject(body):
    import proposals
    try:
        proposals.reject(text(body, "id"), text(body, "note"))
    except proposals.ProposalError as exc:
        raise ApiError(str(exc))
    except OSError as exc:
        raise ApiError("写入失败：%s" % type(exc).__name__, 500)
    return {"message": "已驳回。"}


# --- 盘后分析 -----------------------------------------------------------------------------

def _iso_or_none(value):
    return value.isoformat() if value else None


@get("/api/postclose")
def api_postclose(query):
    import postclose_report
    import webapp_views
    state = webapp_views.run_state()
    report = postclose_report.latest_report()
    out = None
    if report is not None:
        meta = report.get("ai_meta") or {}
        out = {"id": report["id"],
               # markdown_to_html 先整体转义再套格式，报告里出现 <script> 也只会被当文本显示。
               "html": webapp_views.markdown_to_html(postclose_report.render(report)),
               "ai": ("%s · 输入 %s / 输出 %s tokens" % (meta.get("model"), meta.get("input_tokens"), meta.get("output_tokens"))
                      if meta.get("status") == "ok" else "")}
    return {"state": {"running": state["running"], "started_at": _iso_or_none(state["started_at"]),
                      "finished_at": _iso_or_none(state["finished_at"]), "error": state["error"],
                      "last_run_id": state["last_run_id"]},
            "report": out}


@post("/api/postclose/run")
def api_postclose_run(body):
    import webapp_views
    llm_settings.apply()      # 后台线程在本进程里调用大模型，要用页面保存的 key
    started, message = webapp_views.start_run(history_dir(), push_enabled=not body.get("no_push"))
    if not started:
        raise ApiError(message, 409)
    return {"message": message}


# --- 哨兵 ---------------------------------------------------------------------------------

@get("/api/sentinel")
def api_sentinel(query):
    """"全天告警汇总"抽屉：告警、情景对账、周报、采集健康。只读磁盘。"""
    import sentinel_view
    return sentinel_view.sentinel_payload(query.get("day"))


def _live_flow(symbol, day):
    """某只股票"此刻"的资金流与盘口事实。联网，所以每一项都独立降级：取不到就给原因，不让整个接口失败。
    只有查看的就是今天时才联网——历史日期接口本来就查不到，只用留存。"""
    import money_flow
    import sentinel_view
    from datetime import datetime
    from collect_quotes import CST
    out = {"flow_now": None, "flow_error": None, "book_now": None, "flow_table": None,
           "flow_source_note": money_flow.SOURCE_NOTE}
    if day != datetime.now(CST).strftime("%Y-%m-%d"):
        return out
    try:
        flow = money_flow.fetch_flow(symbol)
        out["flow_now"] = money_flow.compact(flow)
        out["flow_table"] = sentinel_view.with_outer(money_flow.half_hour_table(flow["rows"]), symbol, day)
    except money_flow.FlowError as exc:
        out["flow_error"] = str(exc)
    try:
        import live_quote
        quotes = live_quote.snapshot([symbol])["quotes"]
        out["book_now"] = money_flow.book_facts(quotes[0]) if quotes else None
    except (OSError, ValueError):
        pass
    return out


@get("/api/sentinel/symbol")
def api_sentinel_symbol(query):
    """持仓/自选某一行的"告警"抽屉：这只股票当天的告警、情景、留存的资金流表，加上此刻的资金流。"""
    import sentinel_view
    symbol = sentinel_view.safe_symbol(query.get("symbol"))
    if symbol is None:
        raise ApiError("股票代码格式不对：只支持沪深个股，例如 sz300458。")
    payload = sentinel_view.symbol_payload(symbol, query.get("day"))
    live = _live_flow(symbol, payload["day"])
    if live["flow_table"] is None:                 # 没联网（历史日期或取不到）：用留存的表
        live["flow_table"] = payload["flow_table"]
    payload.update(live)
    return payload
