"""
Tests for the local rejected-record writer (Step 7a).

The two that matter most are the zero-rejects header and the Windows line-ending
check. Both cover failures that produce a file which looks fine until someone opens
it — a missing file read as "nothing went wrong", or a CSV with a blank row between
every record.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from src.loaders import REJECTED_COLUMNS, RejectedRecordWriter
from src.models import RejectedRecord


def make_rejected(**overrides) -> RejectedRecord:
    fields = {
        "row_num": 117,
        "source_file": "sales_2026_01.csv",
        "raw_payload": {
            "order_id": "1115",
            "order_date": "2026-01-14",
            "customer_id": "C001",
            "product_id": "P001",
            "quantity": "0",
            "unit_price": "0.00",
            "discount_rate": "1.00",
        },
        "reason_code": "QTY_NOT_POSITIVE",
        "reason_detail": (
            "QTY_NOT_POSITIVE: quantity 0 must be greater than zero | "
            "PRICE_NOT_POSITIVE: unit_price 0.00 must be greater than zero | "
            "DISCOUNT_EQ_ONE: discount_rate is exactly 1.0, which posts zero revenue"
        ),
        "rejected_at": datetime(2026, 8, 17, 14, 30, 22),
    }
    fields.update(overrides)
    return RejectedRecord(**fields)  # type: ignore[arg-type]


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# ======================================================================
# The zero-rejects case
# ======================================================================


def test_header_is_written_even_with_no_rejections(tmp_path: Path) -> None:
    """An empty file says "nothing was wrong"; a missing file says nothing at all.

    Those two states need different responses — one is a clean run, the other is a
    writer that never executed — so they must not look identical on disk.
    """
    path = RejectedRecordWriter(tmp_path).write([])

    assert path.exists()
    assert path.read_text(encoding="utf-8").splitlines() == [",".join(REJECTED_COLUMNS)]
    assert read_rows(path) == []


def test_output_directory_is_created_if_absent(tmp_path: Path) -> None:
    """A fresh clone has no data/rejected/, and the run must not fail on that."""
    target = tmp_path / "does" / "not" / "exist"

    path = RejectedRecordWriter(target).write([])

    assert path.exists()


# ======================================================================
# Windows line endings (§14)
# ======================================================================


def test_no_blank_rows_between_records(tmp_path: Path) -> None:
    """Without newline="" csv.writer emits \\r\\r\\n and Excel shows a blank row per record.

    Checked at the byte level, because reading through the csv module hides exactly the
    defect being tested.
    """
    path = RejectedRecordWriter(tmp_path).write(
        [make_rejected(row_num=n) for n in (5, 9, 14)]
    )

    raw = path.read_bytes()

    assert raw.count(b"\r\r") == 0
    assert raw.count(b"\r\n") == 4  # header + 3 records
    assert not any(line == b"" for line in raw.split(b"\r\n")[:-1])


def test_every_record_produces_exactly_one_row(tmp_path: Path) -> None:
    path = RejectedRecordWriter(tmp_path).write(
        [make_rejected(row_num=n) for n in (5, 9, 14, 18)]
    )

    assert len(read_rows(path)) == 4


# ======================================================================
# Field content
# ======================================================================


def test_columns_are_written_in_the_declared_order(tmp_path: Path) -> None:
    path = RejectedRecordWriter(tmp_path).write([make_rejected()])

    with path.open(newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))

    assert tuple(header) == REJECTED_COLUMNS


def test_pipe_separated_detail_survives_intact(tmp_path: Path) -> None:
    """D-023's separator has to reach the file unmodified, all three codes present.

    The CSV field is quoted by the csv module because it contains commas; the point of
    this test is that quoting round-trips and the " | " boundaries are still readable.
    """
    record = make_rejected()
    path = RejectedRecordWriter(tmp_path).write([record])

    detail = read_rows(path)[0]["reason_detail"]

    assert detail == record.reason_detail
    assert detail.count(" | ") == 2
    assert [part.split(":")[0] for part in detail.split(" | ")] == [
        "QTY_NOT_POSITIVE",
        "PRICE_NOT_POSITIVE",
        "DISCOUNT_EQ_ONE",
    ]


def test_raw_payload_round_trips_as_json(tmp_path: Path) -> None:
    """The payload must be recoverable, since it is the record of what actually arrived."""
    record = make_rejected()
    path = RejectedRecordWriter(tmp_path).write([record])

    recovered = json.loads(read_rows(path)[0]["raw_payload"])

    assert recovered == record.raw_payload


def test_raw_payload_keeps_source_column_order(tmp_path: Path) -> None:
    """JSON preserves insertion order, so the payload reads like the original row."""
    path = RejectedRecordWriter(tmp_path).write([make_rejected()])

    recovered = json.loads(read_rows(path)[0]["raw_payload"])

    assert list(recovered) == [
        "order_id", "order_date", "customer_id", "product_id",
        "quantity", "unit_price", "discount_rate",
    ]


def test_non_ascii_payload_stays_legible(tmp_path: Path) -> None:
    """An audit record a person has to read should not be full of \\u escapes."""
    record = make_rejected(raw_payload={"customer_id": "Cà01"})
    path = RejectedRecordWriter(tmp_path).write([record])

    assert "Cà01" in path.read_text(encoding="utf-8")
    assert json.loads(read_rows(path)[0]["raw_payload"]) == {"customer_id": "Cà01"}


def test_provenance_and_reason_are_written(tmp_path: Path) -> None:
    path = RejectedRecordWriter(tmp_path).write([make_rejected()])
    row = read_rows(path)[0]

    assert row["source_file"] == "sales_2026_01.csv"
    assert row["row_num"] == "117"
    assert row["reason_code"] == "QTY_NOT_POSITIVE"


def test_rejected_at_is_written_as_iso_8601(tmp_path: Path) -> None:
    """A sortable, unambiguous timestamp — the DB column is TIMESTAMPTZ (§5)."""
    path = RejectedRecordWriter(tmp_path).write([make_rejected()])

    assert read_rows(path)[0]["rejected_at"] == "2026-08-17T14:30:22"


# ======================================================================
# Filenames
# ======================================================================


def test_filename_is_timestamped_and_windows_safe(tmp_path: Path) -> None:
    """No colons: an ISO timestamp in a Windows filename produces an unopenable file."""
    path = RejectedRecordWriter(tmp_path).write([])

    assert re.fullmatch(r"rejected_\d{8}_\d{6}\.csv", path.name)
    assert ":" not in path.name


def test_a_second_write_in_the_same_second_does_not_overwrite(tmp_path: Path) -> None:
    """The rejects file is the only record of what went wrong; losing it is the worst case."""
    writer = RejectedRecordWriter(tmp_path)

    first = writer.write([make_rejected(row_num=5)])
    second = writer.write([make_rejected(row_num=9)])

    assert first != second
    assert first.exists() and second.exists()
    assert read_rows(first)[0]["row_num"] == "5"
    assert read_rows(second)[0]["row_num"] == "9"


# ======================================================================
# §3.1
# ======================================================================


def test_loaders_module_does_not_import_the_validator() -> None:
    """The specific back-edge §3.1 names, and a tempting one.

    The column names this writer serialises are already defined in `validators`, so the
    shortcut is right there. Taking it would make the dependency diagram wrong.
    """
    source = (Path(__file__).resolve().parent.parent / "src" / "loaders.py").read_text(
        encoding="utf-8"
    )

    assert "from .validators" not in source
    assert "import validators" not in source


# ======================================================================
# Against real validator output
# ======================================================================


def test_writes_the_real_twenty_eight_rejections(tmp_path: Path) -> None:
    """End-to-end: the catalogue's 28 rejections reach disk with their codes intact."""
    from datetime import date

    from src.extractors import CSVExtractor, JSONExtractor
    from src.transformers import ReferenceDataTransformer
    from src.validators import SalesDataValidator, period_from_filename

    raw_dir = Path(__file__).resolve().parent.parent / "data" / "raw"
    reference = ReferenceDataTransformer()
    customers = reference.to_customers(JSONExtractor(raw_dir / "customers.json").extract())
    products = reference.to_products(JSONExtractor(raw_dir / "products.json").extract())

    _, rejected = SalesDataValidator(
        reference.customer_ids(customers),
        reference.product_ids(products),
        today=date(2026, 8, 17),
        period=period_from_filename("sales_2026_01.csv"),
    ).validate(CSVExtractor(raw_dir / "sales_2026_01.csv").extract())

    path = RejectedRecordWriter(tmp_path).write(rejected)
    rows = read_rows(path)

    assert len(rows) == 28
    assert {int(row["row_num"]) for row in rows} == {
        5, 9, 14, 18, 23, 27, 32, 36, 41, 45, 50, 54, 59, 63, 68, 72,
        77, 81, 86, 90, 95, 99, 104, 108, 113, 117, 122, 160,
    }
    assert all(json.loads(row["raw_payload"]) for row in rows)
