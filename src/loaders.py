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
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
from psycopg import Connection

from .models import FactSalesRecord, RejectedRecord

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
