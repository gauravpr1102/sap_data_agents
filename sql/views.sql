-- Business rules for SAP planning and procurement, as views over SAP-shaped tables.
--
-- Placeholders written as double-braced names are filled from the client config by
-- sapkit/db.py; nothing client-specific is hard-coded here. Every number an
-- agent or a report shows comes from one of these views.
--
-- Dialect: DuckDB. Porting to BigQuery changes date arithmetic
-- (DATE_ADD/DATE_DIFF) and FILTER clauses (COUNTIF/IF), not the logic.

-- ---------------------------------------------------------------- scope
-- Which material-plants are planned at all.
CREATE OR REPLACE VIEW v_scope AS
SELECT c.matnr, c.werks, a.maktx, a.matkl, c.plifz, c.eisbe, c.ekgrp, c.beskz, c.dismm
FROM marc c
JOIN mara a USING (matnr)
WHERE c.beskz IN ({{procurement_types}})
  AND c.dismm NOT IN ({{exclude_mrp_types}})
  AND coalesce(c.mmsta, '') IN ({{active_statuses}})
  AND c.werks IN ({{plants}})
  -- Row-level security: the caller's plants and purchasing groups (empty for full-scope jobs).
  {{session_scope}};

-- ---------------------------------------------------------------- purchasing
-- Open PO schedule lines, with whether a shipping notification (EKES-EBTYP = 'LA')
-- exists for the item. An order acknowledgement ('AB') is NOT a shipping notice.
CREATE OR REPLACE VIEW v_po_lines AS
SELECT k.ebeln, p.ebelp, e.etenr, p.matnr, p.werks, k.lifnr, k.ekgrp, p.bstae,
       e.eindt,
       e.menge - e.wemng            AS open_qty,
       la.asn_date,
       la.ebeln IS NOT NULL         AS has_asn
FROM ekko k
JOIN ekpo p USING (ebeln)
JOIN eket e ON e.ebeln = p.ebeln AND e.ebelp = p.ebelp
LEFT JOIN (
    SELECT ebeln, ebelp, min(eindt) AS asn_date
    FROM ekes WHERE ebtyp = 'LA'
    GROUP BY ebeln, ebelp
) la ON la.ebeln = p.ebeln AND la.ebelp = p.ebelp
WHERE coalesce(p.loekz, '') = ''
  AND e.menge - e.wemng > 0;

-- ---------------------------------------------------------------- projection events
-- Stock today, counted supply, and requirements. Past-due items count today.
CREATE OR REPLACE VIEW v_events AS
WITH stock AS (
    SELECT matnr, werks, DATE '{{as_of}}' AS evt_date, sum(labst) AS qty, 'STOCK' AS kind
    FROM mard GROUP BY matnr, werks
),
supply AS (
    SELECT matnr, werks,
           greatest(CASE WHEN has_asn THEN asn_date ELSE eindt END, DATE '{{as_of}}') AS evt_date,
           open_qty AS qty, 'PO' AS kind
    FROM v_po_lines
    WHERE ('{{cover_rule}}' = 'all_open' OR has_asn)
      AND CASE WHEN has_asn THEN asn_date ELSE eindt END
          <= DATE '{{as_of}}' + INTERVAL {{horizon_days}} DAY
),
demand AS (
    SELECT matnr, werks, greatest(bdter, DATE '{{as_of}}') AS evt_date, -bdmng AS qty, 'REQ' AS kind
    FROM resb
    WHERE bdter <= DATE '{{as_of}}' + INTERVAL {{horizon_days}} DAY
)
SELECT e.* FROM (
    SELECT * FROM stock UNION ALL SELECT * FROM supply UNION ALL SELECT * FROM demand
) e
SEMI JOIN v_scope s ON s.matnr = e.matnr AND s.werks = e.werks;

-- End-of-day projected balance per material-plant.
CREATE OR REPLACE VIEW v_projection AS
SELECT matnr, werks, evt_date,
       sum(day_qty) OVER (PARTITION BY matnr, werks ORDER BY evt_date
                          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS balance
FROM (
    SELECT matnr, werks, CAST(evt_date AS DATE) AS evt_date, sum(qty) AS day_qty
    FROM v_events GROUP BY matnr, werks, CAST(evt_date AS DATE)
);

-- ---------------------------------------------------------------- shortages
CREATE OR REPLACE VIEW v_shortage AS
WITH agg AS (
    SELECT matnr, werks,
           min(evt_date) FILTER (WHERE balance < 0) AS first_short_date,
           min(balance)                             AS min_balance
    FROM v_projection GROUP BY matnr, werks
),
recovery AS (
    SELECT p.matnr, p.werks, min(p.evt_date) AS recovery_date
    FROM v_projection p JOIN agg a USING (matnr, werks)
    WHERE a.first_short_date IS NOT NULL AND p.evt_date > a.first_short_date AND p.balance >= 0
    GROUP BY p.matnr, p.werks
),
parents AS (
    SELECT matnr, werks, count(DISTINCT baugr) AS parent_count FROM resb GROUP BY matnr, werks
),
sup AS (  -- the earliest counted supply after the short date, for the explanation
    SELECT p.matnr, p.werks, min(CASE WHEN p.has_asn THEN p.asn_date ELSE p.eindt END) AS next_firm_receipt
    FROM v_po_lines p JOIN agg a USING (matnr, werks)
    WHERE ('{{cover_rule}}' = 'all_open' OR p.has_asn)
      AND CASE WHEN p.has_asn THEN p.asn_date ELSE p.eindt END >= a.first_short_date
    GROUP BY p.matnr, p.werks
),
base AS (
    SELECT s.matnr, s.werks, s.maktx, s.matkl, s.ekgrp, s.plifz,
           a.first_short_date,
           datediff('day', DATE '{{as_of}}', a.first_short_date) AS days_to_short,
           -a.min_balance                                         AS shortfall_qty,
           r.recovery_date,
           sup.next_firm_receipt,
           coalesce(pa.parent_count, 0)                           AS parent_count,
           -- Standard price is per price unit (MBEW-PEINH). Dividing is not optional.
           CASE WHEN m.stprs > 0 THEN m.stprs / nullif(m.peinh, 0) END AS unit_cost
    FROM agg a
    JOIN v_scope s USING (matnr, werks)
    LEFT JOIN recovery r USING (matnr, werks)
    LEFT JOIN parents pa USING (matnr, werks)
    LEFT JOIN sup USING (matnr, werks)
    LEFT JOIN mbew m ON m.matnr = a.matnr AND m.bwkey = a.werks
    WHERE a.first_short_date IS NOT NULL
),
scored AS (
    SELECT *,
           round(shortfall_qty * unit_cost, 2) AS value_at_risk,
           CASE WHEN parent_count >= 6 THEN 3 WHEN parent_count >= 2 THEN 2 ELSE 1 END AS criticality,
           {{urgency_case}} AS urgency
    FROM base
),
exposed AS (
    -- Exposure ranked within the plant, so small plants are not drowned out.
    SELECT *, cume_dist() OVER (PARTITION BY werks ORDER BY coalesce(value_at_risk, 0)) AS exposure_pct
    FROM scored
)
SELECT *,
       round(exposure_pct * criticality * urgency, 3) AS risk_score,
       row_number() OVER (PARTITION BY werks
                          ORDER BY exposure_pct * criticality * urgency DESC, first_short_date, matnr) AS risk_rank
FROM exposed;

-- Balance dips into safety stock without going short.
CREATE OR REPLACE VIEW v_safety_stock_dip AS
SELECT s.matnr, s.werks, s.maktx, s.eisbe AS safety_stock, min(p.balance) AS lowest_balance,
       min(p.evt_date) FILTER (WHERE p.balance < s.eisbe) AS first_dip_date
FROM v_projection p JOIN v_scope s USING (matnr, werks)
GROUP BY s.matnr, s.werks, s.maktx, s.eisbe
HAVING s.eisbe > 0 AND min(p.balance) >= 0 AND min(p.balance) < s.eisbe;

-- ---------------------------------------------------------------- missing ASN
-- "Should have shipped and hasn't": the line requires a shipping notice, its
-- transit window has started, and no notice exists.
CREATE OR REPLACE VIEW v_missing_asn AS
SELECT p.ebeln, p.ebelp, p.matnr, p.werks, s.maktx, p.lifnr, l.name1 AS vendor_name, p.eindt,
       p.open_qty,
       coalesce(z.transit_days, {{default_transit_days}})                      AS transit_days,
       p.eindt - INTERVAL (coalesce(z.transit_days, {{default_transit_days}})) DAY AS ship_by_date,
       datediff('day', CAST(p.eindt - INTERVAL (coalesce(z.transit_days, {{default_transit_days}})) DAY AS DATE),
                DATE '{{as_of}}')                                              AS days_past_ship_by
FROM v_po_lines p
JOIN v_scope s USING (matnr, werks)
LEFT JOIN zlane z ON z.lifnr = p.lifnr AND z.werks = p.werks
LEFT JOIN lfa1 l ON l.lifnr = p.lifnr
WHERE p.bstae = '{{asn_key}}'
  AND NOT p.has_asn
  AND p.eindt - INTERVAL (coalesce(z.transit_days, {{default_transit_days}})) DAY <= DATE '{{as_of}}'
  AND p.eindt >= DATE '{{as_of}}' - INTERVAL {{lookback_days}} DAY;

-- ---------------------------------------------------------------- forecast change
CREATE OR REPLACE VIEW v_forecast_change AS
WITH w AS (
    SELECT f.matnr, f.werks, s.maktx, s.plifz, s.ekgrp,
           CASE WHEN f.pdatu <= DATE '{{as_of}}' + INTERVAL (s.plifz) DAY THEN 'INSIDE' ELSE 'OUTSIDE' END AS window_,
           coalesce(sum(f.plnmg) FILTER (WHERE f.capture_id = '{{capture_prev}}'), 0) AS prior_qty,
           coalesce(sum(f.plnmg) FILTER (WHERE f.capture_id = '{{capture_cur}}'), 0)  AS current_qty
    FROM forecast_snapshot f
    JOIN v_scope s USING (matnr, werks)
    WHERE f.capture_id IN ('{{capture_prev}}', '{{capture_cur}}')
      AND f.pdatu BETWEEN DATE '{{as_of}}' AND DATE '{{as_of}}' + INTERVAL {{horizon_days}} DAY
    GROUP BY ALL
)
SELECT *,
       current_qty - prior_qty AS change_qty,
       CASE WHEN prior_qty > 0 THEN round((current_qty - prior_qty) / prior_qty * 100, 1) END AS change_pct,
       CASE
         WHEN prior_qty = 0 AND current_qty > 0 THEN 7
         WHEN window_ = 'INSIDE'  AND abs((current_qty - prior_qty) / prior_qty * 100) >= {{hi_pct}} THEN 1
         WHEN window_ = 'INSIDE'  AND abs((current_qty - prior_qty) / prior_qty * 100) >= {{lo_pct}} THEN 2
         WHEN window_ = 'OUTSIDE' AND abs((current_qty - prior_qty) / prior_qty * 100) >= {{hi_pct}} THEN 3
         WHEN window_ = 'OUTSIDE' AND abs((current_qty - prior_qty) / prior_qty * 100) >= {{lo_pct}} THEN 4
         WHEN window_ = 'INSIDE'  THEN 5
         ELSE 6
       END AS change_rank,
       '{{capture_prev}}' AS capture_prev,
       '{{capture_cur}}'  AS capture_cur
FROM w
WHERE prior_qty > 0 OR current_qty > 0;

-- ---------------------------------------------------------------- supplier resolution
-- Precedence: one open-PO vendor; else one fixed, valid source-list vendor;
-- else one valid source-list vendor; otherwise ambiguous or none. Never guesses.
CREATE OR REPLACE VIEW v_supplier_candidates AS
WITH po AS (
    SELECT matnr, werks, count(DISTINCT lifnr) AS po_vendor_count, min(lifnr) AS po_vendor
    FROM v_po_lines GROUP BY matnr, werks
),
sl AS (
    SELECT matnr, werks,
           count(DISTINCT lifnr) FILTER (WHERE flifn = 'X') AS fixed_count,
           min(lifnr) FILTER (WHERE flifn = 'X')            AS fixed_vendor,
           count(DISTINCT lifnr)                            AS valid_count,
           min(lifnr)                                       AS valid_vendor
    FROM eord
    WHERE DATE '{{as_of}}' BETWEEN vdatu AND bdatu
    GROUP BY matnr, werks
)
SELECT s.matnr, s.werks,
       CASE WHEN po.po_vendor_count = 1 THEN 'OPEN_PO'
            WHEN po.po_vendor_count > 1 THEN 'AMBIGUOUS'
            WHEN sl.fixed_count = 1 THEN 'FIXED_SOURCE'
            WHEN sl.fixed_count > 1 THEN 'AMBIGUOUS'
            WHEN sl.valid_count = 1 THEN 'SOURCE_LIST'
            WHEN sl.valid_count > 1 THEN 'AMBIGUOUS'
            ELSE 'NONE' END AS resolution,
       CASE WHEN po.po_vendor_count = 1 THEN po.po_vendor
            WHEN po.po_vendor_count > 1 THEN NULL
            WHEN sl.fixed_count = 1 THEN sl.fixed_vendor
            WHEN sl.fixed_count > 1 THEN NULL
            WHEN sl.valid_count = 1 THEN sl.valid_vendor
       END AS vendor
FROM v_scope s
LEFT JOIN po USING (matnr, werks)
LEFT JOIN sl USING (matnr, werks);

-- Everything outreach eligibility needs, one row per changed material-plant.
CREATE OR REPLACE VIEW v_outreach_input AS
SELECT fc.*, s.matkl, sc.resolution, sc.vendor, l.name1 AS vendor_name,
       a6.smtp_addr AS vendor_email,
       t.eknam AS buyer_name, t.smtp_addr AS buyer_email,
       (v.matnr IS NOT NULL) AS is_vmi
FROM v_forecast_change fc
JOIN v_scope s USING (matnr, werks)
JOIN v_supplier_candidates sc USING (matnr, werks)
LEFT JOIN lfa1 l ON l.lifnr = sc.vendor
LEFT JOIN adr6 a6 ON a6.adrnr = l.adrnr
LEFT JOIN t024 t ON t.ekgrp = s.ekgrp
LEFT JOIN zvmi v ON v.matnr = fc.matnr AND v.werks = fc.werks;
