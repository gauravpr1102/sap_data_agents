"""Load a client's SAP extract (docs/DATA_CONTRACT.md).

A client delivers one CSV per SAP table (``mara.csv``, ``marc.csv``, ...), with
a header row using the SAP field names in lower case. ``load`` validates every
row against the data contract and loads the good rows. It never guesses: a row
that breaks the contract is written to ``rejected_<table>.csv`` with the reason,
and the load summary says how many were rejected per table.

``export`` writes the current tables in exactly that format, so a client can
see what's expected (and so the loader can be tested on the dummy data).

Normalisation the contract applies:
- material numbers (MATNR) are digits, left-padded to 18 characters;
- vendor numbers (LIFNR) are left-padded to 10 when numeric;
- dates are ISO (YYYY-MM-DD) or SAP's YYYYMMDD; timestamps ISO;
- quantities and prices are numbers; empty means NULL only where allowed.
"""
from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

# column -> (type, required); type in: str, matnr, lifnr, date, ts, num, int, bool
C = tuple[str, bool]
CONTRACT: dict[str, dict[str, C]] = {
    "t001w": {"werks": ("str", True), "name1": ("str", True)},
    "t024": {"ekgrp": ("str", True), "eknam": ("str", True), "smtp_addr": ("str", False)},
    "lfa1": {"lifnr": ("lifnr", True), "name1": ("str", True), "land1": ("str", False), "adrnr": ("str", False)},
    "adr6": {"adrnr": ("str", True), "smtp_addr": ("str", True)},
    "mara": {"matnr": ("matnr", True), "maktx": ("str", False), "mtart": ("str", False),
             "matkl": ("str", False), "meins": ("str", True)},
    "marc": {"matnr": ("matnr", True), "werks": ("str", True), "beskz": ("str", True), "dismm": ("str", True),
             "mmsta": ("str", False), "plifz": ("int", True), "eisbe": ("num", False), "ekgrp": ("str", False)},
    "mard": {"matnr": ("matnr", True), "werks": ("str", True), "lgort": ("str", True), "labst": ("num", True)},
    "mbew": {"matnr": ("matnr", True), "bwkey": ("str", True), "stprs": ("num", False), "peinh": ("int", True),
             "vprsv": ("str", False)},
    "ekko": {"ebeln": ("str", True), "lifnr": ("lifnr", True), "ekgrp": ("str", False), "bsart": ("str", False),
             "aedat": ("date", False)},
    "ekpo": {"ebeln": ("str", True), "ebelp": ("str", True), "matnr": ("matnr", True), "werks": ("str", True),
             "menge": ("num", True), "meins": ("str", False), "bstae": ("str", False), "loekz": ("str", False)},
    "eket": {"ebeln": ("str", True), "ebelp": ("str", True), "etenr": ("str", True), "eindt": ("date", True),
             "menge": ("num", True), "wemng": ("num", True)},
    "ekes": {"ebeln": ("str", True), "ebelp": ("str", True), "etens": ("str", True), "ebtyp": ("str", True),
             "eindt": ("date", True), "menge": ("num", True)},
    "eord": {"matnr": ("matnr", True), "werks": ("str", True), "zeord": ("str", True), "lifnr": ("lifnr", True),
             "vdatu": ("date", True), "bdatu": ("date", True), "flifn": ("str", False)},
    "zlane": {"lifnr": ("lifnr", True), "werks": ("str", True), "transit_days": ("int", True)},
    "zvmi": {"matnr": ("matnr", True), "werks": ("str", True)},
    "resb": {"rsnum": ("str", True), "rspos": ("str", True), "matnr": ("matnr", True), "werks": ("str", True),
             "bdter": ("date", True), "bdmng": ("num", True), "baugr": ("str", False)},
    "forecast_snapshot": {"capture_id": ("str", True), "captured_at": ("ts", True), "matnr": ("matnr", True),
                          "werks": ("str", True), "pdatu": ("date", True), "plnmg": ("num", True)},
}
KEYS = {
    "t001w": ["werks"], "t024": ["ekgrp"], "lfa1": ["lifnr"], "adr6": ["adrnr"], "mara": ["matnr"],
    "marc": ["matnr", "werks"], "mard": ["matnr", "werks", "lgort"], "mbew": ["matnr", "bwkey"],
    "ekko": ["ebeln"], "ekpo": ["ebeln", "ebelp"], "eket": ["ebeln", "ebelp", "etenr"],
    "ekes": ["ebeln", "ebelp", "etens"], "eord": ["matnr", "werks", "zeord"], "zlane": ["lifnr", "werks"],
    "zvmi": ["matnr", "werks"], "resb": ["rsnum", "rspos"],
    "forecast_snapshot": ["capture_id", "matnr", "werks", "pdatu"],
}
REQUIRED_TABLES = ["t001w", "mara", "marc", "mard", "mbew", "ekko", "ekpo", "eket", "resb", "forecast_snapshot"]
SQL_TYPES = {"str": "VARCHAR", "matnr": "VARCHAR", "lifnr": "VARCHAR", "date": "DATE", "ts": "TIMESTAMP",
             "num": "DOUBLE", "int": "INTEGER", "bool": "BOOLEAN"}


class ContractError(ValueError):
    pass


def _parse(value: str, typ: str, required: bool):
    v = (value or "").strip()
    if v == "":
        if required:
            raise ContractError("required value is empty")
        return "" if typ == "str" else None
    if typ == "str":
        return v
    if typ == "matnr":
        if not v.isdigit():
            raise ContractError(f"material number {v!r} is not numeric")
        if len(v) > 18:
            raise ContractError(f"material number {v!r} is longer than 18 digits")
        return v.zfill(18)
    if typ == "lifnr":
        return v.zfill(10) if v.isdigit() else v
    if typ == "date":
        for fmt in ("%Y-%m-%d", "%Y%m%d"):
            try:
                return dt.datetime.strptime(v, fmt).date()
            except ValueError:
                pass
        raise ContractError(f"date {v!r} is not YYYY-MM-DD or YYYYMMDD")
    if typ == "ts":
        try:
            return dt.datetime.fromisoformat(v)
        except ValueError:
            raise ContractError(f"timestamp {v!r} is not ISO format") from None
    if typ == "num":
        try:
            return float(v.replace(",", ""))
        except ValueError:
            raise ContractError(f"{v!r} is not a number") from None
    if typ == "int":
        try:
            f = float(v)
        except ValueError:
            raise ContractError(f"{v!r} is not a whole number") from None
        if not f.is_integer():
            raise ContractError(f"{v!r} is not a whole number")
        return int(f)
    raise ContractError(f"unknown type {typ}")


@dataclass
class TableResult:
    table: str
    read: int = 0
    loaded: int = 0
    rejected: int = 0
    reasons: dict[str, int] = field(default_factory=dict)


def validate_table(table: str, path: Path, reject_dir: Path) -> tuple[list[tuple], TableResult]:
    contract = CONTRACT[table]
    res = TableResult(table)
    good: list[tuple] = []
    seen: set[tuple] = set()
    key_cols = KEYS[table]
    rejects: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = [h.strip().lower() for h in (reader.fieldnames or [])]
        missing = [c for c in contract if c not in header]
        if missing:
            raise ContractError(f"{path.name}: missing column(s) {', '.join(missing)}")
        for raw in reader:
            res.read += 1
            rec = {k.strip().lower(): v for k, v in raw.items() if k}
            try:
                values = {c: _parse(rec.get(c, ""), t, req) for c, (t, req) in contract.items()}
                key = tuple(values[k] for k in key_cols)
                if key in seen:
                    raise ContractError(f"duplicate key {dict(zip(key_cols, key, strict=True))}")
                seen.add(key)
                good.append(tuple(values[c] for c in contract))
            except ContractError as e:
                res.rejected += 1
                reason = str(e).split(" '")[0].split(" {")[0]
                res.reasons[reason] = res.reasons.get(reason, 0) + 1
                rejects.append({**rec, "_rejected_because": str(e)})
    if rejects:
        reject_dir.mkdir(parents=True, exist_ok=True)
        with open(reject_dir / f"rejected_{table}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rejects[0]))
            w.writeheader()
            w.writerows(rejects)
    res.loaded = len(good)
    return good, res


def load(source_dir: Path, db_path: Path, reject_dir: Path) -> list[TableResult]:
    source_dir = Path(source_dir)
    absent = [t for t in REQUIRED_TABLES if not (source_dir / f"{t}.csv").exists()]
    if absent:
        raise ContractError(f"missing required file(s): {', '.join(t + '.csv' for t in absent)}")
    validated: dict[str, list[tuple]] = {}
    results: list[TableResult] = []
    for table in CONTRACT:
        path = source_dir / f"{table}.csv"
        if not path.exists():
            validated[table] = []
            results.append(TableResult(table))
            continue
        good, res = validate_table(table, path, reject_dir)
        validated[table] = good
        results.append(res)
    if db_path.exists():
        db_path.unlink()
    import tempfile
    con = duckdb.connect(str(db_path))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for table, cols in CONTRACT.items():
                ddl = ", ".join(f"{c} {SQL_TYPES[t]}" for c, (t, _) in cols.items())
                con.execute(f"CREATE TABLE {table} ({ddl})")
                if not validated[table]:
                    continue
                # Bulk load through a clean CSV: row-by-row inserts take minutes at this volume.
                staged = Path(tmp) / f"{table}.csv"
                with open(staged, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f, quoting=csv.QUOTE_NONNUMERIC)
                    for r in validated[table]:
                        w.writerow([_NULL if v is None else (v.isoformat() if hasattr(v, "isoformat") else v)
                                    for v in r])
                con.execute(f"COPY {table} FROM '{staged.as_posix()}' (HEADER false, NULL '{_NULL}')")
    finally:
        con.close()
    return results


_NULL = "__NULL__"


def export(db_path: Path, out_dir: Path) -> dict[str, int]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=True)
    counts = {}
    try:
        for table, cols in CONTRACT.items():
            col_list = ", ".join(cols)
            data = con.execute(f"SELECT {col_list} FROM {table}").fetchall()
            with open(out_dir / f"{table}.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(list(cols))
                for r in data:
                    w.writerow(["" if v is None else (v.isoformat() if hasattr(v, "isoformat") else v) for v in r])
            counts[table] = len(data)
    finally:
        con.close()
    return counts
