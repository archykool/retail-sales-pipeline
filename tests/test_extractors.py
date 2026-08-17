"""
Tests for the extractor layer.

Two kinds of test here. Counts and planted values are checked against the real
committed files in `data/raw/`, because Step 4's exit criterion is that the
extractor reads *those* files correctly. Failure modes are checked against files
built in `tmp_path`, because a corrupted header cannot be committed to `data/raw/`
without making every count in the bad-record catalogue unreachable (catalogue §7).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.extractors import (
    EXPECTED_SALES_COLUMNS,
    CSVExtractor,
    Extractor,
    JSONExtractor,
    SchemaMismatchError,
)
from src.models import RawSalesRecord

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
SALES_CSV = RAW_DIR / "sales_2026_01.csv"
CUSTOMERS_JSON = RAW_DIR / "customers.json"
PRODUCTS_JSON = RAW_DIR / "products.json"

HEADER_LINE = ",".join(EXPECTED_SALES_COLUMNS)
GOOD_ROW = "1000,2026-01-01,C001,P001,1,9.99,0.00"


def write_csv(path: Path, *lines: str) -> Path:
    """Write a CSV with explicit content, bypassing the generator."""
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
    return path


# --------------------------------------------------------------------------
# The abstraction itself
# --------------------------------------------------------------------------


def test_extractor_base_class_cannot_be_instantiated() -> None:
    """The ABC exists to be subclassed, not used.

    If this ever stops raising, someone has removed @abstractmethod and the
    contract has quietly become optional.
    """
    with pytest.raises(TypeError):
        Extractor(SALES_CSV)  # type: ignore[abstract]


@pytest.mark.parametrize("subclass", [CSVExtractor, JSONExtractor])
def test_concrete_extractors_are_extractors(subclass: type) -> None:
    assert issubclass(subclass, Extractor)


# --------------------------------------------------------------------------
# CSV: counts and provenance against the committed file
# --------------------------------------------------------------------------


def test_csv_extracts_all_two_hundred_rows() -> None:
    records = CSVExtractor(SALES_CSV).extract()
    assert len(records) == 200


def test_csv_row_numbers_span_two_to_two_hundred_and_one() -> None:
    """Row numbers must match what a spreadsheet shows, so row 1 is the header."""
    records = CSVExtractor(SALES_CSV).extract()

    assert records[0].row_num == 2
    assert records[-1].row_num == 201
    assert [r.row_num for r in records] == list(range(2, 202))


def test_csv_one_record_per_physical_line() -> None:
    """Pins the assumption `row_num` depends on: one record occupies one line.

    Asserted as a single claim rather than split across two tests, because it is one
    assumption. `row_num` is the reader's physical line counter, which jumps if a
    quoted field contains an embedded newline. Should that ever appear in the sales
    file, the count and the final row number stop agreeing and this fails — instead
    of every row number after the offending line silently drifting out of step with
    the catalogue and with what Excel displays.
    """
    records = CSVExtractor(SALES_CSV).extract()

    assert len(records) == 200
    assert records[-1].row_num == 201
    # 200 records spanning lines 2..201 leaves no room for a multi-line record.
    assert records[-1].row_num - records[0].row_num + 1 == len(records)


def test_csv_source_file_is_the_filename_not_the_path() -> None:
    """source_file is the idempotency key in §7.3, so it must not vary by machine.

    A full path would make the same file look like a different source on another
    checkout, and the delete-then-insert reload would stop matching prior runs.
    """
    records = CSVExtractor(SALES_CSV).extract()
    assert {r.source_file for r in records} == {"sales_2026_01.csv"}


def test_csv_returns_only_strings() -> None:
    """Nothing is typed yet — that is the whole point of RawSalesRecord."""
    record = CSVExtractor(SALES_CSV).extract()[0]

    assert isinstance(record, RawSalesRecord)
    for field in ("order_id", "order_date", "customer_id", "product_id",
                  "quantity", "unit_price", "discount_rate"):
        assert isinstance(getattr(record, field), str)


@pytest.mark.parametrize(
    "row_num, field, expected",
    [
        (5, "customer_id", ""),               # planted empty field
        (9, "order_id", "ORD-1007"),          # planted non-integer
        (18, "order_date", "15/01/2026"),     # planted bad date format
        (86, "unit_price", "$45.00"),         # planted currency symbol
        (95, "unit_price", "19.999"),         # planted excess precision
        (122, "order_id", "1001"),            # planted duplicate
        (40, "quantity", "4"),                # the golden row
        (40, "unit_price", "25.00"),
        (40, "discount_rate", "0.10"),
    ],
)
def test_csv_planted_values_land_on_documented_rows(
    row_num: int, field: str, expected: str
) -> None:
    """The catalogue names row numbers; the extractor has to agree with it.

    If this drifts, every count in the catalogue becomes unverifiable and Step 5
    has no oracle left.
    """
    records = {r.row_num: r for r in CSVExtractor(SALES_CSV).extract()}
    assert getattr(records[row_num], field) == expected


@pytest.mark.parametrize(
    "row_num, field, expected",
    [
        (130, "customer_id", " C007 "),
        (141, "product_id", " p012 "),
    ],
)
def test_csv_preserves_whitespace_and_case_for_the_validator(
    row_num: int, field: str, expected: str
) -> None:
    """The extractor must not tidy these up.

    Stripping here would repair the defect before anything recorded that it
    existed, so KEY_NORMALIZED could never be logged and the cleaning policy in
    §6.2 would become invisible. Cleaning is the validator's decision to make.
    """
    records = {r.row_num: r for r in CSVExtractor(SALES_CSV).extract()}
    assert getattr(records[row_num], field) == expected


# --------------------------------------------------------------------------
# CSV: structural failure modes, built in tmp_path
# --------------------------------------------------------------------------


def test_csv_blank_line_does_not_shift_row_numbers(tmp_path: Path) -> None:
    """A blank line in the middle must not renumber the rows after it.

    csv.DictReader skips blank lines, so a naive counter would hand row 4's data
    the number 3 and every later row would be off by one — provenance silently
    wrong, with nothing raising. This is why row_num comes from line_num.
    """
    path = write_csv(
        tmp_path / "blank.csv",
        HEADER_LINE,
        GOOD_ROW,          # physical line 2
        "",                # physical line 3, skipped by DictReader
        GOOD_ROW,          # physical line 4
    )

    records = CSVExtractor(path).extract()

    assert len(records) == 2
    assert [r.row_num for r in records] == [2, 4]


def test_csv_short_row_yields_empty_strings_not_none(tmp_path: Path) -> None:
    """A truncated row stays a str-only record so the validator can reject it.

    None would break the type contract here and crash the validator rather than
    producing a MISSING_FIELD rejection, turning a data problem into an outage.
    """
    path = write_csv(
        tmp_path / "short.csv",
        HEADER_LINE,
        "1000,2026-01-01,C001",  # four columns missing
    )

    record = CSVExtractor(path).extract()[0]

    assert record.quantity == ""
    assert record.unit_price == ""
    assert record.discount_rate == ""


def test_csv_strips_byte_order_mark(tmp_path: Path) -> None:
    """A BOM must not become part of the first column's name.

    Under plain utf-8 the first header would read '\\ufefforder_id' and the header
    check would report order_id missing — a confusing failure for a file that is
    actually fine.
    """
    path = tmp_path / "bom.csv"
    path.write_text("﻿" + HEADER_LINE + "\n" + GOOD_ROW + "\n", encoding="utf-8")

    records = CSVExtractor(path).extract()

    assert len(records) == 1
    assert records[0].order_id == "1000"


def test_csv_missing_column_raises_schema_mismatch(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "missing.csv",
        "order_id,order_date,customer_id,product_id,quantity,unit_price",
        "1000,2026-01-01,C001,P001,1,9.99",
    )

    with pytest.raises(SchemaMismatchError, match="discount_rate"):
        CSVExtractor(path).extract()


def test_csv_extra_column_raises_schema_mismatch(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "extra.csv",
        HEADER_LINE + ",sales_rep",
        GOOD_ROW + ",Dana",
    )

    with pytest.raises(SchemaMismatchError, match="sales_rep"):
        CSVExtractor(path).extract()


def test_csv_trailing_comma_raises_schema_mismatch(tmp_path: Path) -> None:
    """A row with more fields than the header is a shape error, not a bad value.

    Nothing in §6 covers it, and inventing a row-level code would be wrong: with
    eight values for seven columns there is no way to know which value belongs to
    which column, so the row cannot be parsed positionally at all. Silently dropping
    the surplus — DictReader's default — would discard data without saying so.
    """
    path = write_csv(
        tmp_path / "trailing.csv",
        HEADER_LINE,
        GOOD_ROW + ",",  # eight fields for seven columns
    )

    with pytest.raises(SchemaMismatchError, match="line 2 has 8 fields"):
        CSVExtractor(path).extract()


def test_csv_surplus_fields_report_their_line_number(tmp_path: Path) -> None:
    """The error has to name the offending line to be actionable."""
    path = write_csv(
        tmp_path / "surplus.csv",
        HEADER_LINE,
        GOOD_ROW,
        GOOD_ROW + ",Dana,extra",
    )

    with pytest.raises(SchemaMismatchError, match="line 3 has 9 fields"):
        CSVExtractor(path).extract()


def test_csv_reordered_columns_are_accepted(tmp_path: Path) -> None:
    """Reordering is not a defect: rows are read by name, so nothing is mis-parsed.

    Rejecting it would be a false positive on a file that is entirely correct.
    """
    path = write_csv(
        tmp_path / "reordered.csv",
        "discount_rate,unit_price,quantity,product_id,customer_id,order_date,order_id",
        "0.00,9.99,1,P001,C001,2026-01-01,1000",
    )

    record = CSVExtractor(path).extract()[0]

    assert record.order_id == "1000"
    assert record.discount_rate == "0.00"


def test_csv_empty_file_raises_schema_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")

    with pytest.raises(SchemaMismatchError, match="empty"):
        CSVExtractor(path).extract()


def test_schema_mismatch_carries_the_reason_code() -> None:
    """The abort path uses the same stable identifier as a row-level rejection."""
    assert SchemaMismatchError.reason_code == "SCHEMA_MISMATCH"


# --------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, expected_count",
    [(CUSTOMERS_JSON, 20), (PRODUCTS_JSON, 15)],
    ids=["customers", "products"],
)
def test_json_extracts_expected_counts(path: Path, expected_count: int) -> None:
    assert len(JSONExtractor(path).extract()) == expected_count


@pytest.mark.parametrize(
    "path", [CUSTOMERS_JSON, PRODUCTS_JSON], ids=["customers", "products"]
)
def test_json_returns_plain_dicts_not_domain_objects(path: Path) -> None:
    """D-016: the extractor hands back raw parsed JSON.

    If this ever returns Customer or Product objects, the rule that everything off
    disk is untrusted has been broken and the transformer has lost half its job.
    """
    records = JSONExtractor(path).extract()

    assert all(type(record) is dict for record in records)


def test_json_reference_data_keeps_expected_keys() -> None:
    """Confirms the shape the transformer will rely on, without validating values."""
    customers = JSONExtractor(CUSTOMERS_JSON).extract()

    assert set(customers[0]) == {
        "customer_id", "customer_name", "region", "segment", "signup_date",
    }


def test_json_object_instead_of_array_raises_schema_mismatch(tmp_path: Path) -> None:
    """Iterating an object would yield its keys as though they were records."""
    path = tmp_path / "wrapped.json"
    path.write_text(json.dumps({"customers": []}), encoding="utf-8")

    with pytest.raises(SchemaMismatchError, match="array"):
        JSONExtractor(path).extract()


def test_json_array_of_scalars_raises_schema_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "scalars.json"
    path.write_text(json.dumps(["C001", "C002"]), encoding="utf-8")

    with pytest.raises(SchemaMismatchError, match="element 0"):
        JSONExtractor(path).extract()


def test_json_malformed_raises_schema_mismatch(tmp_path: Path) -> None:
    """A truncated download should name the file, not surface a bare JSONDecodeError."""
    path = tmp_path / "truncated.json"
    path.write_text('[{"customer_id": "C001"', encoding="utf-8")

    with pytest.raises(SchemaMismatchError, match="not valid JSON"):
        JSONExtractor(path).extract()
