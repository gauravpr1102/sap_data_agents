"""SQL rules against answers known by construction (DECISIONS D8)."""
from __future__ import annotations

import pytest

from sapkit import db


def mismatches(con, sql):
    return con.execute(sql).fetchall()


def test_every_scenario_is_present(con):
    scenarios = {r[0] for r in con.execute("SELECT DISTINCT scenario FROM expected_shortage").fetchall()}
    assert {"COVERED", "SHORT_NO_PO", "SHORT_UNCONFIRMED_PO", "SHORT_LATE_ASN", "COVERED_BY_ASN",
            "MISSING_ASN", "SAFETY_STOCK_DIP", "PAST_DUE_DEMAND", "SHORT_TWICE"} <= scenarios


def test_shortage_flag_matches_expected_asn_only(con):
    bad = mismatches(con, """
        SELECT e.matnr, e.werks, e.scenario FROM expected_shortage e
        LEFT JOIN v_shortage s USING (matnr, werks)
        WHERE (s.matnr IS NOT NULL) <> e.short_asn_only""")
    assert bad == []


def test_first_short_date_and_depth_match(con):
    bad = mismatches(con, """
        SELECT e.matnr, e.scenario, s.first_short_date, e.first_short_asn_only, s.shortfall_qty, e.shortfall_asn_only
        FROM expected_shortage e JOIN v_shortage s USING (matnr, werks)
        WHERE s.first_short_date <> e.first_short_asn_only OR abs(s.shortfall_qty - e.shortfall_asn_only) > 1e-6""")
    assert bad == []


def test_all_open_rule_matches_its_own_expectation(cfg, workdir):
    con = db.connect(cfg.with_overrides(cover_rule="all_open"), workdir / "db.duckdb")
    bad = mismatches(con, """
        SELECT e.matnr, e.scenario FROM expected_shortage e LEFT JOIN v_shortage s USING (matnr, werks)
        WHERE (s.matnr IS NOT NULL) <> e.short_all_open
           OR (e.short_all_open AND (s.first_short_date <> e.first_short_all_open
                                     OR abs(s.shortfall_qty - e.shortfall_all_open) > 1e-6))""")
    con.close()
    assert bad == []


def test_asn_only_reveals_hidden_shortages(cfg, con, workdir):
    asn_only = con.execute("SELECT count(*) FROM v_shortage").fetchone()[0]
    c2 = db.connect(cfg.with_overrides(cover_rule="all_open"), workdir / "db.duckdb")
    all_open = c2.execute("SELECT count(*) FROM v_shortage").fetchone()[0]
    c2.close()
    hidden = con.execute("SELECT count(*) FROM expected_shortage WHERE scenario = 'SHORT_UNCONFIRMED_PO'").fetchone()[0]
    assert asn_only - all_open == hidden > 0


def test_missing_asn_matches_expected(con):
    bad = mismatches(con, """
        SELECT e.matnr, e.werks FROM expected_shortage e
        LEFT JOIN (SELECT DISTINCT matnr, werks FROM v_missing_asn) m USING (matnr, werks)
        WHERE (m.matnr IS NOT NULL) <> e.missing_asn""")
    assert bad == []


def test_order_acknowledgement_is_not_a_shipping_notice(con):
    # Lines with only an AB confirmation must never count as ASN-covered.
    n = con.execute("""
        SELECT count(*) FROM v_po_lines p
        WHERE p.has_asn AND NOT EXISTS (SELECT 1 FROM ekes e WHERE e.ebeln = p.ebeln AND e.ebtyp = 'LA')""").fetchone()[0]
    assert n == 0
    assert con.execute("SELECT count(*) FROM ekes WHERE ebtyp = 'AB'").fetchone()[0] > 0


def test_safety_stock_dip_matches_expected(con):
    bad = mismatches(con, """
        SELECT e.matnr FROM expected_shortage e LEFT JOIN v_safety_stock_dip d USING (matnr, werks)
        WHERE (d.matnr IS NOT NULL) <> e.ss_dip""")
    assert bad == []


def test_forecast_rank_matches_expected(con):
    bad = mismatches(con, """
        SELECT e.matnr, e.window_, e.expected_rank, f.change_rank, f.window_ FROM expected_forecast e
        LEFT JOIN v_forecast_change f USING (matnr, werks)
        WHERE f.change_rank IS DISTINCT FROM e.expected_rank OR f.window_ <> e.window_""")
    assert bad == []
    ranks = {r[0] for r in con.execute("SELECT DISTINCT change_rank FROM v_forecast_change").fetchall()}
    assert ranks == {1, 2, 3, 4, 5, 6, 7}


def test_older_capture_is_ignored(con):
    assert con.execute("SELECT count(*) FROM v_forecast_change WHERE current_qty = 999999 OR prior_qty = 999999").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM forecast_snapshot WHERE plnmg = 999999").fetchone()[0] > 0


def test_out_of_scope_materials_never_appear(con):
    oos = con.execute("SELECT count(*) FROM marc WHERE beskz <> 'F' OR dismm = 'ND'").fetchone()[0]
    assert oos > 0
    leaked = con.execute("""SELECT count(*) FROM v_shortage s JOIN marc m USING (matnr, werks)
                            WHERE m.beskz <> 'F' OR m.dismm = 'ND'""").fetchone()[0]
    assert leaked == 0


def test_value_at_risk_divides_by_price_unit(con):
    rows = db.rows(con, """SELECT s.shortfall_qty, s.value_at_risk, m.stprs, m.peinh
                           FROM v_shortage s JOIN mbew m ON m.matnr = s.matnr AND m.bwkey = s.werks
                           WHERE m.peinh = 100 LIMIT 50""")
    assert rows
    for r in rows:
        assert r["value_at_risk"] == pytest.approx(round(r["shortfall_qty"] * r["stprs"] / 100, 2), abs=0.01)


def test_parent_count_matches_expected(con):
    bad = mismatches(con, """SELECT e.matnr FROM expected_shortage e JOIN v_shortage s USING (matnr, werks)
                             WHERE s.parent_count <> e.parent_count""")
    assert bad == []


def test_risk_rank_is_contiguous_per_plant(con):
    for werks, n, mx, mn in con.execute("SELECT werks, count(*), max(risk_rank), min(risk_rank) FROM v_shortage GROUP BY werks").fetchall():
        assert (mn, mx) == (1, n), werks


def test_supplier_resolution_never_guesses(con):
    # Ambiguous or empty resolutions must not carry a vendor.
    n = con.execute("SELECT count(*) FROM v_supplier_candidates "
                    "WHERE resolution IN ('AMBIGUOUS', 'NONE') AND vendor IS NOT NULL").fetchone()[0]
    assert n == 0


def test_expired_source_list_records_are_ignored(con):
    expired = con.execute("SELECT count(*) FROM eord WHERE bdatu < DATE '2026-10-14'").fetchone()[0]
    assert expired > 0
    bad = con.execute("""SELECT count(*) FROM v_supplier_candidates sc JOIN eord e ON e.matnr = sc.matnr AND e.werks = sc.werks
                         WHERE sc.resolution = 'FIXED_SOURCE' AND e.lifnr = sc.vendor AND e.bdatu < DATE '2026-10-14'""").fetchone()[0]
    assert bad == 0
