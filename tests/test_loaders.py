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


# ======================================================================
# FactPreviewWriter (Step 7b)
# ======================================================================


def make_fact(**overrides):
    from datetime import date

    from src.models import FactSalesRecord

    fields = {
        "row_num": 40,
        "source_file": "sales_2026_01.csv",
        "order_id": 1038,
        "order_date": date(2026, 1, 15),
        "customer_id": "C019",
        "product_id": "P009",
        "quantity": 4,
        "unit_price": Decimal("25.00"),
        "discount_rate": Decimal("0.10"),
        "gross_sales": Decimal("100.00"),
        "discount_amount": Decimal("10.00"),
        "net_sales": Decimal("90.00"),
    }
    fields.update(overrides)
    return FactSalesRecord(**fields)


def test_preview_leading_columns_mirror_fact_sales_declared_order(tmp_path: Path) -> None:
    """The first ten columns must match §5's fact_sales order for a side-by-side read.

    Surrogate keys are substituted for natural ones because FactSalesRecord carries
    natural keys by design (§7.2). Provenance trails at the end because this file's
    reader asks "are the numbers right?" first, where the rejected CSV's reader asks
    "which row?" — column order follows the reader, so the two writers differ on purpose.
    """
    from src.loaders import FactPreviewWriter

    path = FactPreviewWriter(tmp_path).write([make_fact()])

    with path.open(newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))

    assert header[:10] == [
        "order_id", "order_date", "customer_id", "product_id", "quantity",
        "unit_price", "discount_rate", "gross_sales", "discount_amount", "net_sales",
    ]
    assert header[10:] == ["row_num", "source_file"]
    assert "customer_key" not in header
    assert "product_key" not in header


def test_preview_uses_the_fixed_filename_from_section_8_1(tmp_path: Path) -> None:
    from src.loaders import FactPreviewWriter

    assert FactPreviewWriter(tmp_path).write([]).name == "preview_fact_sales.csv"


def test_preview_overwrites_rather_than_accumulating(tmp_path: Path) -> None:
    """A stale preview beside a current one is a way to read the wrong numbers.

    This is the opposite of RejectedRecordWriter's behaviour, and matches §7.3: the
    database keeps the latest run, not an accumulation.
    """
    from src.loaders import FactPreviewWriter

    writer = FactPreviewWriter(tmp_path)
    first = writer.write([make_fact(order_id=1), make_fact(order_id=2)])
    second = writer.write([make_fact(order_id=3)])

    assert first == second
    assert len(list(tmp_path.glob("preview_fact_sales*.csv"))) == 1
    rows = read_rows(second)
    assert [row["order_id"] for row in rows] == ["3"]


def test_preview_header_written_with_zero_facts(tmp_path: Path) -> None:
    from src.loaders import FACT_PREVIEW_COLUMNS, FactPreviewWriter

    path = FactPreviewWriter(tmp_path).write([])

    assert path.read_text(encoding="utf-8").splitlines() == [
        ",".join(FACT_PREVIEW_COLUMNS)
    ]


def test_preview_has_no_blank_rows_between_records(tmp_path: Path) -> None:
    """Same byte-level §14 check as the rejected writer — the csv module hides this."""
    from src.loaders import FactPreviewWriter

    path = FactPreviewWriter(tmp_path).write([make_fact(order_id=n) for n in (1, 2, 3)])

    raw = path.read_bytes()

    assert raw.count(b"\r\r") == 0
    assert raw.count(b"\r\n") == 4  # header + 3 records
    assert not any(line == b"" for line in raw.split(b"\r\n")[:-1])


def test_preview_preserves_decimal_scale(tmp_path: Path) -> None:
    """90.00 must not become 90.0 or 90 — the trailing zeros are part of the value."""
    from src.loaders import FactPreviewWriter

    path = FactPreviewWriter(tmp_path).write([make_fact()])
    row = read_rows(path)[0]

    assert row["unit_price"] == "25.00"
    assert row["gross_sales"] == "100.00"
    assert row["net_sales"] == "90.00"
    assert row["discount_rate"] == "0.10"


def test_preview_writes_natural_keys(tmp_path: Path) -> None:
    from src.loaders import FactPreviewWriter

    path = FactPreviewWriter(tmp_path).write([make_fact()])
    row = read_rows(path)[0]

    assert row["customer_id"] == "C019"
    assert row["product_id"] == "P009"


def test_preview_of_the_real_172_valid_rows(tmp_path: Path) -> None:
    """End-to-end dry-run artifact: every valid row previewed, totals intact (D-024)."""
    from datetime import date

    from src.extractors import CSVExtractor, JSONExtractor
    from src.loaders import FactPreviewWriter
    from src.transformers import ReferenceDataTransformer, SalesDataTransformer
    from src.validators import SalesDataValidator, period_from_filename

    raw_dir = Path(__file__).resolve().parent.parent / "data" / "raw"
    reference = ReferenceDataTransformer()
    customers = reference.to_customers(JSONExtractor(raw_dir / "customers.json").extract())
    products = reference.to_products(JSONExtractor(raw_dir / "products.json").extract())

    valid, _ = SalesDataValidator(
        reference.customer_ids(customers),
        reference.product_ids(products),
        today=date(2026, 8, 17),
        period=period_from_filename("sales_2026_01.csv"),
    ).validate(CSVExtractor(raw_dir / "sales_2026_01.csv").extract())

    facts = SalesDataTransformer().to_facts(valid)
    path = FactPreviewWriter(tmp_path).write(facts)
    rows = read_rows(path)

    assert len(rows) == 172
    assert sum(Decimal(row["net_sales"]) for row in rows) == Decimal("51107.07")
    assert sum(Decimal(row["gross_sales"]) for row in rows) == Decimal("58328.37")
