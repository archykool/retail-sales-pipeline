"""
Getting data out of the pipeline — to disk now, to PostgreSQL at Step 7b.

Two destinations, one file, because the PDF's Step 7 puts both here. What they share
is that they are the only components that write anything anywhere; what they do not
share is a failure mode, which is why the local writer runs even in dry-run and the
database loader does not exist as far as dry-run is concerned (§8.1).

Imports `models` and nothing else from the pipeline. In particular **not
`validators`** — that is the back-edge §3.1 forbids, and it would be an easy one to
introduce, since the column names this writer wants are already defined there.
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path

from .models import RejectedRecord

logger = logging.getLogger(__name__)

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
        run's rejects — the one file whose loss is least acceptable, since it is the
        only record of what went wrong.
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

        `raw_payload` is serialised as JSON in a single column, matching the `JSONB`
        column in `etl_rejected_sales` (§5). Expanding it into named source columns
        would read better in Excel, but it would mean this file knowing the sales
        column list — a third copy of it, or an import from `validators` that §3.1
        forbids. The triage path is `source_file` plus `row_num`: those send a person
        to the actual line in the actual file, and the payload is here as a record of
        what arrived, not as the thing they edit.

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
