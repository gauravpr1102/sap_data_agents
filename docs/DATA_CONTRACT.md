# Data contract

What a client's SAP extract must look like for `sapkit load`. Run `sapkit export
<dir>` to see the exact format, filled with the dummy client's data.

## Delivery

- One UTF-8 CSV per table, named `<table>.csv` (lower case), with a header row
  of SAP field names in lower case.
- Required files: `t001w, mara, marc, mard, mbew, ekko, ekpo, eket, resb,
  forecast_snapshot`. Optional files: `t024, lfa1, adr6, ekes, eord, zlane, zvmi`.
  A missing optional file reduces what the agent can answer; it doesn't stop the load.
- Extraction route is the client's choice: SAP Data Services, ODP / SLT, a
  scheduled ABAP report, or a manual download for a pilot.

## Tables and fields

| Table | SAP meaning | Fields (* = required) | Key |
|---|---|---|---|
| t001w | Plants | werks*, name1* | werks |
| t024 | Purchasing groups | ekgrp*, eknam*, smtp_addr | ekgrp |
| lfa1 | Vendor master | lifnr*, name1*, land1, adrnr | lifnr |
| adr6 | E-mail addresses | adrnr*, smtp_addr* | adrnr |
| mara | Material master | matnr*, maktx, mtart, matkl, meins* | matnr |
| marc | Plant data | matnr*, werks*, beskz*, dismm*, mmsta, plifz*, eisbe, ekgrp | matnr, werks |
| mard | Storage-location stock | matnr*, werks*, lgort*, labst* | matnr, werks, lgort |
| mbew | Valuation | matnr*, bwkey*, stprs, peinh*, vprsv | matnr, bwkey |
| ekko | PO header | ebeln*, lifnr*, ekgrp, bsart, aedat | ebeln |
| ekpo | PO item | ebeln*, ebelp*, matnr*, werks*, menge*, meins, bstae, loekz | ebeln, ebelp |
| eket | Schedule lines | ebeln*, ebelp*, etenr*, eindt*, menge*, wemng* | ebeln, ebelp, etenr |
| ekes | Confirmations (LA = shipping notice, AB = acknowledgement) | ebeln*, ebelp*, etens*, ebtyp*, eindt*, menge* | ebeln, ebelp, etens |
| eord | Source list | matnr*, werks*, zeord*, lifnr*, vdatu*, bdatu*, flifn | matnr, werks, zeord |
| zlane | Transit days per vendor/plant (client table) | lifnr*, werks*, transit_days* | lifnr, werks |
| zvmi | Vendor-managed materials (client table) | matnr*, werks* | matnr, werks |
| resb | Dependent requirements | rsnum*, rspos*, matnr*, werks*, bdter*, bdmng*, baugr | rsnum, rspos |
| forecast_snapshot | Forecast captures (e.g. from PBED / the planning tool) | capture_id*, captured_at*, matnr*, werks*, pdatu*, plnmg* | capture_id, matnr, werks, pdatu |

## Normalisation and rejection

| Rule | Behaviour |
|---|---|
| Material numbers | Digits only, left-padded to 18 (SAP's internal format). Non-numeric or longer than 18: **rejected** |
| Vendor numbers | Numeric values left-padded to 10 |
| Dates | `YYYY-MM-DD` or SAP `YYYYMMDD`; anything else: **rejected** |
| Numbers | Thousands separators allowed; non-numeric: **rejected** |
| Required field empty | **Rejected** |
| Duplicate key | Second and later rows: **rejected** |
| Blank optional text | Stored as empty; blank and missing are treated the same everywhere |

Rejected rows go to `out/load/rejected_<table>.csv` with a
`_rejected_because` column. The load summary prints counts and reasons per table.
Nothing is guessed or repaired silently.

## Data-quality checks

`sapkit dq` runs 12 checks and stores each result in `ops_dq_result`, so trends
can be followed over time.

| Id | Sev | Owner | Check |
|---|---|---|---|
| DQ-01 | P0 | SAP master data | Material numbers not 18 digits |
| DQ-02 | P0 | SAP inventory | Negative unrestricted stock |
| DQ-03 | P0 | Demand planning | Fewer than two forecast captures |
| DQ-04 | P1 | SAP master data | Planned materials without a standard price |
| DQ-05 | P1 | SAP master data | Price unit missing or unusual |
| DQ-06 | P1 | Purchasing | Purchasing groups in use with no buyer mailbox |
| DQ-07 | P1 | Purchasing | Vendors with open POs but no e-mail |
| DQ-08 | P1 | Purchasing | More than one fixed source |
| DQ-09 | P2 | Purchasing | Shortages with no resolvable supplier |
| DQ-10 | P2 | Supplier management | Open PO lines >30 days overdue with no shipping notice |
| DQ-11 | P3 | Logistics | Vendor/plant lanes with no transit time |
| DQ-12 | P3 | Production planning | Requirements >15 days past due |

**What the severities mean:**
- **P0** means answers are wrong until it's fixed; `sapkit dq` exits non-zero.
- **P1** means some answers are incomplete.
- **P2 and P3** are hygiene.

## Onboarding a client, step by step

1. Agree the extract route and cadence; deliver a first extract as CSVs.
2. Copy `config/acme.yaml` and set plants, scope, cover rule, thresholds and exclusions.
3. `sapkit load <dir>`, then fix rejected rows at source.
4. `sapkit dq` until there are no P0 issues.
5. Have planners confirm 10-20 materials by hand against what the tools say.
6. Add the client's users to `config/rbac.yaml`.
