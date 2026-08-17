"""
Tests for both transformer passes.

The three hand-checked golden rows §8.2 asks for live here. They are the answer to
"how do you know the arithmetic is right" that does not depend on any other part of
the pipeline being correct — you can verify them with a calculator.

The most important test in this file is `test_intermediates_are_not_rounded`: it is
the only one that fails if someone rounds `discount_amount` before subtracting it, and
that mistake produces numbers that still look exactly like money.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.models import Customer, FactSalesRecord, Product, ValidSalesRecord
from src.transformers import (
    ReferenceDataTransformer,
    ReferenceDataError,
    SalesDataTransformer,
    normalize_key,
)

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


def make_valid(**overrides) -> ValidSalesRecord:
    """A validated record with the golden-row values, overridable per test."""
    fields = {
        "row_num": 40,
        "source_file": "sales_2026_01.csv",
        "order_id": 1038,
        "order_date": date(2026, 1, 15),
        "customer_id": "C001",
        "product_id": "P001",
        "quantity": 4,
        "unit_price": Decimal("25.00"),
        "discount_rate": Decimal("0.10"),
    }
    fields.update(overrides)
    return ValidSalesRecord(**fields)  # type: ignore[arg-type]


def customer_dict(**overrides) -> dict:
    raw = {
        "customer_id": "C001",
        "customer_name": "Alderman Supply",
        "region": "North",
        "segment": "Retail",
        "signup_date": "2023-01-15",
    }
    raw.update(overrides)
    return raw


def product_dict(**overrides) -> dict:
    raw = {
        "product_id": "P001",
        "product_name": "Mechanical Keyboard",
        "category": "Hardware",
        "list_price": "89.00",
    }
    raw.update(overrides)
    return raw


# ======================================================================
# §8.2 golden rows — hand-computable, no other component involved
# ======================================================================


@pytest.mark.parametrize(
    "quantity, unit_price, discount_rate, gross, discount, net",
    [
        # SPEC §8.2's own example.
        (4, "25.00", "0.10", "100.00", "10.00", "90.00"),
        # Zero discount: the no-op path must not invent a rounding.
        (1, "9.99", "0.00", "9.99", "0.00", "9.99"),
        # 59.97 * 0.25 = 14.9925 -> 14.99; 59.97 - 14.9925 = 44.9775 -> 44.98.
        (3, "19.99", "0.25", "59.97", "14.99", "44.98"),
    ],
    ids=["spec-example", "zero-discount", "quarter-off"],
)
def test_golden_rows(
    quantity: int,
    unit_price: str,
    discount_rate: str,
    gross: str,
    discount: str,
    net: str,
) -> None:
    """Three rows anyone can check by hand (§8.2 check 3)."""
    fact = SalesDataTransformer().to_fact(
        make_valid(
            quantity=quantity,
            unit_price=Decimal(unit_price),
            discount_rate=Decimal(discount_rate),
        )
    )

    assert fact.gross_sales == Decimal(gross)
    assert fact.discount_amount == Decimal(discount)
    assert fact.net_sales == Decimal(net)


def test_intermediates_are_not_rounded() -> None:
    """§7.1's central rule, and the only test that catches breaking it.

    gross = 5 * 34.95 = 174.75, so discount = 17.475 — a half-cent exactly.

      round once:        net = 174.75 - 17.475 = 157.275 -> 157.28
      round intermediate: net = 174.75 - 17.48   = 157.27

    A one-cent difference, on a value that looks like perfectly ordinary money either
    way. Nothing else in the suite distinguishes the two implementations.
    """
    fact = SalesDataTransformer().to_fact(
        make_valid(quantity=5, unit_price=Decimal("34.95"), discount_rate=Decimal("0.10"))
    )

    assert fact.discount_amount == Decimal("17.48")
    assert fact.net_sales == Decimal("157.28")  # not 157.27


def test_rounding_is_half_up_not_half_even() -> None:
    """Python's default is ROUND_HALF_EVEN, which would give a different answer here.

    gross = 12.50, rate = 0.01 -> discount = 0.125 exactly.
      ROUND_HALF_UP   -> 0.13
      ROUND_HALF_EVEN -> 0.12  (2 is already even)

    An invoice rounds half away from zero, so HALF_UP is the behaviour a person
    checking the figures by hand will expect.
    """
    fact = SalesDataTransformer().to_fact(
        make_valid(quantity=1, unit_price=Decimal("12.50"), discount_rate=Decimal("0.01"))
    )

    assert fact.discount_amount == Decimal("0.13")


def test_all_measures_are_exactly_two_decimal_places() -> None:
    fact = SalesDataTransformer().to_fact(
        make_valid(quantity=3, unit_price=Decimal("19.99"), discount_rate=Decimal("0.15"))
    )

    for measure in (fact.gross_sales, fact.discount_amount, fact.net_sales):
        assert measure.as_tuple().exponent == -2


def test_measures_are_decimal_never_float() -> None:
    """A float anywhere in the money path defeats the entire type choice (D-006)."""
    fact = SalesDataTransformer().to_fact(make_valid())

    for measure in (fact.gross_sales, fact.discount_amount, fact.net_sales):
        assert isinstance(measure, Decimal)


# ======================================================================
# Natural keys, provenance, and the §3.1 boundary
# ======================================================================


def test_fact_carries_natural_keys_not_surrogate_keys() -> None:
    """§7.2: surrogate resolution is the loader's job, not the transformer's.

    If this file ever produced customer_key it would need to query dim_customers,
    which means a database connection inside transformers.py — a back-edge against
    §3.1 and the one change that would make the dependency diagram a lie.
    """
    fact = SalesDataTransformer().to_fact(make_valid(customer_id="C007", product_id="P012"))

    assert fact.customer_id == "C007"
    assert fact.product_id == "P012"
    assert not hasattr(fact, "customer_key")
    assert not hasattr(fact, "product_key")


def test_transformers_module_does_not_import_the_database_layer() -> None:
    """Guards the §3.1 one-way rule structurally rather than by review alone.

    A back-edge here is the single rejection trigger that is hardest to spot in a
    diff, because adding one import looks harmless.
    """
    source = (Path(__file__).resolve().parent.parent / "src" / "transformers.py").read_text(
        encoding="utf-8"
    )

    assert "import psycopg" not in source
    assert "from .loaders" not in source
    assert "import loaders" not in source


def test_fact_preserves_provenance() -> None:
    """D-020: row_num and source_file survive into the fact row."""
    fact = SalesDataTransformer().to_fact(make_valid(row_num=117, source_file="x.csv"))

    assert (fact.row_num, fact.source_file) == (117, "x.csv")


def test_to_facts_preserves_order_and_count() -> None:
    records = [make_valid(row_num=n, order_id=n) for n in (2, 3, 4)]
    facts = SalesDataTransformer().to_facts(records)

    assert [f.row_num for f in facts] == [2, 3, 4]
    assert all(isinstance(f, FactSalesRecord) for f in facts)


def test_to_facts_on_empty_input() -> None:
    assert SalesDataTransformer().to_facts([]) == []


# ======================================================================
# The additive identity, which does NOT always hold — see the Step 6 report
# ======================================================================


def test_rounding_each_measure_independently_can_break_additivity() -> None:
    """Documents a real consequence of §7.1 rather than asserting it away.

    gross = 174.75, rate = 0.10 gives discount_raw = 17.475 and net_raw = 157.275.
    Both are exact half-cents, so both round *up*, and the stored triple no longer
    satisfies gross == discount + net — it is off by one cent.

    Pinned as a test because Step 8's CHECK constraints and §8.2's control totals need
    to know this before they are written. A `CHECK (gross_sales = discount_amount +
    net_sales)` would reject three of the 172 valid rows in the committed dataset.
    """
    fact = SalesDataTransformer().to_fact(
        make_valid(quantity=5, unit_price=Decimal("34.95"), discount_rate=Decimal("0.10"))
    )

    assert fact.gross_sales == Decimal("174.75")
    assert fact.discount_amount + fact.net_sales == Decimal("174.76")
    assert fact.gross_sales != fact.discount_amount + fact.net_sales


# ======================================================================
# ReferenceDataTransformer
# ======================================================================


def test_builds_customers_from_raw_dicts() -> None:
    """D-016's boundary: dicts in, domain objects out, in this file and nowhere else."""
    customers = ReferenceDataTransformer().to_customers([customer_dict()])

    assert isinstance(customers[0], Customer)
    assert customers[0].customer_name == "Alderman Supply"
    assert customers[0].signup_date == date(2023, 1, 15)


def test_builds_products_from_raw_dicts() -> None:
    products = ReferenceDataTransformer().to_products([product_dict()])

    assert isinstance(products[0], Product)
    assert products[0].list_price == Decimal("89.00")
    assert isinstance(products[0].list_price, Decimal)


@pytest.mark.parametrize("dirty", [" c001 ", "c001", "C001 "])
def test_reference_ids_are_normalised_the_same_way_as_sales_ids(dirty: str) -> None:
    """The two sides of the foreign-key check must clean IDs identically.

    If the reference set held ' c001 ' while the validator cleaned the sales row to
    'C001', the FK check would compare a cleaned value against an uncleaned set and
    reject a row that is entirely correct.
    """
    customers = ReferenceDataTransformer().to_customers([customer_dict(customer_id=dirty)])

    assert customers[0].customer_id == "C001"


def test_normalize_key_matches_the_validators_rule() -> None:
    assert normalize_key(" c007 ") == "C007"


def test_id_sets_are_what_the_validator_consumes() -> None:
    transformer = ReferenceDataTransformer()
    customers = transformer.to_customers([customer_dict(customer_id="C001"),
                                          customer_dict(customer_id="C002")])
    products = transformer.to_products([product_dict()])

    assert transformer.customer_ids(customers) == {"C001", "C002"}
    assert transformer.product_ids(products) == {"P001"}


@pytest.mark.parametrize("missing", ["segment", "signup_date"])
def test_optional_customer_fields_become_none(missing: str) -> None:
    """These are `| None` in the contract, so absence is a fact, not a defect."""
    raw = customer_dict()
    del raw[missing]

    customer = ReferenceDataTransformer().to_customers([raw])[0]

    assert getattr(customer, missing) is None


def test_optional_list_price_becomes_none() -> None:
    raw = product_dict()
    del raw["list_price"]

    assert ReferenceDataTransformer().to_products([raw])[0].list_price is None


@pytest.mark.parametrize("bad_id", ["", "   ", None])
def test_missing_customer_id_raises(bad_id) -> None:
    """An entity nothing can reference makes the dimension silently incomplete."""
    with pytest.raises(ReferenceDataError, match="customer_id"):
        ReferenceDataTransformer().to_customers([customer_dict(customer_id=bad_id)])


def test_duplicate_customer_id_raises() -> None:
    """Two definitions of one customer make the dimension upsert order-dependent."""
    with pytest.raises(ReferenceDataError, match="duplicate"):
        ReferenceDataTransformer().to_customers([customer_dict(), customer_dict()])


def test_duplicate_product_id_raises() -> None:
    with pytest.raises(ReferenceDataError, match="duplicate"):
        ReferenceDataTransformer().to_products([product_dict(), product_dict()])


def test_missing_required_customer_field_raises() -> None:
    with pytest.raises(ReferenceDataError, match="region"):
        ReferenceDataTransformer().to_customers([customer_dict(region="")])


def test_malformed_signup_date_raises_rather_than_becoming_null() -> None:
    """A discarded bad date is indistinguishable from a genuinely absent one."""
    with pytest.raises(ReferenceDataError, match="not YYYY-MM-DD"):
        ReferenceDataTransformer().to_customers(
            [customer_dict(signup_date="15/01/2023")]
        )


@pytest.mark.parametrize("bad_price", ["nan", "Infinity"])
def test_non_finite_list_price_raises(bad_price: str) -> None:
    """D-022 applies to reference prices too, for the same reason."""
    with pytest.raises(ReferenceDataError, match="finite"):
        ReferenceDataTransformer().to_products([product_dict(list_price=bad_price)])


def test_unparseable_list_price_raises() -> None:
    with pytest.raises(ReferenceDataError, match="not a decimal"):
        ReferenceDataTransformer().to_products([product_dict(list_price="ninety")])


# ======================================================================
# Against the committed reference files
# ======================================================================


def test_real_reference_files_produce_the_expected_dimensions() -> None:
    """Step 6's exit criterion: ID sets non-empty and correctly sized."""
    from src.extractors import JSONExtractor

    transformer = ReferenceDataTransformer()
    customers = transformer.to_customers(
        JSONExtractor(RAW_DIR / "customers.json").extract()
    )
    products = transformer.to_products(JSONExtractor(RAW_DIR / "products.json").extract())

    assert len(customers) == 20
    assert len(products) == 15
    assert len(transformer.customer_ids(customers)) == 20
    assert len(transformer.product_ids(products)) == 15
    assert "C007" in transformer.customer_ids(customers)
    assert "P012" in transformer.product_ids(products)


def test_real_reference_data_repeats_regions_and_categories() -> None:
    """Groups need more than one member or the Step 13 aggregates prove nothing."""
    from src.extractors import JSONExtractor

    transformer = ReferenceDataTransformer()
    customers = transformer.to_customers(
        JSONExtractor(RAW_DIR / "customers.json").extract()
    )
    products = transformer.to_products(JSONExtractor(RAW_DIR / "products.json").extract())

    assert len({c.region for c in customers}) < len(customers)
    assert len({p.category for p in products}) < len(products)
