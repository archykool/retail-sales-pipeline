"""
Tests for the validator, against the catalogue and against edge cases it defers.

The centre of gravity is `test_catalogue_*`: `docs/bad_records_catalogue.md` was
written before this code existed (D-012), so these tests compare the validator to an
expectation it had no hand in producing. Everything below the catalogue section
covers cases the catalogue deliberately does *not* plant, because planting them would
have made its own counts ambiguous.

`TODAY` is pinned rather than taken from the clock. `DATE_IN_FUTURE` compares against
today, so an unpinned test would pass now and fail in 2027 for reasons that have
nothing to do with the code.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.extractors import CSVExtractor, JSONExtractor
from src.models import RawSalesRecord
from src.validators import SalesDataValidator, period_from_filename

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
SALES_CSV = RAW_DIR / "sales_2026_01.csv"

TODAY = date(2026, 8, 17)
PERIOD = (2026, 1)

# docs/bad_records_catalogue.md §4, transcribed: row -> primary reason_code.
CATALOGUE_REJECTIONS = {
    5: "MISSING_FIELD",
    9: "BAD_INT_ORDER_ID",
    14: "BAD_INT_QUANTITY",
    18: "BAD_DATE_FORMAT",
    23: "BAD_DECIMAL_PRICE",
    27: "BAD_DECIMAL_DISCOUNT",
    32: "QTY_NOT_POSITIVE",
    36: "QTY_NOT_POSITIVE",
    41: "PRICE_NOT_POSITIVE",
    45: "PRICE_NOT_POSITIVE",
    50: "DISCOUNT_OUT_OF_RANGE",
    54: "DISCOUNT_OUT_OF_RANGE",
    59: "DISCOUNT_EQ_ONE",
    63: "UNKNOWN_CUSTOMER",
    68: "UNKNOWN_PRODUCT",
    72: "DATE_IN_FUTURE",
    77: "DATE_OUT_OF_PERIOD",
    81: "QTY_EXCEEDS_THRESHOLD",
    86: "NON_NUMERIC_CURRENCY",
    90: "NON_NUMERIC_CURRENCY",
    95: "PRICE_PRECISION",
    99: "MISSING_FIELD",
    104: "MISSING_FIELD",
    108: "BAD_INT_ORDER_ID",
    113: "UNKNOWN_CUSTOMER",
    117: "QTY_NOT_POSITIVE",
    122: "DUPLICATE_ORDER_ID",
    160: "DUPLICATE_ORDER_ID",
}

# Catalogue §4 "also fires" column: additional codes recorded after the primary.
CATALOGUE_SECONDARY = {
    72: {"DATE_OUT_OF_PERIOD"},
    108: {"QTY_NOT_POSITIVE"},
    113: {"UNKNOWN_PRODUCT"},
    117: {"PRICE_NOT_POSITIVE", "DISCOUNT_EQ_ONE"},
}

# Catalogue §6: repaired, not rejected — these stay valid.
CATALOGUE_CLEANED = {
    130: ("customer_id", "C007"),
    141: ("product_id", "P012"),
}


def make_record(**overrides: str) -> RawSalesRecord:
    """A clean raw record, with named fields overridden.

    Defaults are deliberately valid so each test states exactly one defect and
    nothing else can be blamed for the outcome.
    """
    fields = {
        "row_num": 2,
        "source_file": "test.csv",
        "order_id": "1000",
        "order_date": "2026-01-15",
        "customer_id": "C001",
        "product_id": "P001",
        "quantity": "4",
        "unit_price": "25.00",
        "discount_rate": "0.10",
    }
    fields.update(overrides)
    return RawSalesRecord(**fields)  # type: ignore[arg-type]


def make_validator(**kwargs) -> SalesDataValidator:
    """Validator over a small synthetic reference set."""
    defaults = {
        "customer_ids": {"C001", "C002", "C007"},
        "product_ids": {"P001", "P002", "P012"},
        "today": TODAY,
        "period": PERIOD,
    }
    defaults.update(kwargs)
    return SalesDataValidator(**defaults)  # type: ignore[arg-type]


def codes_in(reason_detail: str) -> set[str]:
    """Extract reason codes from a detail string.

    Matches on the `CODE:` prefix rather than splitting on the separator, because a
    detail message may itself contain the separator character — see the note in the
    Step 5 report. Substring matching is insensitive to that choice, so these
    assertions survive a change to how details are joined.
    """
    return {
        token.rstrip(":")
        for token in reason_detail.replace(";", " ").split()
        if token.endswith(":") and token.rstrip(":").isupper()
    }


@pytest.fixture(scope="module")
def real_run() -> tuple[list, list]:
    """Validate the committed demo file once and share the result across tests."""
    sales = CSVExtractor(SALES_CSV).extract()
    customers = JSONExtractor(RAW_DIR / "customers.json").extract()
    products = JSONExtractor(RAW_DIR / "products.json").extract()

    validator = SalesDataValidator(
        customer_ids={c["customer_id"] for c in customers},
        product_ids={p["product_id"] for p in products},
        today=TODAY,
        period=period_from_filename(SALES_CSV.name),
    )
    return validator.validate(sales)


# ======================================================================
# Catalogue agreement — Step 5's exit criterion
# ======================================================================


def test_catalogue_row_conservation(real_run) -> None:
    """extracted == valid + rejected, the invariant §8.2's reconciliation rests on.

    If this ever fails, every count in the analytics output is unexplainable and the
    answer to "how do you know it's correct" collapses.
    """
    valid, rejected = real_run
    assert len(valid) + len(rejected) == 200


def test_catalogue_valid_and_rejected_counts(real_run) -> None:
    valid, rejected = real_run
    assert (len(valid), len(rejected)) == (172, 28)


@pytest.mark.parametrize(
    "row_num, expected_code", sorted(CATALOGUE_REJECTIONS.items())
)
def test_catalogue_primary_code_per_row(
    real_run, row_num: int, expected_code: str
) -> None:
    """The code the catalogue named must be the code that fired, row by row.

    Counts matching is not enough — the right total with the wrong codes would mean
    the precedence rules in D-021 are not actually implemented.
    """
    _, rejected = real_run
    by_row = {r.row_num: r for r in rejected}

    assert row_num in by_row, f"row {row_num} should have been rejected"
    assert by_row[row_num].reason_code == expected_code


def test_catalogue_rejects_exactly_the_documented_rows(real_run) -> None:
    """No extra rejections, no missing ones."""
    _, rejected = real_run
    assert {r.row_num for r in rejected} == set(CATALOGUE_REJECTIONS)


@pytest.mark.parametrize(
    "row_num, expected_extra", sorted(CATALOGUE_SECONDARY.items())
)
def test_catalogue_secondary_codes(
    real_run, row_num: int, expected_extra: set[str]
) -> None:
    """Multi-defect rows record every surviving defect, not just the primary."""
    _, rejected = real_run
    record = {r.row_num: r for r in rejected}[row_num]

    found = codes_in(record.reason_detail)
    assert expected_extra <= found
    assert record.reason_detail.startswith(record.reason_code)


def test_catalogue_multi_defect_row_yields_one_record(real_run) -> None:
    """Row 117 breaks three rules and must still produce exactly one record.

    One row in, one rejection out — otherwise row conservation stops being arithmetic.
    """
    _, rejected = real_run
    assert sum(1 for r in rejected if r.row_num == 117) == 1


@pytest.mark.parametrize(
    "row_num, field, expected",
    [(row, field, want) for row, (field, want) in sorted(CATALOGUE_CLEANED.items())],
)
def test_catalogue_cleaned_rows_stay_valid(
    real_run, row_num: int, field: str, expected: str
) -> None:
    """Whitespace and casing are repaired, so these rows reach fact_sales.

    The boundary §6.2 draws: a cosmetic defect is cleaned because the repair cannot
    change which entity the ID names; a semantic one is rejected.
    """
    valid, _ = real_run
    by_row = {v.row_num: v for v in valid}

    assert row_num in by_row, f"row {row_num} should have been cleaned, not rejected"
    assert getattr(by_row[row_num], field) == expected


def test_catalogue_per_code_counts(real_run) -> None:
    """The whole §2 count table at once, so a drift shows as one failure not twenty."""
    from collections import Counter

    _, rejected = real_run
    expected = Counter(CATALOGUE_REJECTIONS.values())
    assert Counter(r.reason_code for r in rejected) == expected


def test_catalogue_golden_row_survives_validation(real_run) -> None:
    """Row 40 is the hand-checked row; it has to be in the valid set to be useful."""
    valid, _ = real_run
    row_40 = {v.row_num: v for v in valid}[40]

    assert row_40.quantity == 4
    assert row_40.unit_price == Decimal("25.00")
    assert row_40.discount_rate == Decimal("0.10")


def test_valid_records_carry_provenance(real_run) -> None:
    """D-020: row_num and source_file survive validation, or stg_sales cannot be filled."""
    valid, _ = real_run
    assert all(v.source_file == "sales_2026_01.csv" for v in valid)
    assert all(2 <= v.row_num <= 201 for v in valid)


def test_rejected_records_keep_the_raw_payload(real_run) -> None:
    """Quarantine shows what arrived, un-repaired, so it can be fixed at the source."""
    _, rejected = real_run
    row_130_style = {r.row_num: r for r in rejected}[86]

    assert row_130_style.raw_payload["unit_price"] == "$45.00"


# ======================================================================
# Precedence and suppression (D-021), on crafted rows
# ======================================================================


def test_missing_field_suppresses_the_foreign_key_check() -> None:
    """An empty customer_id cannot also be an unknown customer — there is nothing to look up."""
    _, rejected = make_validator().validate([make_record(customer_id="")])

    assert rejected[0].reason_code == "MISSING_FIELD"
    assert "UNKNOWN_CUSTOMER" not in codes_in(rejected[0].reason_detail)


def test_parse_failure_suppresses_the_range_check() -> None:
    """An unparseable quantity has no magnitude, so no positivity verdict is possible."""
    _, rejected = make_validator().validate([make_record(quantity="three")])

    assert rejected[0].reason_code == "BAD_INT_QUANTITY"
    assert "QTY_NOT_POSITIVE" not in codes_in(rejected[0].reason_detail)


def test_currency_marker_suppresses_the_generic_decimal_code() -> None:
    """One defect diagnosed precisely, not two descriptions of the same failure."""
    _, rejected = make_validator().validate([make_record(unit_price="$45.00")])

    assert rejected[0].reason_code == "NON_NUMERIC_CURRENCY"
    assert "BAD_DECIMAL_PRICE" not in codes_in(rejected[0].reason_detail)


def test_discount_eq_one_suppresses_out_of_range() -> None:
    """1.0 is out of range, but the dedicated code says why it matters (D-017)."""
    _, rejected = make_validator().validate([make_record(discount_rate="1.00")])

    assert rejected[0].reason_code == "DISCOUNT_EQ_ONE"
    assert "DISCOUNT_OUT_OF_RANGE" not in codes_in(rejected[0].reason_detail)


def test_lower_tier_wins_over_higher_tier() -> None:
    """A parse failure outranks a range failure even in a later column."""
    _, rejected = make_validator().validate(
        [make_record(order_id="ORD-X", quantity="-2")]
    )

    assert rejected[0].reason_code == "BAD_INT_ORDER_ID"
    assert "QTY_NOT_POSITIVE" in codes_in(rejected[0].reason_detail)


def test_same_tier_ties_break_on_column_order() -> None:
    """customer_id precedes product_id in the CSV, so it takes the primary slot."""
    _, rejected = make_validator().validate(
        [make_record(customer_id="C999", product_id="P999")]
    )

    assert rejected[0].reason_code == "UNKNOWN_CUSTOMER"
    assert "UNKNOWN_PRODUCT" in codes_in(rejected[0].reason_detail)


def test_future_date_outranks_out_of_period() -> None:
    """Both fire for any future date in a past period; the future one is primary."""
    _, rejected = make_validator().validate([make_record(order_date="2026-09-15")])

    assert rejected[0].reason_code == "DATE_IN_FUTURE"
    assert "DATE_OUT_OF_PERIOD" in codes_in(rejected[0].reason_detail)


# ======================================================================
# Duplicate policy — the edge the catalogue deliberately does not plant
# ======================================================================


def test_first_occurrence_wins_and_the_second_is_rejected() -> None:
    valid, rejected = make_validator().validate(
        [make_record(row_num=2, order_id="500"), make_record(row_num=3, order_id="500")]
    )

    assert [v.row_num for v in valid] == [2]
    assert rejected[0].row_num == 3
    assert rejected[0].reason_code == "DUPLICATE_ORDER_ID"


def test_a_rejected_row_does_not_reserve_its_order_id() -> None:
    """Catalogue §4's deferred case, asserted here rather than planted in the data.

    A row rejected for a bad price never reaches fact_sales, so it never occupies the
    grain, so a later row with the same order_id is the only claimant and must be
    accepted. Reserving ids from rejected rows would quarantine a perfectly good row
    because of an unrelated defect on an earlier one.
    """
    valid, rejected = make_validator().validate(
        [
            make_record(row_num=2, order_id="500", unit_price="-1.00"),
            make_record(row_num=3, order_id="500"),
        ]
    )

    assert rejected[0].reason_code == "PRICE_NOT_POSITIVE"
    assert [v.row_num for v in valid] == [3]


def test_unparseable_order_id_cannot_be_a_duplicate() -> None:
    """Without a parsed id there is nothing to compare, so tier 5 is skipped."""
    _, rejected = make_validator().validate(
        [make_record(order_id="500"), make_record(order_id="ORD-X")]
    )

    assert rejected[0].reason_code == "BAD_INT_ORDER_ID"
    assert "DUPLICATE_ORDER_ID" not in codes_in(rejected[0].reason_detail)


# ======================================================================
# Boundaries
# ======================================================================


@pytest.mark.parametrize(
    "discount, expected",
    [
        ("0.00", None),
        ("0.9999", None),
        ("1.00", "DISCOUNT_EQ_ONE"),
        ("1.50", "DISCOUNT_OUT_OF_RANGE"),
        ("-0.01", "DISCOUNT_OUT_OF_RANGE"),
    ],
)
def test_discount_rate_boundaries(discount: str, expected: str | None) -> None:
    """The [0, 1) interval, tested at both ends. D-017 turns on exactly this."""
    valid, rejected = make_validator().validate([make_record(discount_rate=discount)])

    if expected is None:
        assert len(valid) == 1 and not rejected
    else:
        assert rejected[0].reason_code == expected


@pytest.mark.parametrize(
    "quantity, expected",
    [("1", None), ("1000", None), ("1001", "QTY_EXCEEDS_THRESHOLD"), ("0", "QTY_NOT_POSITIVE")],
)
def test_quantity_threshold_boundary(quantity: str, expected: str | None) -> None:
    """The threshold is exclusive: 1000 passes, 1001 does not."""
    valid, rejected = make_validator().validate([make_record(quantity=quantity)])

    if expected is None:
        assert len(valid) == 1 and not rejected
    else:
        assert rejected[0].reason_code == expected


@pytest.mark.parametrize("price", ["25.00", "25.0", "25"])
def test_prices_up_to_two_decimals_are_accepted(price: str) -> None:
    valid, _ = make_validator().validate([make_record(unit_price=price)])
    assert len(valid) == 1


@pytest.mark.parametrize("price", ["19.999", "0.001"])
def test_prices_beyond_two_decimals_are_rejected_not_rounded(price: str) -> None:
    """Q3: silently rounding money changes the number without telling anyone."""
    _, rejected = make_validator().validate([make_record(unit_price=price)])
    assert "PRICE_PRECISION" in codes_in(rejected[0].reason_detail)


@pytest.mark.parametrize("value", ["nan", "Infinity", "-Infinity"])
def test_non_finite_prices_are_rejected(value: str) -> None:
    """Decimal() accepts these, and either would produce nonsense money downstream.

    Without this guard `nan <= 0` is False and `nan > threshold` is False, so a NaN
    price would pass every range check and land in fact_sales.
    """
    _, rejected = make_validator().validate([make_record(unit_price=value)])
    assert rejected[0].reason_code == "BAD_DECIMAL_PRICE"


def test_max_quantity_is_configurable() -> None:
    """§6.2 calls the outlier guard env-configurable, so it must not be hardcoded."""
    _, rejected = make_validator(max_quantity=10).validate([make_record(quantity="11")])
    assert rejected[0].reason_code == "QTY_EXCEEDS_THRESHOLD"


# ======================================================================
# Cleaning, period inference, and the no-raise contract
# ======================================================================


@pytest.mark.parametrize(
    "field, dirty, cleaned",
    [
        ("customer_id", " C007 ", "C007"),
        ("customer_id", "c007", "C007"),
        ("product_id", " p012 ", "P012"),
    ],
)
def test_keys_are_stripped_and_upper_cased(field: str, dirty: str, cleaned: str) -> None:
    valid, _ = make_validator().validate([make_record(**{field: dirty})])
    assert getattr(valid[0], field) == cleaned


def test_key_normalisation_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """§6.2 says cleaned defects are logged, so the repair leaves a trace.

    Without the log line the cleaning policy is invisible: the record looks as though
    it arrived clean, and KEY_NORMALIZED could never be evidenced.
    """
    with caplog.at_level(logging.INFO, logger="src.validators"):
        make_validator().validate([make_record(customer_id=" c007 ")])

    assert "KEY_NORMALIZED" in caplog.text
    assert "' c007 '" in caplog.text


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("sales_2026_01.csv", (2026, 1)),
        ("sales_2026-12.csv", (2026, 12)),
        ("sales.csv", None),
        ("sales_2026_13.csv", None),
    ],
)
def test_period_from_filename(filename: str, expected: tuple[int, int] | None) -> None:
    assert period_from_filename(filename) == expected


def test_period_rule_is_skipped_when_the_filename_is_silent() -> None:
    """An inferred period would reject correct rows for a month nobody stated."""
    valid, _ = make_validator(period=None).validate(
        [make_record(order_date="2024-06-01")]
    )
    assert len(valid) == 1


def test_validator_never_raises_on_bad_data() -> None:
    """D-004: bad data is the expected case, so every defect returns, none raises.

    A raise here would abort the run on the first bad row and lose the other 199
    rows' worth of diagnosis.
    """
    hostile = [
        make_record(order_id="", order_date="", customer_id="", product_id="",
                    quantity="", unit_price="", discount_rate=""),
        make_record(order_id="½", order_date="not-a-date", quantity="1e5",
                    unit_price="£", discount_rate="∞"),
    ]

    valid, rejected = make_validator().validate(hostile)

    assert not valid
    assert len(rejected) == 2


def test_empty_input_returns_two_empty_lists() -> None:
    assert make_validator().validate([]) == ([], [])
