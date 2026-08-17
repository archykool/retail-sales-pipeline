"""
Structural tests for the data contract.

The models carry no behaviour, so there is no logic here to exercise. What these
tests pin are two invariants the rest of the pipeline leans on: a record cannot
be mutated after construction, and no field can be silently omitted at
construction. A defaulted field would swallow a missing input and surface later
as a wrong number rather than an error.
"""

from dataclasses import MISSING, FrozenInstanceError, fields
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from src.models import (
    Customer,
    FactSalesRecord,
    PipelineResult,
    Product,
    RawSalesRecord,
    RejectedRecord,
    ValidSalesRecord,
)

# One fully-populated instance of every model in the contract. Values are
# arbitrary but type-correct; these tests care about structure, not content.
ALL_RECORDS = [
    RawSalesRecord(
        row_num=2,
        source_file="sales_2026_01.csv",
        order_id="100",
        order_date="2026-01-15",
        customer_id="C001",
        product_id="P001",
        quantity="5",
        unit_price="25.00",
        discount_rate="0.10",
    ),
    Customer(
        customer_id="C001",
        customer_name="Alice",
        region="West",
        segment="Premium",
        signup_date=date(2023, 1, 1),
    ),
    Product(
        product_id="P001",
        product_name="Widget",
        category="Hardware",
        list_price=Decimal("25.00"),
    ),
    ValidSalesRecord(
        row_num=2,
        source_file="sales_2026_01.csv",
        order_id=100,
        order_date=date(2026, 1, 15),
        customer_id="C001",
        product_id="P001",
        quantity=5,
        unit_price=Decimal("25.00"),
        discount_rate=Decimal("0.10"),
    ),
    FactSalesRecord(
        row_num=2,
        source_file="sales_2026_01.csv",
        order_id=100,
        order_date=date(2026, 1, 15),
        customer_id="C001",
        product_id="P001",
        quantity=5,
        unit_price=Decimal("25.00"),
        discount_rate=Decimal("0.10"),
        gross_sales=Decimal("125.00"),
        discount_amount=Decimal("12.50"),
        net_sales=Decimal("112.50"),
    ),
    RejectedRecord(
        row_num=3,
        source_file="sales_2026_01.csv",
        raw_payload={"order_id": "abc", "quantity": "-1"},
        reason_code="BAD_INT_ORDER_ID",
        reason_detail="BAD_INT_ORDER_ID: 'abc'; QTY_NOT_POSITIVE: -1",
        rejected_at=datetime(2026, 1, 17, 10, 0, 0),
    ),
    PipelineResult(
        run_id=UUID("12345678-1234-5678-1234-567812345678"),
        rows_extracted=100,
        rows_valid=95,
        rows_rejected=5,
        rows_loaded=95,
        duration_seconds=12.5,
        dry_run=False,
        status="SUCCESS",
    ),
]

ALL_MODEL_CLASSES = [type(record) for record in ALL_RECORDS]


@pytest.mark.parametrize(
    "record", ALL_RECORDS, ids=lambda record: type(record).__name__
)
def test_model_is_frozen(record: object) -> None:
    """A record must be immutable once built.

    Records are handed between pipeline stages; if any stage could mutate one in
    place, the type of a variable would stop telling you which stage it came
    from, which is the entire point of having four sales models.
    """
    first_field = fields(record)[0].name

    with pytest.raises(FrozenInstanceError):
        setattr(record, first_field, None)


@pytest.mark.parametrize(
    "model_class", ALL_MODEL_CLASSES, ids=lambda cls: cls.__name__
)
def test_model_has_no_defaulted_fields(model_class: type) -> None:
    """Every field must be supplied explicitly at construction.

    A default would let a caller omit a field and still get an object, turning a
    missing input into a plausible-looking wrong value. Failing at construction
    keeps that failure where it can be traced.
    """
    defaulted = [
        field.name
        for field in fields(model_class)
        if field.default is not MISSING or field.default_factory is not MISSING
    ]

    assert defaulted == [], (
        f"{model_class.__name__} has defaulted field(s): {defaulted}"
    )
