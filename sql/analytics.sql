-- =====================================================================
-- analytics.sql — the five business questions, plus the evidence that the
-- answers are trustworthy.
--
-- The five queries come from the assignment's Project Scenario (p.1), not
-- from its Step 13, which says only "should verify: if the data is correct".
-- That sentence is answered by the RECONCILIATION block at the bottom, and
-- it is the more interesting half of this file: any pipeline can produce
-- numbers, and the question is why you would believe them.
--
-- Run top to bottom in DBeaver or psql. Every statement is read-only.
--
-- Every query reads fact_sales unscoped rather than filtering on run_id.
-- That is correct because §7.3 makes a reload replace a file's rows rather
-- than append them, so the table holds one row per order line regardless of
-- how many times the file has been processed. The RECONCILIATION block does
-- scope to a run, because that is a question about a run.
-- =====================================================================


-- ---------------------------------------------------------------------
-- Q1. Revenue per day
--
-- net_sales, not gross: the discount has already been given away, so gross
-- would overstate what the business actually earned. Row count alongside it
-- so a spike in revenue can be told apart from a spike in volume.
-- ---------------------------------------------------------------------
SELECT
    order_date,
    count(*)                        AS order_lines,
    sum(quantity)                   AS units,
    sum(gross_sales)                AS gross_sales,
    sum(discount_amount)            AS discount_given,
    sum(net_sales)                  AS net_revenue
FROM fact_sales
GROUP BY order_date
ORDER BY order_date;


-- ---------------------------------------------------------------------
-- Q2. Top products by revenue
--
-- Joins through product_key to recover the business ID and name. This join
-- is D-005's cost, paid once per query: the fact table stores product_key = 7,
-- and nobody wants to read a report about product 7.
--
-- Ranked by net_sales rather than units, because "top" for a business
-- question means money. Units are shown so a high-volume, low-margin product
-- is distinguishable from the reverse.
-- ---------------------------------------------------------------------
SELECT
    p.product_id,
    p.product_name,
    p.category,
    sum(f.quantity)                 AS units_sold,
    sum(f.net_sales)                AS net_revenue,
    round(avg(f.discount_rate), 4)  AS avg_discount_rate
FROM fact_sales f
JOIN dim_products p USING (product_key)
GROUP BY p.product_id, p.product_name, p.category
ORDER BY net_revenue DESC;


-- ---------------------------------------------------------------------
-- Q3. Revenue by region
--
-- Region lives on the customer dimension, not on the fact, which is what
-- makes this a one-join question instead of a schema change. Adding "revenue
-- by segment" or "by signup cohort" costs nothing for the same reason — that
-- is the argument for the star schema, stated as something you can try.
-- ---------------------------------------------------------------------
SELECT
    c.region,
    count(DISTINCT c.customer_id)   AS customers,
    count(*)                        AS order_lines,
    sum(f.net_sales)                AS net_revenue,
    round(sum(f.net_sales) / sum(sum(f.net_sales)) OVER () * 100, 2) AS pct_of_total
FROM fact_sales f
JOIN dim_customers c USING (customer_key)
GROUP BY c.region
ORDER BY net_revenue DESC;


-- ---------------------------------------------------------------------
-- Q4. Highest-lifetime-value customer
--
-- "Lifetime" is honest only to the extent of the data loaded — one month
-- here. Say that out loud rather than letting the column name imply more
-- than it holds; the query is the same shape at any horizon.
--
-- Returns the ranking rather than just the winner, because a top customer
-- 2% ahead of the next is a different business fact from one 300% ahead.
-- ---------------------------------------------------------------------
SELECT
    c.customer_id,
    c.customer_name,
    c.region,
    c.segment,
    count(*)                        AS order_lines,
    sum(f.net_sales)                AS lifetime_value,
    min(f.order_date)               AS first_order,
    max(f.order_date)               AS last_order
FROM fact_sales f
JOIN dim_customers c USING (customer_key)
GROUP BY c.customer_id, c.customer_name, c.region, c.segment
ORDER BY lifetime_value DESC;


-- ---------------------------------------------------------------------
-- Q5. Rejected records grouped by reason_code
--
-- This is the query D-013 exists for. reason_code is its own column, so this
-- is a GROUP BY rather than a text search — and it answers a question counts
-- alone cannot: not "how much is broken" but "which kind of broken is
-- growing". A jump in NON_NUMERIC_CURRENCY means somebody changed an export
-- format; a jump in UNKNOWN_CUSTOMER means a reference feed is stale. Those
-- need different phone calls.
--
-- KEY_NORMALIZED never appears here: whitespace and casing on IDs are cleaned
-- and the record stays valid (§6.2), so it is logged rather than rejected.
-- SCHEMA_MISMATCH never appears either — it aborts the run before any row is
-- processed, so its evidence is the exception and its tests (D-004).
-- ---------------------------------------------------------------------
SELECT
    reason_code,
    count(*)                                                    AS rejections,
    round(100.0 * count(*) / sum(count(*)) OVER (), 1)          AS pct_of_rejections,
    min(row_num)                                                AS first_row,
    max(row_num)                                                AS last_row
FROM etl_rejected_sales
GROUP BY reason_code
ORDER BY rejections DESC, reason_code;


-- The same question one level down: the specific rows, with the raw values
-- that caused them. raw_payload is JSONB, so the offending field is reachable
-- with an operator rather than a regex over prose.
SELECT
    row_num,
    reason_code,
    raw_payload ->> 'order_id'      AS raw_order_id,
    raw_payload ->> 'quantity'      AS raw_quantity,
    raw_payload ->> 'unit_price'    AS raw_unit_price,
    raw_payload ->> 'discount_rate' AS raw_discount_rate,
    reason_detail
FROM etl_rejected_sales
ORDER BY row_num;


-- =====================================================================
-- RECONCILIATION — "how do you know the answer is correct?"
--
-- Four independent checks (§8.2) plus the additivity identity. They are
-- independent on purpose: each one can fail while the others pass, so
-- together they localise a fault rather than only detecting one.
-- =====================================================================


-- ---------------------------------------------------------------------
-- CHECK 1. Row conservation.
--
-- Every source row ended up in exactly one place. rows_extracted comes from
-- the run log, which the pipeline wrote from its own counts, and the other
-- two are counted from the tables — so this compares what the pipeline
-- believed against what the database contains.
--
-- Expected for sales_2026_01.csv: 200 = 172 + 28, and staged = facts = 172.
-- ---------------------------------------------------------------------
WITH latest AS (
    SELECT run_id, source_file, rows_extracted, rows_valid, rows_rejected, rows_loaded
    FROM etl_run_log
    ORDER BY started_at DESC
    LIMIT 1
)
SELECT
    l.source_file,
    l.rows_extracted,
    (SELECT count(*) FROM stg_sales          WHERE run_id = l.run_id) AS staged,
    (SELECT count(*) FROM fact_sales         WHERE run_id = l.run_id) AS facts,
    (SELECT count(*) FROM etl_rejected_sales WHERE run_id = l.run_id) AS rejected,
    l.rows_extracted
        = (SELECT count(*) FROM stg_sales          WHERE run_id = l.run_id)
        + (SELECT count(*) FROM etl_rejected_sales WHERE run_id = l.run_id)
                                                                     AS conservation_holds,
    (SELECT count(*) FROM stg_sales  WHERE run_id = l.run_id)
        = (SELECT count(*) FROM fact_sales WHERE run_id = l.run_id)  AS staged_equals_facts
FROM latest l;


-- ---------------------------------------------------------------------
-- CHECK 2. Control totals, recomputed independently.
--
-- The strongest check in the file. It recalculates all three measures in SQL
-- from the *staged inputs* — quantity, unit_price, discount_rate — and
-- compares them row by row to what the Python transformer stored. Any
-- disagreement means the transformer's arithmetic is wrong, and no row count
-- would reveal it. Two independent implementations of §7.1, in two languages,
-- agreeing on 172 rows.
--
-- What this check does NOT verify, despite appearances: the rounding mode.
-- ROUND_HALF_UP and banker's rounding differ only on an exact half-cent whose
-- preceding digit is even, and this dataset contains no such value — the three
-- half-cent rows are all 17.475, where the 7 makes both modes round up. So a
-- transformer using ROUND_HALF_EVEN would pass this check unchanged. The mode
-- is pinned by a unit test instead (test_rounding_is_half_up_not_half_even,
-- which uses 12.50 x 0.01 = 0.125 -> 0.13 not 0.12). Worth stating rather than
-- letting the check look stronger than it is.
--
-- The last two columns are the same D-024 phenomenon at aggregate scale:
--
--   recomputed_net    sums the per-row ROUNDED values, and must equal
--                     stored_net exactly — this is the control total.
--   exact_then_rounded sums the per-row EXACT values and rounds once at the
--                     end, giving 51107.10. It differs from stored_net by
--                     exactly the 0.03 from rows 34, 76 and 118, because
--                     rounding 172 times and rounding once are not the same
--                     operation. Neither figure is wrong; they answer
--                     different questions, and the stored one is correct
--                     because each column is aggregated independently.
--
-- Expected: mismatched_rows = 0, stored_net = recomputed_net = 51107.07,
--           exact_then_rounded = 51107.10.
-- ---------------------------------------------------------------------
SELECT
    count(*)                                            AS rows_compared,
    count(*) FILTER (
        WHERE f.gross_sales     <> round(s.quantity * s.unit_price, 2)
           OR f.discount_amount <> round(s.quantity * s.unit_price * s.discount_rate, 2)
           OR f.net_sales       <> round(
                  s.quantity * s.unit_price
                  - s.quantity * s.unit_price * s.discount_rate, 2)
    )                                                   AS mismatched_rows,
    sum(f.net_sales)                                    AS stored_net,
    sum(round(s.quantity * s.unit_price
              - s.quantity * s.unit_price * s.discount_rate, 2)) AS recomputed_net,
    sum(f.net_sales)
        = sum(round(s.quantity * s.unit_price
                    - s.quantity * s.unit_price * s.discount_rate, 2)) AS control_total_matches,
    round(sum(s.quantity * s.unit_price
              - s.quantity * s.unit_price * s.discount_rate), 2) AS exact_then_rounded
FROM fact_sales f
JOIN stg_sales  s ON s.order_id = f.order_id AND s.run_id = f.run_id;


-- ---------------------------------------------------------------------
-- CHECK 3. Referential integrity.
--
-- Zero orphans from the fact table to either dimension. The foreign keys
-- already make an orphan impossible to insert, so this is a check that the
-- constraints are still there — the same defence-in-depth argument as D-010.
--
-- Expected: 0, 0.
-- ---------------------------------------------------------------------
SELECT
    count(*) FILTER (WHERE c.customer_key IS NULL) AS orphan_customers,
    count(*) FILTER (WHERE p.product_key  IS NULL) AS orphan_products
FROM fact_sales f
LEFT JOIN dim_customers c ON c.customer_key = f.customer_key
LEFT JOIN dim_products  p ON p.product_key  = f.product_key;


-- ---------------------------------------------------------------------
-- CHECK 4. Never both, never neither.
--
-- The negative check: no source row appears in both stg_sales and
-- etl_rejected_sales, and none is missing from both. Joined on
-- (source_file, row_num) — the provenance pair D-020 exists for, and the only
-- key that identifies a source row independently of whether it was valid.
-- order_id cannot do this job: the rows hardest to account for are the ones
-- whose order_id was the defect.
--
-- Expected: 0 in both, 0 in neither.
-- ---------------------------------------------------------------------
WITH latest AS (
    SELECT run_id, source_file, rows_extracted FROM etl_run_log
    ORDER BY started_at DESC LIMIT 1
)
SELECT
    (SELECT count(*)
       FROM stg_sales s
       JOIN etl_rejected_sales r
         ON r.source_file = s.source_file AND r.row_num = s.row_num
      WHERE s.run_id = l.run_id)                                    AS in_both,
    l.rows_extracted
      - (SELECT count(*) FROM stg_sales          WHERE run_id = l.run_id)
      - (SELECT count(*) FROM etl_rejected_sales WHERE run_id = l.run_id)
                                                                    AS in_neither
FROM latest l;


-- ---------------------------------------------------------------------
-- CHECK 5. The additivity identity — expected to be -0.03, NOT zero.
--
-- SUM(gross) - SUM(discount) - SUM(net) does not come to zero, and that is
-- correct (D-024).
--
-- Each measure is rounded once to two places from its own exact value (§7.1).
-- Rows 34, 76 and 118 are all qty=5, price=34.95, rate=0.10, which produces
-- two exact half-cents at once:
--
--     gross        = 174.75            exact
--     discount_raw =  17.475  -> 17.48   rounds up
--     net_raw      = 157.275  -> 157.28  rounds up
--                    17.48 + 157.28 = 174.76, against a gross of 174.75
--
-- Each column is the correctly rounded value of its own quantity, which is
-- what matters because each is summed independently. Additivity and
-- per-column accuracy cannot both survive rounding — that is a property of
-- rounding, not a defect, and Decimal does not change it: Decimal fixes
-- binary representation error, which is a different problem.
--
-- Asserting the predicted -0.03 rather than zero is deliberate. A zero can be
-- produced by two errors cancelling, or by a check that is not really running.
-- A specific predicted non-zero can only be produced by the arithmetic being
-- exactly as documented.
--
-- The constant belongs to this dataset. Regenerating the demo data with
-- different prices or rates changes it.
-- ---------------------------------------------------------------------
SELECT
    sum(gross_sales)                                                AS gross,
    sum(discount_amount)                                            AS discount,
    sum(net_sales)                                                  AS net,
    sum(gross_sales) - sum(discount_amount) - sum(net_sales)        AS additivity_gap,
    sum(gross_sales) - sum(discount_amount) - sum(net_sales) = -0.03 AS gap_as_expected,
    count(*) FILTER (WHERE gross_sales <> discount_amount + net_sales) AS half_cent_rows
FROM fact_sales;


-- The three rows that contribute the cents, named. A total that is right for
-- the wrong reasons is the one thing a control total cannot catch alone.
SELECT
    s.row_num,
    f.quantity,
    f.unit_price,
    f.discount_rate,
    f.gross_sales,
    f.discount_amount,
    f.net_sales,
    f.gross_sales - f.discount_amount - f.net_sales AS row_gap
FROM fact_sales f
JOIN stg_sales  s ON s.order_id = f.order_id AND s.run_id = f.run_id
WHERE f.gross_sales <> f.discount_amount + f.net_sales
ORDER BY s.row_num;


-- =====================================================================
-- PREVIEW COMPARISON — mirrors preview_fact_sales.csv column for column.
--
-- The two joins ARE the surrogate-key mechanism, made visible: natural keys
-- are swapped for integer keys at load time (§7.2) and swapped back at query
-- time. Nothing else in the project shows both halves of that trade in one
-- statement.
--
-- The columns and their order match the dry-run artifact written by
-- FactPreviewWriter, so the file and the table can be read side by side. The
-- preview cannot contain customer_key and product_key: it is built from
-- FactSalesRecord, which carries natural keys because §3.1 leaves surrogate
-- resolution nowhere but the loader (D-005).
-- =====================================================================
SELECT c.customer_id, p.product_id, f.order_id, f.order_date,
       f.quantity, f.unit_price, f.discount_rate,
       f.gross_sales, f.discount_amount, f.net_sales
FROM fact_sales f
JOIN dim_customers c USING (customer_key)
JOIN dim_products  p USING (product_key)
ORDER BY f.order_id;
