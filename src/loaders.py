"""
Getting data out of the pipeline — to disk, and to PostgreSQL.

Three things live here. `RejectedRecordWriter` quarantines what failed;
`FactPreviewWriter` shows what would be loaded; `DatabaseConnection` owns the
transaction boundary. `PostgresLoader` joins them at Step 8b.

The two writers run in dry-run mode and neither opens a database connection, which is
the point of §8.1: dry-run proves the logic without proving the load, and those are
different claims. `DatabaseConnection` is the line between them.

From the pipeline layers this imports `models` only — and, for type annotations alone,
`config`, which §3.1 permits as a leaf. In particular **not `validators`**: that is the
back-edge §3.1 forbids, and it would be an easy one to introduce, since the column names
these writers want are already defined there.

**The two writers do not agree on filenames, deliberately but not ideally.**
`FactPreviewWriter` writes a fixed name and overwrites, matching what §7.3's
delete-then-insert does to the database. `RejectedRecordWriter` accumulates a
timestamped file per run. See its docstring — the asymmetry is real and unresolved.
"""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import psycopg
from psycopg import Connection
from psycopg.types.json import Json

from .models import (
    Customer,
    FactSalesRecord,
    PipelineResult,
    Product,
    RejectedRecord,
    ValidSalesRecord,
)

if TYPE_CHECKING:  # import only for annotations, so loaders stays runnable without config
    from .config import PipelineConfig

logger = logging.getLogger(__name__)

# Mirrors `fact_sales` as declared in §5, in its declared order, with two deliberate
# departures.
#
# `customer_key` and `product_key` become `customer_id` and `product_id`: the preview
# is built from `FactSalesRecord`, which carries natural keys because §7.2 keeps
# surrogate resolution in the loader and out of the transformer. A preview containing
# surrogate keys would require this file to query `dim_customers`, which is the thing
# §7.2 exists to prevent.
#
# `sales_key`, `run_id` and `loaded_at` are absent: all three are generated at insert
# time, so nothing before the load can know them.
#
# The `-- PREVIEW COMPARISON` query at Step 13 joins the surrogate keys back to natural
# ones so the table can be read against this file directly.
#
# `row_num` and `source_file` trail at the end, where the rejected CSV leads with them.
# That is not an inconsistency between the two writers — **the two files have different
# readers, and column order follows the reader's first question.** Whoever opens the
# rejected CSV is asking "which row do I go fix?", so provenance leads. Whoever opens the
# preview is asking "are the numbers right before I load this?", so the measures lead and
# provenance is there for when the answer is no (D-020). Each convention is correct for
# its reader; a shared one would be wrong for both.
FACT_PREVIEW_COLUMNS = (
    "order_id",
    "order_date",
    "customer_id",
    "product_id",
    "quantity",
    "unit_price",
    "discount_rate",
    "gross_sales",
    "discount_amount",
    "net_sales",
    "row_num",
    "source_file",
)

# Fixed, not timestamped — §8.1 names this exact file, and overwriting matches what
# §7.3's delete-then-insert does to `fact_sales`.
FACT_PREVIEW_FILENAME = "preview_fact_sales.csv"

# The rejected CSV's columns. Provenance first: `source_file` and `row_num` are how a
# person finds the offending line in the original file, which is the whole point of
# quarantining rather than dropping.
REJECTED_COLUMNS = (
    "source_file",
    "row_num",
    "reason_code",
    "reason_detail",
    "raw_payload",
    "rejected_at",
)

# Seconds precision, and no colons — an ISO-8601 timestamp contains them and Windows
# forbids them in filenames, so the obvious `rejected_2026-08-17T14:30:22.csv` is
# simply an unopenable file (§14).
_FILENAME_TIMESTAMP = "%Y%m%d_%H%M%S"


class RejectedRecordWriter:
    """Writes quarantined records to a timestamped CSV.

    Runs before and independently of any database work, so the rejects are on disk even
    if the load fails — a failed run that discards its own diagnosis is worse than one
    that never started.
    """

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    def write(self, records: list[RejectedRecord]) -> Path:
        """Write every rejected record and return the path written.

        **The header is written even when there are no rejections.** An empty file with
        a header states "this run found nothing wrong"; a missing file is ambiguous
        between that and "the writer never ran", and those need different responses.

        `newline=""` is mandatory: without it `csv.writer` emits `\\r\\r\\n` on Windows
        and every record is followed by a blank row, which turns a clean file into one
        that looks corrupt in Excel (§14).
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self._next_available_path()

        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=REJECTED_COLUMNS)
            writer.writeheader()
            for record in records:
                writer.writerow(self._as_row(record))

        logger.info("wrote %d rejected records to %s", len(records), path)
        return path

    def _next_available_path(self) -> Path:
        """A timestamped path, suffixed if something is already there.

        Two runs inside the same second would otherwise silently overwrite the first
        run's rejects, so the suffix prevents a same-second collision.

        **Known asymmetry with the database, unresolved.** §7.3 makes a rerun
        `DELETE FROM etl_rejected_sales WHERE source_file = %s` before reinserting, so
        the table holds one run's rejections — the latest. This directory accumulates
        one file per run. After five reruns there are five files and one table state,
        so "the CSV and the table are two carriers of the same audit record" is only
        true of the newest file. Do not claim parity without that qualification.
        `FactPreviewWriter` in this same module does the opposite: fixed filename,
        overwrite, which does match the database. Whether both should overwrite is
        deferred to Step 14.
        """
        stem = f"rejected_{datetime.now().strftime(_FILENAME_TIMESTAMP)}"
        candidate = self.output_dir / f"{stem}.csv"

        suffix = 2
        while candidate.exists():
            candidate = self.output_dir / f"{stem}_{suffix}.csv"
            suffix += 1

        return candidate

    @staticmethod
    def _as_row(record: RejectedRecord) -> dict[str, object]:
        """Flatten one record for CSV.

        `raw_payload` is serialised as JSON in a single column so that it has the same
        structure as the `JSONB` column in `etl_rejected_sales` (§5). The file and the
        table are then two carriers of one audit record rather than two formats of it,
        and nothing has to be reshaped to compare them.

        Expanding it into named source columns would read better in Excel, but it would
        mean this file knowing the sales column list — a third copy of it, or an import
        from `validators` that §3.1 forbids. The triage path is `source_file` plus
        `row_num`: those send a person to the actual line in the actual file, and the
        payload is here as a record of what arrived, not as the thing they edit.

        `ensure_ascii=False` so a non-ASCII value stays legible instead of becoming an
        escape sequence in an audit record someone has to read.
        """
        return {
            "source_file": record.source_file,
            "row_num": record.row_num,
            "reason_code": record.reason_code,
            # Already joined with " | " by the validator (D-023). Nothing here
            # re-formats it: the CSV and the database column carry the same string.
            "reason_detail": record.reason_detail,
            "raw_payload": json.dumps(record.raw_payload, ensure_ascii=False),
            "rejected_at": record.rejected_at.isoformat(),
        }


class FactPreviewWriter:
    """Writes the fact rows a run *would* load, before any database is involved.

    This is the concrete answer to the assignment's checkpoint question — "can you
    check your output before load into the postgres?" — and it is a stronger answer
    than a printed summary, because a summary reports counts while this reports the
    actual numbers that would be inserted.

    Dry-run proves the logic and the dev database proves the load; they catch
    different failures (§8.1). This file is the evidence for the first claim only.
    """

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    def write(self, facts: list[FactSalesRecord]) -> Path:
        """Write every fact row to a fixed filename, overwriting any previous preview.

        Overwriting is correct here and is the opposite of what `RejectedRecordWriter`
        does. A preview describes one prospective load, so a stale preview beside a
        current one is a way to read the wrong numbers on camera. It also matches §7.3:
        the database keeps the latest run, not an accumulation.

        Header written even with zero rows, for the same reason as the rejected writer:
        an empty file is a statement, a missing file is an ambiguity. `newline=""` per
        §14 — without it every record is followed by a blank row on Windows.
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / FACT_PREVIEW_FILENAME

        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FACT_PREVIEW_COLUMNS)
            writer.writeheader()
            for fact in facts:
                writer.writerow(self._as_row(fact))

        logger.info("wrote %d preview fact rows to %s", len(facts), path)
        return path

    @staticmethod
    def _as_row(fact: FactSalesRecord) -> dict[str, object]:
        """Flatten one fact row, letting Decimal render itself.

        `str(Decimal("90.00"))` is `"90.00"` — the trailing zero is preserved because
        the exponent is part of the value. Passing the Decimal to the csv module would
        reach the same result, but going through `str` explicitly documents that no
        float conversion happens anywhere in the money path (D-006).
        """
        return {
            "order_id": fact.order_id,
            "order_date": fact.order_date.isoformat(),
            "customer_id": fact.customer_id,
            "product_id": fact.product_id,
            "quantity": fact.quantity,
            "unit_price": str(fact.unit_price),
            "discount_rate": str(fact.discount_rate),
            "gross_sales": str(fact.gross_sales),
            "discount_amount": str(fact.discount_amount),
            "net_sales": str(fact.net_sales),
            "row_num": fact.row_num,
            "source_file": fact.source_file,
        }


class DatabaseConnection:
    """One transaction for one pipeline run, as a context manager (§7.4).

    Commit on clean exit, rollback on any exception, close either way. The reason the
    boundary is the whole run rather than per-table is D-008: a run that loaded
    dimensions and then failed on facts would leave the warehouse in a state no query
    could interpret — dimensions describing rows that do not exist. All-or-nothing means
    a failed run leaves the database exactly as it was, and the rejected CSV on disk
    still explains why (`RejectedRecordWriter` runs before any of this).

    Never suppresses an exception. `__exit__` returns `False` so a failure that rolled
    back still reaches the caller — a silent rollback would report success on a run that
    loaded nothing.
    """

    def __init__(
        self,
        host: str,
        port: int,
        dbname: str,
        user: str,
        password: str,
    ) -> None:
        self.host = host
        self.port = port
        self.dbname = dbname
        self.user = user
        self.password = password
        self._connection: Connection | None = None

    @classmethod
    def from_config(cls, config: "PipelineConfig") -> "DatabaseConnection":
        """Build from `PipelineConfig`.

        Kept as a separate constructor so the class can be instantiated with plain
        arguments in a test without assembling a whole config, while `main.py` still
        wires it in one line.
        """
        return cls(
            host=config.db_host,
            port=config.db_port,
            dbname=config.db_name,
            user=config.db_user,
            password=config.db_password,
        )

    def __enter__(self) -> Connection:
        """Open the connection with autocommit off, so the block is one transaction."""
        self._connection = psycopg.connect(
            host=self.host,
            port=self.port,
            dbname=self.dbname,
            user=self.user,
            password=self.password,
            autocommit=False,
        )
        logger.info("connected to %s on %s:%s", self.dbname, self.host, self.port)
        return self._connection

    def __exit__(self, exc_type, exc, traceback) -> bool:
        """Commit or roll back, then always close.

        The `finally` matters: a failure during `commit()` itself must still close the
        connection, or a long-running process leaks one per failed run.
        """
        if self._connection is None:  # pragma: no cover - __enter__ raised
            return False

        try:
            if exc_type is None:
                self._connection.commit()
                logger.info("committed transaction on %s", self.dbname)
            else:
                self._connection.rollback()
                logger.warning(
                    "rolled back transaction on %s after %s",
                    self.dbname,
                    exc_type.__name__,
                )
        finally:
            self._connection.close()
            self._connection = None

        return False  # never swallow the exception that caused the rollback


class PostgresLoader:
    """Writes a validated run into the star schema.

    Takes an open connection rather than opening one, because the transaction boundary
    belongs to the caller (§7.4). The loader decides *what* to write and in what order;
    `DatabaseConnection` decides when it becomes permanent. A loader that committed its
    own work could leave dimensions loaded and facts not.

    This is also the only place surrogate keys exist. `FactSalesRecord` arrives carrying
    natural keys, and the `{business_id: surrogate_key}` dictionaries built below are the
    entire mechanic — see D-005: a surrogate key does not exist until its dimension row
    is inserted, so producing one requires a query, so it can only happen here.
    """

    def __init__(
        self,
        connection: Connection,
        *,
        batch_size: int = 1000,
        schema_path: Path | None = None,
    ) -> None:
        self.connection = connection
        self.batch_size = batch_size
        self.schema_path = schema_path or (
            Path(__file__).resolve().parent.parent / "sql" / "schema.sql"
        )

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def create_tables(self) -> None:
        """Apply `sql/schema.sql`.

        Safe on every run because the DDL is `IF NOT EXISTS` throughout. **That makes it
        idempotent, not migrating**: an existing table is skipped whole, so a column or
        constraint added to the file later will not appear on a database that already has
        the table. Changing the schema means `docker compose down -v`, the same trap §14
        records for the container's init scripts.
        """
        self.connection.execute(self.schema_path.read_text(encoding="utf-8"))
        logger.info("applied schema from %s", self.schema_path)

    # ------------------------------------------------------------------
    # Idempotency (§7.3) — must run before any insert
    # ------------------------------------------------------------------

    def delete_previous_load(self, source_file: str, order_ids: Sequence[int]) -> None:
        """Remove what a previous load of this file wrote, so a rerun replaces it.

        Three deletes, and the first is the one that is easy to get wrong.

        `fact_sales` has no `source_file` column, so facts cannot be scoped by file
        directly — §7.3 says to delete by "this file's order IDs". Deleting only the
        order_ids in the *current* load is not enough: if a row was valid last run and has
        since been edited into a rejection, or removed from the file entirely, its old
        fact row is not in the new set and would survive as a stale fact that no source
        row explains. So the first delete reaches through `stg_sales`, which *does* keep
        `source_file` (D-020), to find what the previous load of this file actually wrote.

        That is provenance paying for itself in a way that has nothing to do with
        debugging: `stg_sales.source_file` is what makes the fact table's idempotency
        expressible at all.

        Dimensions are never deleted, only upserted (§7.3). A customer does not stop
        existing because one file stopped mentioning them.
        """
        with self.connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM fact_sales WHERE order_id IN"
                " (SELECT order_id FROM stg_sales WHERE source_file = %s)",
                (source_file,),
            )
            facts_removed = cursor.rowcount

            if order_ids:
                # Also clear this load's own order_ids, in case the same order arrived
                # under a different filename. order_id is UNIQUE on fact_sales, so a
                # collision aborts the whole transaction rather than replacing a row.
                cursor.execute(
                    "DELETE FROM fact_sales WHERE order_id = ANY(%s)",
                    (list(order_ids),),
                )
                facts_removed += cursor.rowcount

            cursor.execute(
                "DELETE FROM stg_sales WHERE source_file = %s", (source_file,)
            )
            staged_removed = cursor.rowcount

            cursor.execute(
                "DELETE FROM etl_rejected_sales WHERE source_file = %s", (source_file,)
            )
            rejected_removed = cursor.rowcount

        logger.info(
            "cleared previous load of %s: %d facts, %d staged, %d rejected",
            source_file,
            facts_removed,
            staged_removed,
            rejected_removed,
        )

    # ------------------------------------------------------------------
    # Dimensions — before facts, always
    # ------------------------------------------------------------------

    def upsert_dim_customers(self, customers: list[Customer]) -> dict[str, int]:
        """Upsert customers and return `{customer_id: customer_key}`.

        `ON CONFLICT DO UPDATE`, deliberately not `DO NOTHING`. `DO NOTHING` returns no
        row when it conflicts, so `RETURNING` yields nothing for every customer that
        already existed — the map comes back missing keys and the fact load fails on the
        second run only, which is the worst kind of bug to find. `DO UPDATE` always
        returns, so the map is always complete.

        Overwrite-on-conflict, no history: no SCD Type 2 (D-014). The surrogate key makes
        versioning possible later without forcing it now.
        """
        keys: dict[str, int] = {}
        with self.connection.cursor() as cursor:
            for customer in customers:
                row = cursor.execute(
                    "INSERT INTO dim_customers"
                    " (customer_id, customer_name, region, segment, signup_date)"
                    " VALUES (%s, %s, %s, %s, %s)"
                    " ON CONFLICT (customer_id) DO UPDATE"
                    " SET customer_name = EXCLUDED.customer_name,"
                    "     region        = EXCLUDED.region,"
                    "     segment       = EXCLUDED.segment,"
                    "     signup_date   = EXCLUDED.signup_date,"
                    "     updated_at    = now()"
                    " RETURNING customer_key",
                    (
                        customer.customer_id,
                        customer.customer_name,
                        customer.region,
                        customer.segment,
                        customer.signup_date,
                    ),
                ).fetchone()
                keys[customer.customer_id] = row[0]

        logger.info("upserted %d customers", len(keys))
        return keys

    def upsert_dim_products(self, products: list[Product]) -> dict[str, int]:
        """Upsert products and return `{product_id: product_key}`. Same contract as customers."""
        keys: dict[str, int] = {}
        with self.connection.cursor() as cursor:
            for product in products:
                row = cursor.execute(
                    "INSERT INTO dim_products"
                    " (product_id, product_name, category, list_price)"
                    " VALUES (%s, %s, %s, %s)"
                    " ON CONFLICT (product_id) DO UPDATE"
                    " SET product_name = EXCLUDED.product_name,"
                    "     category     = EXCLUDED.category,"
                    "     list_price   = EXCLUDED.list_price,"
                    "     updated_at   = now()"
                    " RETURNING product_key",
                    (
                        product.product_id,
                        product.product_name,
                        product.category,
                        product.list_price,
                    ),
                ).fetchone()
                keys[product.product_id] = row[0]

        logger.info("upserted %d products", len(keys))
        return keys

    # ------------------------------------------------------------------
    # Staging, facts, rejections
    # ------------------------------------------------------------------

    def load_staging(self, records: list[ValidSalesRecord], run_id: UUID) -> int:
        """Insert validated rows into `stg_sales`, keeping natural keys and provenance.

        Staging holds typed valid records, not raw text (SPEC Q5), so §8.2's first
        reconciliation check can count it directly against `rows_valid`.
        """
        rows = [
            (
                run_id,
                record.source_file,
                record.row_num,
                record.order_id,
                record.order_date,
                record.customer_id,
                record.product_id,
                record.quantity,
                record.unit_price,
                record.discount_rate,
            )
            for record in records
        ]
        inserted = self._insert_batched(
            "INSERT INTO stg_sales (run_id, source_file, row_num, order_id, order_date,"
            " customer_id, product_id, quantity, unit_price, discount_rate)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            rows,
        )
        logger.info("staged %d rows", inserted)
        return inserted

    def load_facts(
        self,
        facts: list[FactSalesRecord],
        run_id: UUID,
        customer_keys: dict[str, int],
        product_keys: dict[str, int],
    ) -> int:
        """Insert fact rows, resolving natural keys to surrogates through the two maps.

        A missing key raises rather than inserting NULL or skipping the row. It means the
        dimensions were not loaded first, which is a programmer error in the orchestrator
        rather than a data error — and silently dropping facts would make the
        reconciliation fail with nothing to say why.
        """
        rows = []
        for fact in facts:
            try:
                customer_key = customer_keys[fact.customer_id]
                product_key = product_keys[fact.product_id]
            except KeyError as error:
                raise KeyError(
                    f"row {fact.row_num}: {error.args[0]!r} has no surrogate key. "
                    f"Dimensions must be upserted before facts (SPEC 7.2)."
                ) from None

            rows.append(
                (
                    fact.order_id,
                    fact.order_date,
                    customer_key,
                    product_key,
                    fact.quantity,
                    fact.unit_price,
                    fact.discount_rate,
                    fact.gross_sales,
                    fact.discount_amount,
                    fact.net_sales,
                    run_id,
                )
            )

        inserted = self._insert_batched(
            "INSERT INTO fact_sales (order_id, order_date, customer_key, product_key,"
            " quantity, unit_price, discount_rate, gross_sales, discount_amount,"
            " net_sales, run_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            rows,
        )
        logger.info("loaded %d fact rows", inserted)
        return inserted

    def load_rejected(self, records: list[RejectedRecord], run_id: UUID) -> int:
        """Insert quarantined rows, payload as JSONB.

        `Json(...)` wraps the dict so psycopg adapts it to `JSONB` rather than to a text
        rendering of a Python dict — the latter round-trips as a string and stops being
        queryable with JSON operators, which is most of the reason for choosing JSONB.
        """
        rows = [
            (
                run_id,
                record.source_file,
                record.row_num,
                Json(record.raw_payload),
                record.reason_code,
                record.reason_detail,
                record.rejected_at,
            )
            for record in records
        ]
        inserted = self._insert_batched(
            "INSERT INTO etl_rejected_sales (run_id, source_file, row_num, raw_payload,"
            " reason_code, reason_detail, rejected_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            rows,
        )
        logger.info("recorded %d rejected rows", inserted)
        return inserted

    # ------------------------------------------------------------------
    # Run log
    # ------------------------------------------------------------------

    def write_run_log(
        self,
        result: PipelineResult,
        *,
        source_file: str,
        started_at: datetime,
        finished_at: datetime,
    ) -> None:
        """Record the run's outcome in `etl_run_log`.

        Written once at the end with the final status, not as RUNNING then updated. Under
        §7.4 the whole load is one transaction, so a RUNNING row would never be visible to
        anyone: it would commit at the same instant as the row superseding it.

        **A consequence worth stating plainly: a FAILED run leaves no log row at all.**
        The failure rolls back the transaction and the log row is inside it, so this table
        records successes only. Making failures observable needs a second connection
        outside this transaction — a decision for the owner, raised in the Step 8b report.

        `source_file`, `started_at` and `finished_at` are parameters because
        `PipelineResult` carries none of them; it has `duration_seconds` and `dry_run`,
        for which this table has no columns. A small but real model/table mismatch.
        """
        self.connection.execute(
            "INSERT INTO etl_run_log (run_id, source_file, started_at, finished_at,"
            " rows_extracted, rows_valid, rows_rejected, rows_loaded, status)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (run_id) DO UPDATE"
            " SET finished_at    = EXCLUDED.finished_at,"
            "     rows_extracted = EXCLUDED.rows_extracted,"
            "     rows_valid     = EXCLUDED.rows_valid,"
            "     rows_rejected  = EXCLUDED.rows_rejected,"
            "     rows_loaded    = EXCLUDED.rows_loaded,"
            "     status         = EXCLUDED.status",
            (
                result.run_id,
                source_file,
                started_at,
                finished_at,
                result.rows_extracted,
                result.rows_valid,
                result.rows_rejected,
                result.rows_loaded,
                result.status,
            ),
        )
        logger.info("run %s logged as %s", result.run_id, result.status)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _insert_batched(self, sql: str, rows: list[tuple]) -> int:
        """`executemany` in chunks of `batch_size` (§7.5).

        Chunked rather than one call because a single `executemany` over a very large file
        builds the whole parameter set before sending anything; chunking bounds that
        regardless of file size. At 200 rows it makes no measurable difference, which is
        the point — the shape is already right for the file that eventually arrives with
        two million.
        """
        if not rows:
            return 0

        total = 0
        with self.connection.cursor() as cursor:
            for start in range(0, len(rows), self.batch_size):
                chunk = rows[start : start + self.batch_size]
                cursor.executemany(sql, chunk)
                total += len(chunk)
        return total
