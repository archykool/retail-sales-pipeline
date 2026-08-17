"""
Tests for the orchestrator (Step 10).

The one that matters most is `test_dry_run_opens_no_database_connection`. §8.1's claim is
not "dry-run skips the writes" but "dry-run opens no connection", and the difference is
only observable if something makes connecting impossible. So the test sabotages
`psycopg.connect` and asserts the dry run completes anyway — which is the same thing as
proving the mode works on a laptop with no database running.

`test_orchestrator_computes_nothing` guards the §13 rejection trigger from the other side:
every figure in the summary has to be traceable to a layer that produced it.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")

from src.pipeline import SalesPipeline  # noqa: E402

from .conftest import RAW_DIR  # noqa: E402

TODAY = date(2026, 8, 17)
EXPECTED_EXTRACTED = 200
EXPECTED_VALID = 172
EXPECTED_REJECTED = 28
EXPECTED_NET = Decimal("51107.07")


@pytest.fixture
def config(pipeline_config, tmp_path: Path):
    """The real config, pointed at the committed inputs and a throwaway output directory.

    `dataclasses.replace` because `PipelineConfig` is frozen — which is also how `--file`
    will work at Step 10+ rather than adding a parameter to the pipeline.
    """
    return dataclasses.replace(
        pipeline_config,
        sales_file=RAW_DIR / "sales_2026_01.csv",
        customers_file=RAW_DIR / "customers.json",
        products_file=RAW_DIR / "products.json",
        rejected_dir=tmp_path,
    )


@pytest.fixture
def isolated_connect(monkeypatch, schema: str):
    """Route every connection the pipeline opens into the disposable schema.

    `options=-c search_path=...` goes through the connection parameters rather than a `SET`
    afterwards, because the pipeline owns its own connection and gives a test no hook to run
    a statement first. This is what lets a *real* run be tested without touching
    `sales_dev`, which is the database being demonstrated on camera.
    """
    original = psycopg.connect

    def connect(*args, **kwargs):
        kwargs["options"] = f"-c search_path={schema}"
        return original(*args, **kwargs)

    monkeypatch.setattr(psycopg, "connect", connect)
    return schema


# ======================================================================
# Dry run — §8.1
# ======================================================================


def test_dry_run_opens_no_database_connection(config, monkeypatch) -> None:
    """§8.1's actual claim, and the only test that can tell the difference.

    "Skips the writes" would still open a connection and would still fail on a machine with
    no database. Sabotaging `connect` proves the stronger property: the mode never reaches
    the database layer at all, so it runs anywhere.
    """
    def refuse(*args, **kwargs):
        raise AssertionError("dry run must not open a database connection")

    monkeypatch.setattr(psycopg, "connect", refuse)

    result = SalesPipeline(config, dry_run=True, today=TODAY).run()

    assert result.dry_run is True
    assert result.rows_loaded == 0


def test_dry_run_still_produces_the_full_summary(config, monkeypatch) -> None:
    """Dry-run proves the logic, so every count except rows_loaded must be real."""
    monkeypatch.setattr(
        psycopg, "connect", lambda *a, **k: pytest.fail("connection opened")
    )

    result = SalesPipeline(config, dry_run=True, today=TODAY).run()

    assert result.rows_extracted == EXPECTED_EXTRACTED
    assert result.rows_valid == EXPECTED_VALID
    assert result.rows_rejected == EXPECTED_REJECTED
    assert result.status == "SUCCESS"
    assert isinstance(result.run_id, UUID)
    assert result.duration_seconds > 0


def test_dry_run_writes_both_local_artifacts(config, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        psycopg, "connect", lambda *a, **k: pytest.fail("connection opened")
    )

    SalesPipeline(config, dry_run=True, today=TODAY).run()

    assert (tmp_path / "preview_fact_sales.csv").exists()
    assert list(tmp_path.glob("rejected_*.csv"))


def test_dry_run_preview_contains_every_valid_row(
    config, monkeypatch, tmp_path: Path
) -> None:
    import csv

    monkeypatch.setattr(
        psycopg, "connect", lambda *a, **k: pytest.fail("connection opened")
    )

    SalesPipeline(config, dry_run=True, today=TODAY).run()

    with (tmp_path / "preview_fact_sales.csv").open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    assert len(rows) == EXPECTED_VALID
    assert sum(Decimal(row["net_sales"]) for row in rows) == EXPECTED_NET


def test_dry_run_rejected_csv_has_every_rejection(
    config, monkeypatch, tmp_path: Path
) -> None:
    import csv

    monkeypatch.setattr(
        psycopg, "connect", lambda *a, **k: pytest.fail("connection opened")
    )

    SalesPipeline(config, dry_run=True, today=TODAY).run()

    path = next(iter(tmp_path.glob("rejected_*.csv")))
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    assert len(rows) == EXPECTED_REJECTED


# ======================================================================
# Real run
# ======================================================================


def test_real_run_loads_all_six_tables(config, isolated_connect, db_params: dict) -> None:
    schema = isolated_connect
    result = SalesPipeline(config, today=TODAY).run()

    assert result.dry_run is False
    assert result.rows_loaded == EXPECTED_VALID

    with psycopg.connect(**db_params) as connection:
        counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "etl_run_log", "stg_sales", "dim_customers",
                "dim_products", "fact_sales", "etl_rejected_sales",
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


def test_real_run_does_not_write_a_preview(
    config, isolated_connect, tmp_path: Path
) -> None:
    """A "preview" of a load that already happened is a misnomer.

    After a real run, `fact_sales` is the artifact, and a stale preview beside it is a way
    to read the wrong numbers on camera.
    """
    SalesPipeline(config, today=TODAY).run()

    assert not (tmp_path / "preview_fact_sales.csv").exists()
    assert list(tmp_path.glob("rejected_*.csv"))


def test_real_run_control_total_matches_the_summary(
    config, isolated_connect, db_params: dict
) -> None:
    result = SalesPipeline(config, today=TODAY).run()

    with psycopg.connect(**db_params) as connection:
        net = connection.execute("SELECT sum(net_sales) FROM fact_sales").fetchone()[0]
        staged = connection.execute("SELECT count(*) FROM stg_sales").fetchone()[0]

    assert net == EXPECTED_NET
    assert staged == result.rows_valid


def test_running_twice_is_idempotent(config, isolated_connect, db_params: dict) -> None:
    """§7.3's claim, driven through the orchestrator rather than the loader directly."""
    SalesPipeline(config, today=TODAY).run()
    SalesPipeline(config, today=TODAY).run()

    with psycopg.connect(**db_params) as connection:
        net = connection.execute("SELECT sum(net_sales) FROM fact_sales").fetchone()[0]
        facts = connection.execute("SELECT count(*) FROM fact_sales").fetchone()[0]
        runs = connection.execute("SELECT count(*) FROM etl_run_log").fetchone()[0]

    assert net == EXPECTED_NET
    assert facts == EXPECTED_VALID
    # Two runs, two log rows — the log accumulates while the data is replaced.
    assert runs == 2


def test_each_run_gets_its_own_run_id(config, isolated_connect) -> None:
    first = SalesPipeline(config, today=TODAY).run()
    second = SalesPipeline(config, today=TODAY).run()

    assert first.run_id != second.run_id
    assert first.run_id.version == 4


# ======================================================================
# Failure behaviour
# ======================================================================


def test_a_missing_input_file_raises_rather_than_reporting_success(
    config, tmp_path: Path
) -> None:
    """The orchestrator must not convert a failure into a summary.

    `main.py` turns the exception into exit code 1, which is what makes the pipeline
    cron-able. A FAILED result object would have to be inspected to notice anything went
    wrong, and cron does not inspect return values.
    """
    broken = dataclasses.replace(config, sales_file=tmp_path / "does_not_exist.csv")

    with pytest.raises(FileNotFoundError):
        SalesPipeline(broken, dry_run=True, today=TODAY).run()


def test_status_is_only_ever_success(config, monkeypatch) -> None:
    """FAILED is unrepresentable here, matching D-025's conclusion about the table.

    Under one transaction, failure is not a state anything survives to record — so the
    model and the column agree, and they agree for the same reason.
    """
    monkeypatch.setattr(
        psycopg, "connect", lambda *a, **k: pytest.fail("connection opened")
    )

    assert SalesPipeline(config, dry_run=True, today=TODAY).run().status == "SUCCESS"


# ======================================================================
# The orchestrator does not compute
# ======================================================================


def test_orchestrator_computes_nothing(config, monkeypatch) -> None:
    """Every figure in the summary must come from a layer that owns the data.

    Checked by comparing the summary against the same stages run independently. If the
    pipeline ever derived a number itself, this would drift — which is the §13 rejection
    trigger "business logic in pipeline.py", caught mechanically.
    """
    from src.extractors import CSVExtractor, JSONExtractor
    from src.transformers import ReferenceDataTransformer, SalesDataTransformer
    from src.validators import SalesDataValidator, period_from_filename

    monkeypatch.setattr(
        psycopg, "connect", lambda *a, **k: pytest.fail("connection opened")
    )

    reference = ReferenceDataTransformer()
    customers = reference.to_customers(JSONExtractor(config.customers_file).extract())
    products = reference.to_products(JSONExtractor(config.products_file).extract())
    raw = CSVExtractor(config.sales_file).extract()
    valid, rejected = SalesDataValidator(
        reference.customer_ids(customers),
        reference.product_ids(products),
        max_quantity=config.max_quantity,
        today=TODAY,
        period=period_from_filename(config.sales_file.name),
    ).validate(raw)
    facts = SalesDataTransformer().to_facts(valid)

    result = SalesPipeline(config, dry_run=True, today=TODAY).run()

    assert result.rows_extracted == len(raw)
    assert result.rows_valid == len(valid)
    assert result.rows_rejected == len(rejected)
    assert len(facts) == len(valid)


def test_source_file_in_the_run_log_is_the_bare_filename(
    config, isolated_connect, db_params: dict
) -> None:
    """D-020: a full path would differ per machine and break §7.3's delete key."""
    SalesPipeline(config, today=TODAY).run()

    with psycopg.connect(**db_params) as connection:
        source = connection.execute("SELECT source_file FROM etl_run_log").fetchone()[0]

    assert source == "sales_2026_01.csv"


def test_every_stage_is_logged_with_a_duration(
    config, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Per-stage timing is what makes the log the first place to look when a run slows."""
    monkeypatch.setattr(
        psycopg, "connect", lambda *a, **k: pytest.fail("connection opened")
    )

    with caplog.at_level(logging.INFO, logger="src.pipeline"):
        SalesPipeline(config, dry_run=True, today=TODAY).run()

    stages = [line for line in caplog.text.splitlines() if "stage " in line]

    assert len(stages) >= 8
    assert any("validate" in line for line in stages)
    assert any("transform sales" in line for line in stages)
