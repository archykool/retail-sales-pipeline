-- =====================================================================
-- schema.sql — star schema for the retail sales pipeline (SPEC §5)
--
-- Idempotent: every statement is IF NOT EXISTS, so applying this file to an
-- existing database is a no-op rather than an error. That matters because
-- PostgresLoader.create_tables() runs it on every pipeline run — a schema step
-- that must only run once is a schema step someone eventually runs twice.
--
-- Grain statement: one row in fact_sales = one validated order line, uniquely
-- identified by order_id.
--
-- Dimensions carry SERIAL surrogate keys with the natural business key kept as a
-- UNIQUE constraint. See D-005 — the deciding argument is not warehousing
-- convention but the dependency rule in §3.1: a surrogate key does not exist
-- until its dimension row is inserted, so producing one requires a database
-- query, so it can only happen in the loader. The transformer emits natural keys
-- because §3.1 leaves it nowhere else to put them.
-- =====================================================================


-- ---------------------------------------------------------------------
-- Run log. Not requested by the assignment; added because §8.2's
-- reconciliation has to be answerable per run, and "how do you know it's
-- correct" is unanswerable if you cannot say which run you are asking about.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS etl_run_log (
    run_id          UUID PRIMARY KEY,
    source_file     TEXT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,
    rows_extracted  INT,
    rows_valid      INT,
    rows_rejected   INT,
    rows_loaded     INT,
    -- SUCCESS only, and that is a consequence of the design rather than a gap.
    -- The whole load is one transaction (§7.4), so a RUNNING row would commit at the
    -- same instant as the row superseding it, and a FAILED run rolls back its own log
    -- entry along with everything else. Failed runs are visible in the log file and the
    -- process exit code, not here. See D-025.
    status          TEXT NOT NULL
);


-- ---------------------------------------------------------------------
-- Staging. Holds typed, validated rows — not raw text (SPEC Q5). A stricter
-- reading of "staging" would keep the original strings and validate downstream;
-- the assignment asks for a "staging table for valid sales records", so validity
-- is the entry condition here.
--
-- Keeps source_file and row_num: provenance survives validation (D-020), which
-- is what makes a failed reconciliation localisable to a line in a file rather
-- than merely detectable.
--
-- The CHECK constraints match fact_sales' deliberately. D-010's tripwire argument
-- applies *most* at the first point data lands, not the last: this is where
-- §8.2's first reconciliation check reads from, so a validator bug that reaches
-- staging has already corrupted the number the reconciliation trusts. Catching it
-- only at fact_sales would mean staging and facts disagree, with staging wrong.
--
-- Asymmetric constraints — permissive staging, strict facts — would be a coherent
-- design, describing a landing zone that accepts anything and cleans up later.
-- That is not this design. Per SPEC Q5, stg_sales holds *typed, validated*
-- records, and the constraints are here to say so out loud.
--
-- No additivity constraint here either, for exactly the reason given at
-- fact_sales below (D-024).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stg_sales (
    run_id        UUID NOT NULL,
    source_file   TEXT NOT NULL,
    row_num       INT  NOT NULL,
    order_id      INT  NOT NULL,
    order_date    DATE NOT NULL,
    customer_id   TEXT NOT NULL,
    product_id    TEXT NOT NULL,
    quantity      INT  NOT NULL CHECK (quantity > 0),
    unit_price    NUMERIC(12,2) NOT NULL CHECK (unit_price > 0),
    discount_rate NUMERIC(5,4)  NOT NULL CHECK (discount_rate >= 0 AND discount_rate < 1),
    loaded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- No index on stg_sales(source_file), and that is a decision rather than an
-- oversight. §7.3 deletes on this column on every run
-- (DELETE FROM stg_sales WHERE source_file = %s), so an index is the obvious
-- addition. At 200 rows per file a sequential scan is faster than an index
-- lookup plus the write cost of maintaining the index on every insert.
--
-- This is the first thing to add at real volume. Stated explicitly because a
-- silently missing index and a deliberately omitted one look identical in the
-- file and completely different when someone asks about it.


-- ---------------------------------------------------------------------
-- Dimensions. Overwrite-on-conflict, no SCD Type 2 (D-014) — the surrogate keys
-- leave the door open for versioning later without forcing it now.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_customers (
    customer_key  SERIAL PRIMARY KEY,
    customer_id   TEXT NOT NULL UNIQUE,
    customer_name TEXT NOT NULL,
    region        TEXT NOT NULL,
    segment       TEXT,
    signup_date   DATE,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dim_products (
    product_key   SERIAL PRIMARY KEY,
    product_id    TEXT NOT NULL UNIQUE,
    product_name  TEXT NOT NULL,
    category      TEXT NOT NULL,
    list_price    NUMERIC(12,2),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------
-- Fact table.
--
-- The CHECK constraints below deliberately duplicate rules the Python validator
-- already enforces (D-010). The redundancy is the point: if one of them ever
-- fires, the validator has a bug, and the database refusing the row is how that
-- bug becomes visible instead of becoming data.
--
-- NOTE — there is deliberately NO constraint of the form:
--
--     CHECK (gross_sales = discount_amount + net_sales)
--
-- It looks like an obvious integrity rule and it would reject three valid rows
-- in the committed dataset (rows 34, 76 and 118 of sales_2026_01.csv). Each
-- measure is rounded once to two places from its own exact value (§7.1), and
-- when both discount_amount and net_sales land on an exact half-cent they both
-- round up, so the three columns are each individually correct while their sum
-- is a cent apart. Additivity and per-column accuracy cannot both survive
-- rounding; the columns are aggregated independently, so per-column accuracy is
-- the property that matters. Decimal solves binary representation error, not
-- this. Full reasoning in D-024, and the expected -0.03 is asserted by the
-- -- RECONCILIATION block in analytics.sql.
--
-- Do not add this constraint. It is absent on purpose.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_sales (
    sales_key       BIGSERIAL PRIMARY KEY,
    order_id        INT NOT NULL UNIQUE,
    order_date      DATE NOT NULL,
    customer_key    INT NOT NULL REFERENCES dim_customers(customer_key),
    product_key     INT NOT NULL REFERENCES dim_products(product_key),
    quantity        INT NOT NULL CHECK (quantity > 0),
    unit_price      NUMERIC(12,2) NOT NULL CHECK (unit_price > 0),
    discount_rate   NUMERIC(5,4)  NOT NULL CHECK (discount_rate >= 0 AND discount_rate < 1),
    gross_sales     NUMERIC(14,2) NOT NULL,
    discount_amount NUMERIC(14,2) NOT NULL,
    net_sales       NUMERIC(14,2) NOT NULL,
    run_id          UUID NOT NULL,
    loaded_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_fact_sales_date ON fact_sales(order_date);
CREATE INDEX IF NOT EXISTS ix_fact_sales_cust ON fact_sales(customer_key);


-- ---------------------------------------------------------------------
-- Rejected-record audit table.
--
-- raw_payload is JSONB and holds the row exactly as it came off disk,
-- un-repaired. The rejected CSV written by RejectedRecordWriter serialises the
-- same structure, so the file and this table are two carriers of one audit
-- record rather than two formats of it.
--
-- No foreign key to anything: the whole point is that these rows failed to
-- resolve. A row rejected for UNKNOWN_CUSTOMER cannot reference dim_customers,
-- which is why it is here.
--
-- reason_code is its own column so rejections group in SQL without parsing
-- prose (D-013). reason_detail lists every surviving defect joined by " | ",
-- primary first (D-021, D-023).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS etl_rejected_sales (
    rejected_id   BIGSERIAL PRIMARY KEY,
    run_id        UUID NOT NULL,
    source_file   TEXT NOT NULL,
    row_num       INT  NOT NULL,
    raw_payload   JSONB NOT NULL,
    reason_code   TEXT NOT NULL,
    reason_detail TEXT,
    rejected_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
