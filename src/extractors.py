"""
Reading raw input off disk, and nothing else.

The single rule this layer enforces: **everything that comes off disk is
untrusted** (D-016). `CSVExtractor` hands back strings, `JSONExtractor` hands back
plain dicts, and neither builds a domain object or judges a value. That uniformity
is what lets the model layer act as a state machine — if an extractor returned a
`Customer`, "typed" would no longer mean "checked".

The one judgment this layer *does* make is structural: a file whose shape is wrong
cannot be processed row by row, so it fails immediately (D-004). A wrong-shape file
and a wrong-value row are different failure classes and get different treatment.
"""

from __future__ import annotations

import csv
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Generic, TypeVar

from .models import RawSalesRecord

T = TypeVar("T")

# The header CSVExtractor requires. Order here is the canonical column order that
# the validator's tie-breaking rule refers to (D-021); the file itself may present
# the columns in any order, since records are read by name.
EXPECTED_SALES_COLUMNS = (
    "order_id",
    "order_date",
    "customer_id",
    "product_id",
    "quantity",
    "unit_price",
    "discount_rate",
)


class SchemaMismatchError(Exception):
    """A file's overall shape is wrong, so no row in it can be trusted.

    Distinct from a bad value in a good file: that becomes a `RejectedRecord` and
    the run continues (D-004). This aborts, because a header with the wrong columns
    means every row is potentially mis-parsed, and quarantining 200 rows one at a
    time would report 200 problems where there is really only one.

    Carries the §6 reason code so the failure has the same stable identifier as a
    row-level rejection, even though it never reaches `etl_rejected_sales`.
    """

    reason_code = "SCHEMA_MISMATCH"


class Extractor(ABC, Generic[T]):
    """Base class for reading one file into a list of records.

    Parameterised by what it produces, so the abstract contract can promise
    `list[T]` while each subclass pins T to its own output type. Without the
    parameter the base class could only promise `list`, and callers would lose the
    element type at exactly the boundary where it matters most.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    @abstractmethod
    def extract(self) -> list[T]:
        """Read the file and return its records. Raises on a wrong-shaped file."""


class CSVExtractor(Extractor[RawSalesRecord]):
    """Reads the sales CSV into `RawSalesRecord`s — strings only, nothing validated."""

    def extract(self) -> list[RawSalesRecord]:
        """Read every data row, preserving its physical line number.

        `utf-8-sig` because a CSV exported from Excel usually carries a BOM, which
        under plain `utf-8` would attach itself to the first header name and make
        `order_id` unfindable. The codec is a no-op on files without one.

        `newline=""` so the csv module controls line splitting rather than the text
        layer (SPEC §14).
        """
        source_file = self.path.name
        records: list[RawSalesRecord] = []

        with self.path.open(newline="", encoding="utf-8-sig") as handle:
            # restval="" makes a short row yield empty strings rather than None,
            # keeping every field a str. The absent value then reaches the
            # validator as MISSING_FIELD, which is where that judgment belongs.
            reader = csv.DictReader(handle, restval="")
            self._require_expected_header(reader.fieldnames)

            for row in reader:
                records.append(
                    RawSalesRecord(
                        # reader.line_num, not enumerate(): DictReader silently
                        # skips blank lines, so a counter would renumber every
                        # record after one and quietly break provenance. line_num
                        # is the physical line, which is the row number a human
                        # sees in Excel.
                        row_num=reader.line_num,
                        source_file=source_file,
                        order_id=row["order_id"],
                        order_date=row["order_date"],
                        customer_id=row["customer_id"],
                        product_id=row["product_id"],
                        quantity=row["quantity"],
                        unit_price=row["unit_price"],
                        discount_rate=row["discount_rate"],
                    )
                )

        return records

    def _require_expected_header(self, fieldnames: list[str] | None) -> None:
        """Fail the file unless its columns are exactly the expected set.

        Compared as a set, not a sequence: records are read by name, so a supplier
        reordering columns changes nothing about correctness and rejecting it would
        be a false positive. Missing and extra columns are reported separately
        because they point at different causes — a dropped field versus an added one.
        """
        if fieldnames is None:
            raise SchemaMismatchError(
                f"{self.path.name}: file is empty, no header row found"
            )

        found = set(fieldnames)
        expected = set(EXPECTED_SALES_COLUMNS)

        missing = sorted(expected - found)
        extra = sorted(found - expected)

        if missing or extra:
            details = []
            if missing:
                details.append(f"missing {missing}")
            if extra:
                details.append(f"unexpected {extra}")
            raise SchemaMismatchError(
                f"{self.path.name}: header mismatch — {'; '.join(details)}. "
                f"Expected exactly {list(EXPECTED_SALES_COLUMNS)}"
            )


class JSONExtractor(Extractor[dict[str, Any]]):
    """Reads a reference JSON file into plain dicts.

    Deliberately knows nothing about customers or products. Both reference files
    are a top-level array of objects, so one class reads both and the transformer
    decides what they mean (D-016). Checking keys here would be validating, and
    validating is somebody else's file.
    """

    def extract(self) -> list[dict[str, Any]]:
        """Parse the file and confirm it is an array of objects.

        Shape is checked for the same reason the CSV header is: a JSON file that is
        an object where an array was expected is not a bad record, it is the wrong
        file, and iterating it would yield its keys as if they were rows.
        """
        with self.path.open(encoding="utf-8-sig") as handle:
            try:
                payload = json.load(handle)
            except json.JSONDecodeError as error:
                raise SchemaMismatchError(
                    f"{self.path.name}: not valid JSON — {error}"
                ) from None

        if not isinstance(payload, list):
            raise SchemaMismatchError(
                f"{self.path.name}: expected a top-level JSON array, "
                f"found {type(payload).__name__}"
            )

        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                raise SchemaMismatchError(
                    f"{self.path.name}: element {index} is "
                    f"{type(item).__name__}, expected an object"
                )

        return payload
