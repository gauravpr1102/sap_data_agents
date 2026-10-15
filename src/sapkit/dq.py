"""Data-quality checks on the SAP data (docs/DATA_CONTRACT.md).

Each check returns the number of exception rows, with a severity and the team
that owns the fix. P0 means answers are wrong until it's fixed; P1 means some
answers are incomplete; P2/P3 are hygiene. ``sapkit dq`` runs them all and
stores the results in ops_dq_result, so trends are visible over time.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import duckdb


@dataclass(frozen=True)
class Check:
    id: str
    severity: str
    owner: str
    description: str
    sql: str          # returns exception rows
    population: str   # returns the number of rows checked


CHECKS = [
    Check("DQ-01", "P0", "SAP master data", "Material numbers that are not 18 digits",
          "SELECT matnr FROM mara WHERE length(matnr) <> 18 OR NOT regexp_matches(matnr, '^[0-9]+$')",
          "SELECT count(*) FROM mara"),
    Check("DQ-02", "P0", "SAP inventory", "Negative unrestricted stock",
          "SELECT matnr, werks FROM mard WHERE labst < 0", "SELECT count(*) FROM mard"),
    Check("DQ-03", "P0", "Demand planning", "Fewer than two forecast captures to compare",
          "SELECT 1 WHERE (SELECT count(DISTINCT capture_id) FROM forecast_snapshot) < 2",
          "SELECT 1"),
    Check("DQ-04", "P1", "SAP master data", "Planned materials without a standard price",
          "SELECT s.matnr, s.werks FROM v_scope s LEFT JOIN mbew m ON m.matnr = s.matnr AND m.bwkey = s.werks "
          "WHERE m.stprs IS NULL OR m.stprs <= 0", "SELECT count(*) FROM v_scope"),
    Check("DQ-05", "P1", "SAP master data", "Price unit (PEINH) missing or unusual",
          "SELECT matnr, bwkey FROM mbew WHERE peinh IS NULL OR peinh NOT IN (1, 10, 100, 1000)",
          "SELECT count(*) FROM mbew"),
    Check("DQ-06", "P1", "Purchasing", "Purchasing groups in use with no buyer mailbox",
          "SELECT DISTINCT s.ekgrp FROM v_scope s LEFT JOIN t024 t ON t.ekgrp = s.ekgrp "
          "WHERE t.smtp_addr IS NULL OR t.smtp_addr = ''", "SELECT count(DISTINCT ekgrp) FROM v_scope"),
    Check("DQ-07", "P1", "Purchasing", "Vendors with open POs but no e-mail on file",
          "SELECT DISTINCT p.lifnr FROM v_po_lines p JOIN lfa1 l ON l.lifnr = p.lifnr "
          "LEFT JOIN adr6 a ON a.adrnr = l.adrnr WHERE a.smtp_addr IS NULL",
          "SELECT count(DISTINCT lifnr) FROM v_po_lines"),
    Check("DQ-08", "P1", "Purchasing", "More than one fixed source on the source list",
          "SELECT matnr, werks FROM eord WHERE flifn = 'X' AND DATE '{as_of}' BETWEEN vdatu AND bdatu "
          "GROUP BY matnr, werks HAVING count(DISTINCT lifnr) > 1",
          "SELECT count(DISTINCT matnr || werks) FROM eord"),
    Check("DQ-09", "P2", "Purchasing", "Shortages with no resolvable supplier",
          "SELECT s.matnr, s.werks FROM v_shortage s JOIN v_supplier_candidates c USING (matnr, werks) "
          "WHERE c.resolution IN ('NONE', 'AMBIGUOUS')", "SELECT count(*) FROM v_shortage"),
    Check("DQ-10", "P2", "Supplier management", "Open PO lines more than 30 days overdue with no shipping notice",
          "SELECT ebeln, ebelp FROM v_po_lines WHERE NOT has_asn AND eindt < DATE '{as_of}' - INTERVAL 30 DAY",
          "SELECT count(*) FROM v_po_lines"),
    Check("DQ-11", "P3", "Logistics", "Vendor/plant lanes with no transit time (default used)",
          "SELECT DISTINCT p.lifnr, p.werks FROM v_po_lines p LEFT JOIN zlane z "
          "ON z.lifnr = p.lifnr AND z.werks = p.werks WHERE z.transit_days IS NULL AND p.bstae <> ''",
          "SELECT count(DISTINCT lifnr || werks) FROM v_po_lines"),
    Check("DQ-12", "P3", "Production planning", "Requirements more than 15 days past due",
          "SELECT rsnum, rspos FROM resb WHERE bdter < DATE '{as_of}' - INTERVAL 15 DAY",
          "SELECT count(*) FROM resb"),
]


def run_checks(con: duckdb.DuckDBPyConnection, as_of: str, record: bool = True) -> list[dict]:
    run_id = uuid.uuid4().hex[:12]
    out = []
    for c in CHECKS:
        exc = con.execute(f"SELECT count(*) FROM ({c.sql.format(as_of=as_of)})").fetchone()[0]
        pop = con.execute(c.population.format(as_of=as_of)).fetchone()[0]
        out.append({"check": c.id, "severity": c.severity, "owner": c.owner, "description": c.description,
                    "exceptions": exc, "checked": pop, "status": "PASS" if exc == 0 else "FAIL"})
        if record:
            con.execute("INSERT INTO ops_dq_result (run_id, check_id, severity, owner, description, exceptions, "
                        "checked_rows) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [run_id, c.id, c.severity, c.owner, c.description, exc, pop])
    return out


def blocking_failures(results: list[dict]) -> list[dict]:
    return [r for r in results if r["severity"] == "P0" and r["exceptions"] > 0]
