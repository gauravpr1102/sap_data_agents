"""Facts the eval cases refer to as {name}, computed from the data in the user's scope."""
from __future__ import annotations

import datetime as dt

import duckdb

from .. import outreach
from ..config import ClientConfig


def _one(con, sql, params=None, default=None):
    try:
        r = con.execute(sql, params or []).fetchone()
    except duckdb.CatalogException:  # e.g. no answer-key tables in a client's real extract
        return default
    return r[0] if r and r[0] is not None else default


def _s(m: str | None) -> str:
    return (m or "").lstrip("0") or "-"


def _dates(prefix: str, d) -> dict:
    if d is None:
        return {f"{prefix}_iso": "-", f"{prefix}_long": "-", f"{prefix}_short": "-"}
    if isinstance(d, str):
        d = dt.date.fromisoformat(d)
    return {f"{prefix}_iso": d.isoformat(), f"{prefix}_long": d.strftime("%d %b %Y"),
            f"{prefix}_short": d.strftime("%b %d").replace(" 0", " ")}


def _n(prefix: str, n: int) -> dict:
    return {prefix: str(n), f"{prefix}_comma": f"{n:,}"}


def compute(con: duckdb.DuckDBPyConnection, cfg: ClientConfig, user: str) -> dict[str, str]:
    """Facts for one user. ``con`` must be connected with that user's session."""
    f: dict[str, str] = {}
    q = lambda sql, p=None, d=None: _one(con, sql, p, d)  # noqa: E731
    f["top_plant"] = q("SELECT werks FROM v_shortage GROUP BY werks ORDER BY count(*) DESC, werks LIMIT 1", d="-")
    f["top_value_plant"] = q("SELECT werks FROM v_shortage GROUP BY werks ORDER BY sum(value_at_risk) DESC LIMIT 1",
                             d="-")
    for p in cfg.plants:
        f[f"rank1_{p}"] = _s(q("SELECT matnr FROM v_shortage WHERE werks = ? AND risk_rank = 1", [p]))
    first = q("SELECT first_short_date FROM v_shortage WHERE werks = '1000' AND risk_rank = 1")
    f.update(_dates("rank1_1000_date", first))
    f.update(_n("n7_2000", q("SELECT count(*) FROM v_shortage WHERE werks = '2000' AND days_to_short <= 7", d=0)))
    f.update(_n("jordan_n7_1000", q("SELECT count(*) FROM v_shortage WHERE werks = '1000' AND days_to_short <= 7", d=0)))
    f["avery_n7_1000"] = str(q("SELECT count(*) FROM v_shortage WHERE werks = '1000' AND days_to_short <= 7", d=0))
    f["covered_3000"] = _s(q("""SELECT e.matnr FROM expected_shortage e JOIN v_scope s USING (matnr, werks)
                                WHERE e.werks = '3000' AND e.scenario = 'COVERED' ORDER BY e.matnr LIMIT 1"""))
    f["unconfirmed_1000"] = _s(q("""SELECT e.matnr FROM expected_shortage e JOIN v_scope s USING (matnr, werks)
                                    WHERE e.werks = '1000' AND e.scenario = 'SHORT_UNCONFIRMED_PO'
                                    ORDER BY e.matnr LIMIT 1"""))
    for p in cfg.plants:
        f.update(_n(f"masn_{p}", q("SELECT count(*) FROM v_missing_asn WHERE werks = ?", [p], 0)))
        f.update(_n(f"dips_{p}", q("SELECT count(*) FROM v_safety_stock_dip WHERE werks = ?", [p], 0)))
    f["fc1_1000"] = _s(q("""SELECT matnr FROM v_forecast_change WHERE werks = '1000' AND change_rank = 1
                            ORDER BY abs(change_pct) DESC, matnr LIMIT 1"""))
    f["fc7_2000"] = str(q("SELECT count(*) FROM v_forecast_change WHERE werks = '2000' AND change_rank = 7", d=0))
    f["capture_prev"] = q("SELECT any_value(capture_prev) FROM v_forecast_change", d="-")
    f["capture_cur"] = q("SELECT any_value(capture_cur) FROM v_forecast_change", d="-")
    dec2 = outreach.decide_all(con, cfg, ["2000"])
    f["vendors_2000"] = str(len({d["resolved_vendor"] for d in dec2 if d["disposition"] == "ELIGIBLE"}))
    f["any_vendor_2000"] = _s(next((d["resolved_vendor"] for d in dec2 if d["disposition"] == "ELIGIBLE"), None))
    dec3 = outreach.decide_all(con, cfg, ["3000"])
    f.update(_n("attn_3000", sum(1 for d in dec3 if d["disposition"] not in ("ELIGIBLE", "REPORT_ONLY"))))
    f.update(_n("noreply", q("""SELECT count(DISTINCT o.lifnr) FROM v_my_outreach o WHERE NOT EXISTS
                               (SELECT 1 FROM ops_reply_extract r WHERE r.lifnr = o.lifnr)""", d=0)))
    f.update(_n("review", q("SELECT count(*) FROM v_my_reply_extract WHERE needs_buyer_review", d=0)))
    f["cannot_meet"] = str(q("SELECT count(*) FROM v_my_reply_extract WHERE category = 'cannot_meet'", d=0))
    f["inj_vendor"] = _s(q("SELECT lifnr FROM v_my_reply_extract WHERE instructions_to_ai_detected ORDER BY lifnr LIMIT 1"))
    from ..rbac import load_rbac
    session = load_rbac().session_for(user, cfg)
    from ..workflow import review_queue
    f["avery_pending"] = str(review_queue(con, session, "PENDING_REVIEW", 1)[0])
    f["morgan_on_hold"] = str(review_queue(con, session, "ON_HOLD", 1)[0])
    f.update(_dates("as_of", cfg.as_of))
    return f
