"""
Tests requiring a live PostgreSQL (Step 8a).

**These skip rather than fail when Postgres is unreachable.** A grader running `pytest`
on a fresh clone without `docker compose up` should see skips, not a red suite — a test
that fails for want of infrastructure teaches nothing about the code.

Every test runs inside its own throwaway schema, dropped afterwards. The dev database is
what gets demonstrated on camera; tests must not populate or truncate it.

The most interesting test here is `test_additivity_check_is_absent`: it asserts a
constraint is *missing*, because D-024 decided it must be, and an absence nothing checks
is an absence someone eventually fills in.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")

from dotenv import load_dotenv  # noqa: E402

from src.config import PipelineConfig  # noqa: E402
from src.loaders import DatabaseConnection  # noqa: E402

SCHEMA_SQL = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"

EXPECTED_TABLES = {
    "etl_run_log",
    "stg_sales",
    "dim_customers",
    "dim_products",
    "fact_sales",
    "etl_rejected_sales",
}


@pytest.fixture(scope="module")
def db_params() -> dict:
    """Connection parameters, or skip the module if there is nothing to connect to."""
    load_dotenv()
    try:
        config = PipelineConfig.from_env()
    except ValueError as error:
        pytest.skip(f"database env vars not configured: {error}")

    params = {
        "host": config.db_host,
        "port": config.db_port,
        "dbname": config.db_name,
        "user": config.db_user,
        "password": config.db_password,
    }

    try:
        with psycopg.connect(**params, connect_timeout=3) as connection:
            connection.execute("SELECT 1")
    except Exception as error:  # noqa: BLE001 - any failure means "no database here"
        pytest.skip(f"postgres unreachable ({type(error).__name__}): {error}")

    return params


@pytest.fixture
def schema(db_params: dict):
    """A disposable schema per test, so nothing touches the demo database's public tables."""
    name = f"t_{uuid4().hex[:12]}"

    with psycopg.connect(**db_params, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{name}"')
    try:
        yield name
    finally:
        with psycopg.connect(**db_params, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA "{name}" CASCADE')


def apply_ddl(db_params: dict, schema: str) -> None:
    """Apply schema.sql inside the given schema."""
    ddl = SCHEMA_SQL.read_text(encoding="utf-8")
    with DatabaseConnection(**db_params) as connection:
        connection.execute(f'SET search_path TO "{schema}"')
        connection.execute(ddl)


def table_names(db_params: dict, schema: str) -> set[str]:
    with psycopg.connect(**db_params) as connection:
        rows = connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (schema,),
        ).fetchall()
    return {row[0] for row in rows}


def seed_dimensions(connection, schema: str) -> tuple[int, int]:
    """Insert one customer and one product, returning their surrogate keys.

    Facts need dimension rows to reference — which is §7.2's ordering constraint made
    concrete: dimensions load before the fact table because the fact table cannot exist
    without them.
    """
    connection.execute(f'SET search_path TO "{schema}"')
    customer_key = connection.execute(
        "INSERT INTO dim_customers (customer_id, customer_name, region) "
        "VALUES ('C001', 'Alderman Supply', 'North') RETURNING customer_key"
    ).fetchone()[0]
    product_key = connection.execute(
        "INSERT INTO dim_products (product_id, product_name, category) "
        "VALUES ('P001', 'Mechanical Keyboard', 'Hardware') RETURNING product_key"
    ).fetchone()[0]
    return customer_key, product_key


def insert_fact(connection, schema: str, **overrides) -> None:
    customer_key, product_key = seed_dimensions(connection, schema)
    values = {
        "order_id": 1000,
        "order_date": "2026-01-15",
        "quantity": 4,
        "unit_price": "25.00",
        "discount_rate": "0.10",
        "gross_sales": "100.00",
        "discount_amount": "10.00",
        "net_sales": "90.00",
    }
    values.update(overrides)
    connection.execute(
        "INSERT INTO fact_sales (order_id, order_date, customer_key, product_key,"
        " quantity, unit_price, discount_rate, gross_sales, discount_amount,"
        " net_sales, run_id)"
        " VALUES (%(order_id)s, %(order_date)s, %(ck)s, %(pk)s, %(quantity)s,"
        " %(unit_price)s, %(discount_rate)s, %(gross_sales)s, %(discount_amount)s,"
        " %(net_sales)s, gen_random_uuid())",
        {**values, "ck": customer_key, "pk": product_key},
    )


# ======================================================================
# DDL idempotency
# ======================================================================


def test_ddl_creates_all_six_tables(db_params: dict, schema: str) -> None:
    apply_ddl(db_params, schema)

    assert table_names(db_params, schema) == EXPECTED_TABLES


def test_ddl_applies_twice_without_error(db_params: dict, schema: str) -> None:
    """`create_tables()` runs on every pipeline run, so applying twice must be a no-op.

    A schema step that may only run once is a schema step someone eventually runs twice.
    """
    apply_ddl(db_params, schema)
    apply_ddl(db_params, schema)

    assert table_names(db_params, schema) == EXPECTED_TABLES


# ======================================================================
# The constraint that must NOT exist (D-024)
# ======================================================================


def test_additivity_check_is_absent(db_params: dict, schema: str) -> None:
    """A row where gross != discount + net must be accepted.

    This is the shape of rows 34, 76 and 118 in the committed dataset: gross 174.75,
    discount 17.48, net 157.28, which sum to 174.76. Each column is the correctly rounded
    value of its own exact quantity; the identity is off by a cent because rounding cannot
    preserve both (D-024).

    Asserted as a test because the constraint's absence is deliberate and looks like an
    oversight. Without this, someone adds `CHECK (gross_sales = discount_amount +
    net_sales)` as an obvious improvement and three valid rows start failing.

    **This is what turns D-024 from documentation into a gate.** A comment in `schema.sql`
    asks the next person to agree; this test makes the schema unable to change without
    someone deciding to overrule it. A documented decision that nothing enforces is a
    decision with a shelf life.
    """
    apply_ddl(db_params, schema)

    with DatabaseConnection(**db_params) as connection:
        insert_fact(
            connection,
            schema,
            quantity=5,
            unit_price="34.95",
            gross_sales="174.75",
            discount_amount="17.48",
            net_sales="157.28",
        )

    with psycopg.connect(**db_params) as connection:
        connection.execute(f'SET search_path TO "{schema}"')
        total = connection.execute(
            "SELECT gross_sales - discount_amount - net_sales FROM fact_sales"
        ).fetchone()[0]

    assert str(total) == "-0.01"


# ======================================================================
# The constraints that must exist (D-010)
# ======================================================================


@pytest.mark.parametrize(
    "overrides, constraint",
    [
        ({"quantity": 0}, "quantity"),
        ({"quantity": -3}, "quantity"),
        ({"unit_price": "0.00"}, "unit_price"),
        ({"unit_price": "-19.99"}, "unit_price"),
        ({"discount_rate": "1.0000"}, "discount_rate"),
        ({"discount_rate": "-0.1000"}, "discount_rate"),
    ],
)
def test_check_constraints_reject_what_the_validator_rejects(
    db_params: dict, schema: str, overrides: dict, constraint: str
) -> None:
    """D-010's redundancy, verified from the database side.

    These duplicate rules `SalesDataValidator` already enforces. If one ever fires in
    production the validator has a bug, and the row being refused is how that bug becomes
    visible instead of becoming data.
    """
    apply_ddl(db_params, schema)

    with pytest.raises(psycopg.errors.CheckViolation, match=constraint):
        with DatabaseConnection(**db_params) as connection:
            insert_fact(connection, schema, **overrides)


def test_duplicate_order_id_is_refused_by_the_unique_constraint(
    db_params: dict, schema: str
) -> None:
    """The grain statement enforced in the schema, not only in the validator."""
    apply_ddl(db_params, schema)

    with pytest.raises(psycopg.errors.UniqueViolation):
        with DatabaseConnection(**db_params) as connection:
            connection.execute(f'SET search_path TO "{schema}"')
            customer_key, product_key = seed_dimensions(connection, schema)
            for _ in range(2):
                connection.execute(
                    "INSERT INTO fact_sales (order_id, order_date, customer_key,"
                    " product_key, quantity, unit_price, discount_rate, gross_sales,"
                    " discount_amount, net_sales, run_id)"
                    " VALUES (1000, '2026-01-15', %s, %s, 4, 25.00, 0.10, 100.00,"
                    " 10.00, 90.00, gen_random_uuid())",
                    (customer_key, product_key),
                )


# ======================================================================
# DatabaseConnection — the transaction boundary (§7.4)
# ======================================================================


def test_commits_on_clean_exit(db_params: dict, schema: str) -> None:
    apply_ddl(db_params, schema)

    with DatabaseConnection(**db_params) as connection:
        connection.execute(f'SET search_path TO "{schema}"')
        connection.execute(
            "INSERT INTO dim_customers (customer_id, customer_name, region)"
            " VALUES ('C001', 'Alderman Supply', 'North')"
        )

    with psycopg.connect(**db_params) as connection:
        connection.execute(f'SET search_path TO "{schema}"')
        count = connection.execute("SELECT count(*) FROM dim_customers").fetchone()[0]

    assert count == 1


def test_rolls_back_on_exception(db_params: dict, schema: str) -> None:
    """A run that fails part-way must leave the database exactly as it was (D-008).

    Committing what succeeded would leave dimensions describing facts that do not exist —
    a state no query can interpret and no reconciliation can explain.
    """
    apply_ddl(db_params, schema)

    with pytest.raises(RuntimeError, match="deliberate"):
        with DatabaseConnection(**db_params) as connection:
            connection.execute(f'SET search_path TO "{schema}"')
            connection.execute(
                "INSERT INTO dim_customers (customer_id, customer_name, region)"
                " VALUES ('C001', 'Alderman Supply', 'North')"
            )
            raise RuntimeError("deliberate failure mid-transaction")

    with psycopg.connect(**db_params) as connection:
        connection.execute(f'SET search_path TO "{schema}"')
        count = connection.execute("SELECT count(*) FROM dim_customers").fetchone()[0]

    assert count == 0


def test_exception_is_not_swallowed(db_params: dict) -> None:
    """__exit__ returns False, so a rollback still reports failure to the caller.

    A silent rollback is the worst outcome available here: the run says SUCCESS and the
    database contains nothing.
    """
    with pytest.raises(ValueError, match="propagate"):
        with DatabaseConnection(**db_params):
            raise ValueError("must propagate")


def test_connection_is_closed_after_a_clean_block(db_params: dict) -> None:
    with DatabaseConnection(**db_params) as connection:
        connection.execute("SELECT 1")

    assert connection.closed


def test_connection_is_closed_after_a_failed_block(db_params: dict) -> None:
    """The `finally` in __exit__ — a long-running process must not leak one per failure."""
    captured = None

    with pytest.raises(RuntimeError):
        with DatabaseConnection(**db_params) as connection:
            captured = connection
            raise RuntimeError("boom")

    assert captured is not None
    assert captured.closed


def test_from_config_builds_a_usable_connection(db_params: dict) -> None:
    """main.py wires this in one line; the plain constructor keeps tests config-free."""
    load_dotenv()
    config = PipelineConfig.from_env()

    with DatabaseConnection.from_config(config) as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1


def test_autocommit_is_off_so_the_block_is_one_transaction(db_params: dict) -> None:
    """One transaction per run (§7.4) — per-statement autocommit would defeat rollback."""
    with DatabaseConnection(**db_params) as connection:
        assert connection.autocommit is False


# ======================================================================
# stg_sales constraints — the tripwire at the first landing point
# ======================================================================


def insert_staging(connection, schema: str, **overrides) -> None:
    connection.execute(f'SET search_path TO "{schema}"')
    values = {
        "order_id": 1000,
        "order_date": "2026-01-15",
        "customer_id": "C001",
        "product_id": "P001",
        "quantity": 4,
        "unit_price": "25.00",
        "discount_rate": "0.10",
    }
    values.update(overrides)
    connection.execute(
        "INSERT INTO stg_sales (run_id, source_file, row_num, order_id, order_date,"
        " customer_id, product_id, quantity, unit_price, discount_rate)"
        " VALUES (gen_random_uuid(), 'sales_2026_01.csv', 40, %(order_id)s,"
        " %(order_date)s, %(customer_id)s, %(product_id)s, %(quantity)s,"
        " %(unit_price)s, %(discount_rate)s)",
        values,
    )


def test_staging_accepts_a_valid_row(db_params: dict, schema: str) -> None:
    apply_ddl(db_params, schema)

    with DatabaseConnection(**db_params) as connection:
        insert_staging(connection, schema)

    with psycopg.connect(**db_params) as connection:
        connection.execute(f'SET search_path TO "{schema}"')
        assert connection.execute("SELECT count(*) FROM stg_sales").fetchone()[0] == 1


@pytest.mark.parametrize(
    "overrides, constraint",
    [
        ({"quantity": 0}, "quantity"),
        ({"quantity": -3}, "quantity"),
        ({"unit_price": "0.00"}, "unit_price"),
        ({"unit_price": "-19.99"}, "unit_price"),
        ({"discount_rate": "1.0000"}, "discount_rate"),
        ({"discount_rate": "-0.1000"}, "discount_rate"),
    ],
)
def test_staging_constraints_match_the_fact_table(
    db_params: dict, schema: str, overrides: dict, constraint: str
) -> None:
    """The tripwire has to be at the first landing point, not only the last.

    §8.2's first reconciliation check counts rows in stg_sales, so a validator bug that
    reaches staging has already corrupted the number the reconciliation trusts. Catching
    it only at fact_sales would leave staging and facts disagreeing, with staging wrong.

    Permissive staging with strict facts would be a coherent design — a landing zone that
    accepts anything and cleans up downstream. It is not this design: per SPEC Q5,
    stg_sales holds typed validated records, and these constraints say so.
    """
    apply_ddl(db_params, schema)

    with pytest.raises(psycopg.errors.CheckViolation, match=constraint):
        with DatabaseConnection(**db_params) as connection:
            insert_staging(connection, schema, **overrides)


def test_staging_has_no_additivity_constraint(db_params: dict, schema: str) -> None:
    """The cleaner form of the D-024 argument, one layer earlier.

    At `fact_sales` the missing additivity constraint is a *choice* — the three columns are
    there, they could be constrained against each other, and we decided not to. Here it is
    a *structural fact*: `stg_sales` holds the inputs to the arithmetic, not its outputs, so
    there is nothing to constrain. The measures do not exist at this layer.

    That distinction is worth keeping straight. A reader who sees no additivity constraint
    in either table might conclude the rule was forgotten twice. It was declined once and
    was never applicable at all in the other place, and this test pins the second half.
    """
    apply_ddl(db_params, schema)

    with psycopg.connect(**db_params) as connection:
        columns = {
            row[0]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = %s AND table_name = 'stg_sales'",
                (schema,),
            ).fetchall()
        }

    assert not {"gross_sales", "discount_amount", "net_sales"} & columns
