"""各页面的 JSON 接口：看板、持仓与自选、策略参数提议、盘后分析、哨兵。

设置页（推送/大模型/备份/健康）的接口在 api.py。数据层全部复用现有模块，这里只做「取数 → 整形」。
导入本模块即完成路由注册（api.get / api.post 装饰器）。
"""
from api import ApiError, get, history_dir, post, text
import llm_settings

# --- 看板 ---------------------------------------------------------------------------------


@get("/api/dashboard")
def api_dashboard(query):
    import dashboard_page
    data, fetched_at, stale, error = dashboard_page.get_dashboard_data(force_refresh=query.get("refresh") == "1")
    return {"data": data, "fetched_at": dashboard_page.iso_cst(fetched_at) if fetched_at else None,
            "stale": stale, "error": error}


# --- 持仓与自选 ---------------------------------------------------------------------------

def _strings(body):
    """账本的校验函数按「表单字符串」写的；JSON 里的数字/null 先统一成字符串。"""
    return {k: "" if v is None else str(v) for k, v in body.items()}


def _declared(h):
    """把你声明过的信息压成一行：只列出你真的填了的，没填的不显示（也就不会触发对应提醒）。"""
    import portfolio_book
    parts = []
    if h.get("hold_type"):
        parts.append(portfolio_book.HOLD_TYPE_LABEL.get(h["hold_type"], h["hold_type"]))
    if h.get("stop_price"):
        parts.append("止损 %s" % h["stop_price"])
    if h.get("target_price"):
        parts.append("目标 %s" % h["target_price"])
    if h.get("t_base_shares"):
        parts.append("做T底仓 %d" % h["t_base_shares"])
    if h.get("note"):
        parts.append(h["note"])
    return " · ".join(parts)


@get("/api/book")
def api_book(query):
    """持仓/自选页顺带展示现价。取价失败不影响账本本身：行情只是锦上添花，
    不能因为网络问题让人连自己录的持仓都看不到；但也不能静默，失败原因放进 trouble。"""
    import portfolio_book
    holdings = portfolio_book.load("holdings")
    watchlist = portfolio_book.load("watchlist")
    quotes, trouble = {}, ""
    symbols = sorted({r["symbol"] for r in holdings} | {r["symbol"] for r in watchlist})
    if symbols:
        try:
            import live_quote
            snapshot = live_quote.snapshot(symbols)
            quotes = {q["symbol"]: q for q in snapshot["quotes"]}
            if snapshot["failures"]:
                trouble = "%d 只股票未取得行情，现价显示为“—”：%s" % (
                    len(snapshot["failures"]), ",".join(f["symbol"] for f in snapshot["failures"][:5]))
        except (OSError, ValueError) as exc:
            trouble = "行情获取失败，只显示账本数据：%s" % exc

    rows, total_cost, total_value = [], 0.0, 0.0
    for h in holdings:
        q = quotes.get(h["symbol"])
        last = float(q["last"]) if q else None
        total_cost += h["cost_price"] * h["shares"]
        if last:
            total_value += last * h["shares"]
        rows.append({"symbol": h["symbol"], "name": h.get("name") or "", "shares": h["shares"],
                     "cost_price": h["cost_price"], "last": last,
                     "pct": (last / h["cost_price"] - 1) * 100 if last else None, "declared": _declared(h)})
    summary = None
    if total_cost and total_value:
        pnl = total_value - total_cost
        summary = {"cost": total_cost, "value": total_value, "pnl": pnl, "pnl_pct": pnl / total_cost * 100}

    watch_rows = []
    for w in watchlist:
        q = quotes.get(w["symbol"])
        watch_rows.append({"symbol": w["symbol"], "name": w.get("name") or "",
                           "intent_label": portfolio_book.INTENT_LABEL.get(w.get("intent"), w.get("intent", "")),
                           "last": float(q["last"]) if q else None,
                           "change_pct": float(q["change_pct"]) if q else None, "note": w.get("note") or ""})
    return {"holdings": rows, "watchlist": watch_rows, "summary": summary, "trouble": trouble,
            "hold_types": [{"value": k, "label": v} for k, v in portfolio_book.HOLD_TYPE_LABEL.items()],
            "intents": [{"value": k, "label": v} for k, v in portfolio_book.INTENT_LABEL.items()]}


def _book_action(fn, message):
    import portfolio_book
    try:
        fn()
    except portfolio_book.BookError as exc:
        raise ApiError(str(exc))
    except OSError as exc:
        raise ApiError("写入账本失败：%s" % exc, 500)
    return {"message": message}


@post("/api/book/holding")
def api_book_holding(body):
    import portfolio_book
    return _book_action(lambda: portfolio_book.add_holding(_strings(body)), "已保存持仓。")


@post("/api/book/watch")
def api_book_watch(body):
    import portfolio_book
    return _book_action(lambda: portfolio_book.add_watch(_strings(body)), "已加入自选。")


@post("/api/book/holding/remove")
def api_book_holding_remove(body):
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
    import sentinel_view
    return sentinel_view.sentinel_payload(query.get("day"))
