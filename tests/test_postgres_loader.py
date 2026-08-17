"""
Tests for `PostgresLoader` (Step 8b) — the whole pipeline, into a real database.

Step 8b's exit criterion is that a hand-run load populates all six tables and §8.2's
reconciliation balances, including the expected `-0.03` from D-024. The tests below are
that run, automated, against a disposable schema.

Three of them are the ones worth showing on camera:

- `test_reconciliation_balances` — §8.2's four checks in one place, including the
  predicted non-zero. A predicted non-zero is stronger evidence than a zero, which can
  come from two errors cancelling or from a check that silently is not running.
- `test_running_twice_leaves_totals_unchanged` — §7.3's idempotency claim.
- `test_stale_fact_is_removed_when_a_row_leaves_the_file` — the case naive
  delete-by-current-order-ids gets wrong.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")

from src.extractors import CSVExtractor, JSONExtractor  # noqa: E402
from src.loaders import DatabaseConnection, PostgresLoader  # noqa: E402
from src.models import PipelineResult  # noqa: E402
from src.transformers import (  # noqa: E402
    ReferenceDataTransformer,
    SalesDataTransformer,
)
from src.validators import SalesDataValidator, period_from_filename  # noqa: E402

from .conftest import RAW_DIR  # noqa: E402

SOURCE_FILE = "sales_2026_01.csv"
TODAY = date(2026, 8, 17)

EXPECTED_VALID = 172
EXPECTED_REJECTED = 28
EXPECTED_NET = Decimal("51107.07")
EXPECTED_GROSS = Decimal("58328.37")
EXPECTED_DISCOUNT = Decimal("7221.33")
# D-024: rows 34, 76 and 118 each contribute a cent.
EXPECTED_ADDITIVITY_GAP = Decimal("-0.03")


def build_run():
    """Run everything up to the load, in §7.0's order. No database involved yet."""
    reference = ReferenceDataTransformer()
    customers = reference.to_customers(
        JSONExtractor(RAW_DIR / "customers.json").extract()
    )
    products = reference.to_products(JSONExtractor(RAW_DIR / "products.json").extract())

    valid, rejected = SalesDataValidator(
        reference.customer_ids(customers),
        reference.product_ids(products),
        today=TODAY,
        period=period_from_filename(SOURCE_FILE),
    ).validate(CSVExtractor(RAW_DIR / SOURCE_FILE).extract())

    facts = SalesDataTransformer().to_facts(valid)
    return customers, products, valid, rejected, facts


def load_once(db_params: dict, schema: str, *, batch_size: int = 1000) -> dict:
    """One complete load into the given schema, returning the counts it reported.

    The ordering here is the ordering: clear the previous load, dimensions, then staging,
    facts and rejections. Dimensions must precede facts because the surrogate keys facts
    need do not exist until the dimension rows do (§7.2).
    """
    customers, products, valid, rejected, facts = build_run()
    run_id = uuid4()
    started = datetime.now()

    with DatabaseConnection(**db_params) as connection:
        connection.execute(f'SET search_path TO "{schema}"')
        loader = PostgresLoader(connection, batch_size=batch_size)
        loader.create_tables()

        loader.delete_previous_load(SOURCE_FILE, [fact.order_id for fact in facts])

        customer_keys = loader.upsert_dim_customers(customers)
        product_keys = loader.upsert_dim_products(products)

        staged = loader.load_staging(valid, run_id)
        loaded = loader.load_facts(facts, run_id, customer_keys, product_keys)
        quarantined = loader.load_rejected(rejected, run_id)

        result = PipelineResult(
            run_id=run_id,
            rows_extracted=len(valid) + len(rejected),
            rows_valid=len(valid),
            rows_rejected=len(rejected),
            rows_loaded=loaded,
            duration_seconds=0.0,
            dry_run=False,
            status="SUCCESS",
        )
        loader.write_run_log(
            result,
            source_file=SOURCE_FILE,
            started_at=started,
            finished_at=datetime.now(),
        )

    return {
        "run_id": run_id,
        "staged": staged,
        "loaded": loaded,
        "rejected": quarantined,
        "customer_keys": customer_keys,
        "product_keys": product_keys,
    }


def query(db_params: dict, schema: str, sql: str, params=None):
    with psycopg.connect(**db_params) as connection:
        connection.execute(f'SET search_path TO "{schema}"')
        return connection.execute(sql, params).fetchall()


def scalar(db_params: dict, schema: str, sql: str, params=None):
    return query(db_params, schema, sql, params)[0][0]


@pytest.fixture
def loaded(db_params: dict, schema: str) -> dict:
    """One completed load, shared by the assertions below."""
    return load_once(db_params, schema)


# ======================================================================
# All six tables populated — Step 8b's exit criterion
# ======================================================================


def test_all_six_tables_are_populated(db_params: dict, schema: str, loaded: dict) -> None:
    counts = {
        table: scalar(db_params, schema, f"SELECT count(*) FROM {table}")
        for table in (
            "etl_run_log",
            "stg_sales",
            "dim_customers",
            "dim_products",
            "fact_sales",
            "etl_rejected_sales",
        )
    }

    assert counts == {
        "etl_run_log": 1,
        "stg_sales": EXPECTED_VALID,
        "dim_customers": 20,
        "dim_products": 15,
        "fact_sales": EXPECTED_VALID,
        "etl_rejected_sales": EXPECTED_REJECTED,
    }


def test_loader_reports_what_it_actually_wrote(
    db_params: dict, schema: str, loaded: dict
) -> None:
    """Returned counts must match the database, or the run summary is fiction."""
    assert loaded["staged"] == scalar(db_params, schema, "SELECT count(*) FROM stg_sales")
    assert loaded["loaded"] == scalar(db_params, schema, "SELECT count(*) FROM fact_sales")
    assert loaded["rejected"] == scalar(
        db_params, schema, "SELECT count(*) FROM etl_rejected_sales"
    )


# ======================================================================
# §8.2 — the reconciliation
# ======================================================================


def test_reconciliation_balances(db_params: dict, schema: str, loaded: dict) -> None:
    """All four §8.2 checks, plus D-024's predicted non-zero.

    1. Row conservation — extracted == valid + rejected, and valid == count(stg_sales).
    2. Control totals — the Python figure equals the database figure to the cent.
    3. Referential integrity — zero orphans from fact_sales to either dimension.
    4. Every source row appears in facts or rejections, never both, never neither.

    The fifth assertion is the interesting one: gross - discount - net is expected to be
    -0.03, not zero (D-024). Asserting the predicted value rather than zero is what makes
    this evidence — a zero could come from two errors cancelling, or from a query that is
    not measuring what it claims.
    """
    # 1. row conservation
    staged = scalar(db_params, schema, "SELECT count(*) FROM stg_sales")
    facts = scalar(db_params, schema, "SELECT count(*) FROM fact_sales")
    rejected = scalar(db_params, schema, "SELECT count(*) FROM etl_rejected_sales")
    assert staged == facts == EXPECTED_VALID
    assert staged + rejected == EXPECTED_VALID + EXPECTED_REJECTED == 200

    # 2. control totals
    assert scalar(db_params, schema, "SELECT sum(net_sales) FROM fact_sales") == EXPECTED_NET
    assert scalar(db_params, schema, "SELECT sum(gross_sales) FROM fact_sales") == EXPECTED_GROSS
    assert (
        scalar(db_params, schema, "SELECT sum(discount_amount) FROM fact_sales")
        == EXPECTED_DISCOUNT
    )

    # 3. referential integrity
    orphans = scalar(
        db_params,
        schema,
        "SELECT count(*) FROM fact_sales f"
        " LEFT JOIN dim_customers c ON c.customer_key = f.customer_key"
        " LEFT JOIN dim_products  p ON p.product_key  = f.product_key"
        " WHERE c.customer_key IS NULL OR p.product_key IS NULL",
    )
    assert orphans == 0

    # 4. never both, never neither
    both = scalar(
        db_params,
        schema,
        "SELECT count(*) FROM stg_sales s"
        " JOIN etl_rejected_sales r"
        "   ON r.source_file = s.source_file AND r.row_num = s.row_num",
    )
    assert both == 0

    # 5. the expected -0.03 (D-024)
    gap = scalar(
        db_params,
        schema,
        "SELECT sum(gross_sales) - sum(discount_amount) - sum(net_sales) FROM fact_sales",
    )
    assert gap == EXPECTED_ADDITIVITY_GAP


def test_the_three_cents_are_the_documented_rows(
    db_params: dict, schema: str, loaded: dict
) -> None:
    """The -0.03 must come from rows 34, 76 and 118, not from three other rows.

    A total that happens to be right for the wrong reasons is the failure mode a control
    total cannot catch on its own, so the rows are named.
    """
    rows = query(
        db_params,
        schema,
        "SELECT s.row_num FROM fact_sales f"
        " JOIN stg_sales s ON s.order_id = f.order_id"
        " WHERE f.gross_sales <> f.discount_amount + f.net_sales"
        " ORDER BY s.row_num",
    )

    assert [row[0] for row in rows] == [34, 76, 118]


# ======================================================================
# Surrogate keys (§7.2, D-005)
# ======================================================================


def test_surrogate_keys_resolve_back_to_natural_keys(
    db_params: dict, schema: str, loaded: dict
) -> None:
    """The `-- PREVIEW COMPARISON` join at Step 13, in miniature.

    Natural keys go in, integer keys come out, and the join puts them back. That round trip
    is the whole surrogate-key mechanism and the reason D-005's cost is a join.
    """
    rows = query(
        db_params,
        schema,
        "SELECT c.customer_id, p.product_id, f.net_sales FROM fact_sales f"
        " JOIN dim_customers c USING (customer_key)"
        " JOIN dim_products  p USING (product_key)"
        " JOIN stg_sales s ON s.order_id = f.order_id"
        " WHERE s.row_num = 40",
    )

    assert len(rows) == 1
    customer_id, product_id, net = rows[0]
    assert customer_id == "C019"
    assert product_id == "P009"
    assert net == Decimal("90.00")  # the golden row


def test_fact_table_stores_no_natural_keys(
    db_params: dict, schema: str, loaded: dict
) -> None:
    """fact_sales holds keys, not business IDs — otherwise the schema is not a star."""
    columns = {
        row[0]
        for row in query(
            db_params,
            schema,
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = %s AND table_name = 'fact_sales'",
            (schema,),
        )
    }

    assert {"customer_key", "product_key"} <= columns
    assert not {"customer_id", "product_id"} & columns


def test_upsert_returns_keys_on_the_second_run(db_params: dict, schema: str) -> None:
    """The `DO NOTHING` trap, asserted.

    `ON CONFLICT DO NOTHING` returns no row when it conflicts, so on a second run
    `RETURNING` yields nothing for every existing customer and the key map comes back
    empty — a bug that appears only on rerun. `DO UPDATE` always returns.
    """
    first = load_once(db_params, schema)
    second = load_once(db_params, schema)

    assert len(second["customer_keys"]) == 20
    assert len(second["product_keys"]) == 15
    assert second["customer_keys"] == first["customer_keys"]


def test_missing_dimension_key_raises_rather_than_dropping_the_fact(
    db_params: dict, schema: str
) -> None:
    """Loading facts before dimensions is a programmer error and must be loud.

    Silently skipping the fact would make the reconciliation fail with nothing to point at.
    """
    _, _, _, _, facts = build_run()

    with pytest.raises(KeyError, match="no surrogate key"):
        with DatabaseConnection(**db_params) as connection:
            connection.execute(f'SET search_path TO "{schema}"')
            loader = PostgresLoader(connection)
            loader.create_tables()
            loader.load_facts(facts, uuid4(), {}, {})


# ======================================================================
# §7.3 — idempotency
# ======================================================================


def test_running_twice_leaves_totals_unchanged(db_params: dict, schema: str) -> None:
    """The most convincing fifteen seconds available (§7.3), asserted rather than demoed."""
    load_once(db_params, schema)
    first_net = scalar(db_params, schema, "SELECT sum(net_sales) FROM fact_sales")
    first_facts = scalar(db_params, schema, "SELECT count(*) FROM fact_sales")

    load_once(db_params, schema)
    second_net = scalar(db_params, schema, "SELECT sum(net_sales) FROM fact_sales")
    second_facts = scalar(db_params, schema, "SELECT count(*) FROM fact_sales")

    assert first_net == second_net == EXPECTED_NET
    assert first_facts == second_facts == EXPECTED_VALID


def test_running_twice_does_not_duplicate_rejections(
    db_params: dict, schema: str
) -> None:
    load_once(db_params, schema)
    load_once(db_params, schema)

    assert (
        scalar(db_params, schema, "SELECT count(*) FROM etl_rejected_sales")
        == EXPECTED_REJECTED
    )
    assert scalar(db_params, schema, "SELECT count(*) FROM stg_sales") == EXPECTED_VALID


def test_dimensions_are_upserted_not_duplicated(db_params: dict, schema: str) -> None:
    """Dimensions are never deleted (§7.3) — a customer does not stop existing."""
    load_once(db_params, schema)
    load_once(db_params, schema)

    assert scalar(db_params, schema, "SELECT count(*) FROM dim_customers") == 20
    assert scalar(db_params, schema, "SELECT count(*) FROM dim_products") == 15


def test_stale_fact_is_removed_when_a_row_leaves_the_file(
    db_params: dict, schema: str
) -> None:
    """The case a naive delete-by-current-order-ids gets wrong.

    Load the file, then insert an extra fact and its staging row as though a previous load
    had contained an order this file no longer does. A rerun must remove it: it is scoped
    to the file through `stg_sales.source_file`, not to the order_ids of the current load.

    Without the subquery through staging, that row survives forever as a fact no source row
    explains — and the reconciliation would report a count mismatch it could not localise.
    """
    load_once(db_params, schema)

    with DatabaseConnection(**db_params) as connection:
        connection.execute(f'SET search_path TO "{schema}"')
        customer_key = connection.execute(
            "SELECT customer_key FROM dim_customers LIMIT 1"
        ).fetchone()[0]
        product_key = connection.execute(
            "SELECT product_key FROM dim_products LIMIT 1"
        ).fetchone()[0]
        run_id = uuid4()
        connection.execute(
            "INSERT INTO stg_sales (run_id, source_file, row_num, order_id, order_date,"
            " customer_id, product_id, quantity, unit_price, discount_rate)"
            " VALUES (%s, %s, 999, 999999, '2026-01-05', 'C001', 'P001', 1, 5.00, 0.00)",
            (run_id, SOURCE_FILE),
        )
        connection.execute(
            "INSERT INTO fact_sales (order_id, order_date, customer_key, product_key,"
            " quantity, unit_price, discount_rate, gross_sales, discount_amount,"
            " net_sales, run_id)"
            " VALUES (999999, '2026-01-05', %s, %s, 1, 5.00, 0.00, 5.00, 0.00, 5.00, %s)",
            (customer_key, product_key, run_id),
        )

    assert scalar(db_params, schema, "SELECT count(*) FROM fact_sales") == EXPECTED_VALID + 1

    load_once(db_params, schema)

    assert scalar(db_params, schema, "SELECT count(*) FROM fact_sales") == EXPECTED_VALID
    assert (
        scalar(
            db_params, schema, "SELECT count(*) FROM fact_sales WHERE order_id = 999999"
        )
        == 0
    )
    assert scalar(db_params, schema, "SELECT sum(net_sales) FROM fact_sales") == EXPECTED_NET


# ======================================================================
# Payload, batching, run log
# ======================================================================


def test_raw_payload_is_queryable_jsonb_not_a_string(
    db_params: dict, schema: str, loaded: dict
) -> None:
    """`Json(...)` matters: a dict adapted as text round-trips unqueryable.

    Row 86 has a currency-formatted price, so JSON operators must reach it directly.
    """
    value = scalar(
        db_params,
        schema,
        "SELECT raw_payload ->> 'unit_price' FROM etl_rejected_sales WHERE row_num = 86",
    )

    assert value == "$45.00"


def test_reason_codes_group_in_sql(db_params: dict, schema: str, loaded: dict) -> None:
    """R23 and D-013: rejections group on a column, with no string parsing."""
    rows = dict(
        query(
            db_params,
            schema,
            "SELECT reason_code, count(*) FROM etl_rejected_sales"
            " GROUP BY reason_code ORDER BY reason_code",
        )
    )

    assert rows["MISSING_FIELD"] == 3
    assert rows["QTY_NOT_POSITIVE"] == 3
    assert rows["DUPLICATE_ORDER_ID"] == 2
    assert sum(rows.values()) == EXPECTED_REJECTED
    assert len(rows) == 18


def test_pipe_separator_survives_into_the_database(
    db_params: dict, schema: str, loaded: dict
) -> None:
    """D-023's separator, end to end. Row 117 carries three defects."""
    detail = scalar(
        db_params,
        schema,
        "SELECT reason_detail FROM etl_rejected_sales WHERE row_num = 117",
    )

    assert detail.count(" | ") == 2
    assert detail.startswith("QTY_NOT_POSITIVE:")


@pytest.mark.parametrize("batch_size", [1, 7, 1000])
def test_batching_loads_everything_regardless_of_chunk_size(
    db_params: dict, schema: str, batch_size: int
) -> None:
    """§7.5's chunking must not drop or duplicate a partial final chunk.

    172 is not a multiple of 7, so the last chunk is short — the off-by-one that chunked
    inserts get wrong.
    """
    load_once(db_params, schema, batch_size=batch_size)

    assert scalar(db_params, schema, "SELECT count(*) FROM fact_sales") == EXPECTED_VALID
    assert scalar(db_params, schema, "SELECT sum(net_sales) FROM fact_sales") == EXPECTED_NET


def test_run_log_records_the_run(db_params: dict, schema: str, loaded: dict) -> None:
    rows = query(
        db_params,
        schema,
        "SELECT source_file, rows_extracted, rows_valid, rows_rejected, rows_loaded,"
        " status FROM etl_run_log WHERE run_id = %s",
        (loaded["run_id"],),
    )

    assert rows == [(SOURCE_FILE, 200, EXPECTED_VALID, EXPECTED_REJECTED, EXPECTED_VALID, "SUCCESS")]


def test_a_failed_load_leaves_nothing_behind(db_params: dict, schema: str) -> None:
    """One transaction for the whole load (§7.4, D-008).

    Also demonstrates the run-log limitation noted in `write_run_log`: the failure rolls
    back its own log row, so `etl_run_log` records successes only. Making failures
    observable needs a connection outside this transaction.
    """
    customers, products, valid, rejected, facts = build_run()

    with pytest.raises(RuntimeError, match="deliberate"):
        with DatabaseConnection(**db_params) as connection:
            connection.execute(f'SET search_path TO "{schema}"')
            loader = PostgresLoader(connection)
            loader.create_tables()
            loader.upsert_dim_customers(customers)
            loader.load_staging(valid, uuid4())
            raise RuntimeError("deliberate failure after staging")

    with psycopg.connect(**db_params) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = %s",
                (schema,),
            ).fetchall()
        }

    # Even the DDL rolled back, because create_tables() ran inside the transaction.
    assert tables == set()
