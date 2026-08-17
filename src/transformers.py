"""
Turning trusted data into the shapes the warehouse wants.

Two transformers, and they run at different times (§7.0). `ReferenceDataTransformer`
runs *before* validation, because the validator's foreign-key check needs the ID sets
it produces. `SalesDataTransformer` runs *after*, because there is no point computing
money for a record that is about to be rejected. One file, two passes, and the
ordering is the least obvious thing in the pipeline.

Neither transformer touches the database. `FactSalesRecord` carries natural keys
(`customer_id`, `product_id`), never surrogate keys — resolving a surrogate key means
querying `dim_customers`, which would put a database connection inside this file and
turn §3.1's one-way dependency into a cycle. Surrogate resolution belongs to the
loader (§7.2), where the connection already lives.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .models import Customer, FactSalesRecord, Product, ValidSalesRecord

logger = logging.getLogger(__name__)

# Money is stored to two decimal places. Named because the value appears in three
# quantize calls and a literal repeated three times is a literal waiting to diverge.
CENTS = Decimal("0.01")


class ReferenceDataError(Exception):
    """Reference data is structurally unusable, so the dimensions cannot be trusted.

    Fails the run rather than skipping the row, on the same reasoning as
    `SchemaMismatchError`: a reference file with a broken entry produces a dimension
    that is quietly incomplete, and every foreign-key check afterwards is then
    measuring against the wrong set. A sales record can be quarantined and the run
    continues; a broken reference set has no equivalent containment.
    """


def normalize_key(raw: str) -> str:
    """Strip surrounding whitespace and upper-case an ID.

    Must stay identical to the validator's cleaning rule (§6.2). The validator
    normalises the sales side and this normalises the reference side; if the two ever
    disagree, every foreign-key check compares a cleaned ID against an uncleaned set
    and correct rows start failing as UNKNOWN_CUSTOMER.

    Deliberately duplicated here rather than imported from `validators`. The reference
    pass runs before validation (§7.0), so depending on the validator would invert the
    execution order in the import graph. The honest cost is one rule in two files;
    hoisting it into `models` would fix that but reaches into Step 3's file.
    """
    return raw.strip().upper()


class ReferenceDataTransformer:
    """Builds `Customer` and `Product` objects from raw parsed JSON.

    This is where the extractor deliberately stopped (D-016). Taking `list[dict]`
    rather than typed input is the point: the boundary between untrusted and trusted
    is a line of code in this file, and it is visible.
    """

    def to_customers(self, raw_records: list[dict[str, Any]]) -> list[Customer]:
        """Build the customer dimension, keyed on a normalised `customer_id`."""
        customers: list[Customer] = []
        seen: set[str] = set()

        for index, raw in enumerate(raw_records):
            customer_id = self._require_key(raw, "customer_id", index, "customer")
            if customer_id in seen:
                raise ReferenceDataError(
                    f"customer {index}: duplicate customer_id {customer_id!r}. "
                    f"Two definitions of one customer would make the dimension "
                    f"upsert order-dependent."
                )
            seen.add(customer_id)

            customers.append(
                Customer(
                    customer_id=customer_id,
                    customer_name=self._require_text(
                        raw, "customer_name", index, "customer"
                    ),
                    region=self._require_text(raw, "region", index, "customer"),
                    # segment and signup_date are `| None` in the data contract, so a
                    # missing one is a fact about the customer, not a broken file.
                    segment=self._optional_text(raw, "segment"),
                    signup_date=self._optional_date(raw, "signup_date", index),
                )
            )

        logger.info("built %d customers", len(customers))
        return customers

    def to_products(self, raw_records: list[dict[str, Any]]) -> list[Product]:
        """Build the product dimension, keyed on a normalised `product_id`."""
        products: list[Product] = []
        seen: set[str] = set()

        for index, raw in enumerate(raw_records):
            product_id = self._require_key(raw, "product_id", index, "product")
            if product_id in seen:
                raise ReferenceDataError(
                    f"product {index}: duplicate product_id {product_id!r}. "
                    f"Two definitions of one product would make the dimension "
                    f"upsert order-dependent."
                )
            seen.add(product_id)

            products.append(
                Product(
                    product_id=product_id,
                    product_name=self._require_text(
                        raw, "product_name", index, "product"
                    ),
                    category=self._require_text(raw, "category", index, "product"),
                    list_price=self._optional_decimal(raw, "list_price", index),
                )
            )

        logger.info("built %d products", len(products))
        return products

    @staticmethod
    def customer_ids(customers: list[Customer]) -> set[str]:
        """The set the validator's UNKNOWN_CUSTOMER check tests against (§7.0 step 4)."""
        return {customer.customer_id for customer in customers}

    @staticmethod
    def product_ids(products: list[Product]) -> set[str]:
        """The set the validator's UNKNOWN_PRODUCT check tests against (§7.0 step 4)."""
        return {product.product_id for product in products}

    # ------------------------------------------------------------------
    # Field helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _require_key(
        raw: dict[str, Any], field: str, index: int, kind: str
    ) -> str:
        """An entity with no ID cannot be referenced, so it cannot be tolerated."""
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ReferenceDataError(
                f"{kind} {index}: {field} is missing or empty, so nothing can "
                f"reference this record"
            )
        return normalize_key(value)

    @staticmethod
    def _require_text(
        raw: dict[str, Any], field: str, index: int, kind: str
    ) -> str:
        """Required descriptive fields — the dimension is useless without them."""
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ReferenceDataError(
                f"{kind} {index}: {field} is missing or empty"
            )
        return value.strip()

    @staticmethod
    def _optional_text(raw: dict[str, Any], field: str) -> str | None:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip()

    @staticmethod
    def _optional_date(
        raw: dict[str, Any], field: str, index: int
    ) -> date | None:
        """Parse an ISO date, or fail loudly if it is present but malformed.

        Absent is allowed; present-but-wrong is not. Silently discarding an
        unparseable date would put a NULL in the dimension that looks exactly like a
        customer who genuinely has no signup date on record.
        """
        value = raw.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if not isinstance(value, str):
            raise ReferenceDataError(
                f"customer {index}: {field} is {type(value).__name__}, expected a "
                f"YYYY-MM-DD string"
            )
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise ReferenceDataError(
                f"customer {index}: {field} {value!r} is not YYYY-MM-DD"
            ) from None

    @staticmethod
    def _optional_decimal(
        raw: dict[str, Any], field: str, index: int
    ) -> Decimal | None:
        """Parse a reference price, rejecting non-finite values (D-022).

        `Decimal(str(...))` and never `Decimal(float)`: constructing a Decimal from a
        float carries the float's binary error into the exact type chosen to avoid it.
        """
        value = raw.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            price = Decimal(str(value).strip())
        except InvalidOperation:
            raise ReferenceDataError(
                f"product {index}: {field} {value!r} is not a decimal"
            ) from None
        if not price.is_finite():
            raise ReferenceDataError(
                f"product {index}: {field} {value!r} is not a finite decimal"
            )
        return price


class SalesDataTransformer:
    """Computes the star-schema measures for validated sales records (§7.1)."""

    def to_facts(self, records: list[ValidSalesRecord]) -> list[FactSalesRecord]:
        facts = [self.to_fact(record) for record in records]
        logger.info("computed measures for %d fact rows", len(facts))
        return facts

    def to_fact(self, record: ValidSalesRecord) -> FactSalesRecord:
        """Apply §7.1's three formulas, rounding only the stored results.

        The order matters more than the arithmetic. Each measure is computed at full
        precision and quantized once on the way out; nothing downstream of a rounding
        is computed from the rounded value. Rounding `discount_amount` and then
        subtracting it would fold the rounding error into `net_sales` — the exact
        error `Decimal` was chosen to avoid, and invisible because the result still
        looks like money.

        `quantity` is an `int` and `unit_price` a `Decimal`, so every product here is
        exact; no float is created at any point.
        """
        gross_sales = record.quantity * record.unit_price
        discount_amount = gross_sales * record.discount_rate
        net_sales = gross_sales - discount_amount

        return FactSalesRecord(
            row_num=record.row_num,
            source_file=record.source_file,
            order_id=record.order_id,
            order_date=record.order_date,
            # Natural keys, not surrogate keys — see the module docstring and §7.2.
            customer_id=record.customer_id,
            product_id=record.product_id,
            quantity=record.quantity,
            unit_price=record.unit_price,
            discount_rate=record.discount_rate,
            gross_sales=self._to_cents(gross_sales),
            discount_amount=self._to_cents(discount_amount),
            net_sales=self._to_cents(net_sales),
        )

    @staticmethod
    def _to_cents(amount: Decimal) -> Decimal:
        """Quantize to 2dp with ROUND_HALF_UP.

        `ROUND_HALF_UP` and not Python's default `ROUND_HALF_EVEN`: banker's rounding
        is defensible statistically but it is not what an invoice does, and the
        assignment's expected figures assume the arithmetic a person would do by hand.
        """
        return amount.quantize(CENTS, rounding=ROUND_HALF_UP)
