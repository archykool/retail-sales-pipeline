"""
Deciding which records are trustworthy, and saying precisely why when they are not.

Never raises on bad data — bad data is the expected case (D-004). Every record
either becomes a `ValidSalesRecord` or a `RejectedRecord`, and the two lists always
sum to the input count, which is the invariant §8.2's reconciliation depends on.

The interesting part is not the individual rules but what happens when a row breaks
several at once. One row produces exactly one `RejectedRecord`, so something has to
choose which code is *the* code. That choice is D-021: rules are grouped into tiers
by what is knowable at each stage, and the earliest tier to fire supplies the primary
code. You cannot range-check a number that failed to parse, and you cannot
foreign-key-check a field that was empty.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import RawSalesRecord, RejectedRecord, ValidSalesRecord

logger = logging.getLogger(__name__)

# Canonical CSV column order. Ties inside a precedence tier break on position here,
# so this ordering is part of the contract, not decoration (D-021).
FIELD_ORDER = (
    "order_id",
    "order_date",
    "customer_id",
    "product_id",
    "quantity",
    "unit_price",
    "discount_rate",
)
_FIELD_INDEX = {name: index for index, name in enumerate(FIELD_ORDER)}

# Precedence tiers. Lower fires first and wins the primary reason_code.
TIER_MISSING = 1
TIER_PARSE = 2
TIER_RANGE = 3
TIER_FOREIGN_KEY = 4
TIER_FILE_SCOPE = 5

DEFAULT_MAX_QUANTITY = 1000

# Characters that mark a price as formatted-for-humans rather than machine-readable.
# Their presence produces NON_NUMERIC_CURRENCY, which is a more precise diagnosis of
# the same parse failure BAD_DECIMAL_PRICE would report, so it suppresses it.
_CURRENCY_MARKERS = frozenset("$€£¥,")

# sales_2026_01.csv -> (2026, 1). The filename is the only statement of which period
# the file is supposed to cover, which is what makes DATE_OUT_OF_PERIOD possible.
_PERIOD_IN_FILENAME = re.compile(r"(?P<year>\d{4})[_-](?P<month>\d{2})")


@dataclass(frozen=True)
class _Defect:
    """One thing wrong with one field, tagged with its precedence tier."""

    tier: int
    field: str
    code: str
    detail: str


@dataclass
class _Parsed:
    """Values that survived parsing. `None` means "not usable", never "absent".

    Downstream checks test for `None` rather than re-parsing, which is how
    suppression is enforced structurally: a field that failed to parse simply has
    nothing for the range checks to look at.
    """

    order_id: int | None = None
    order_date: date | None = None
    customer_id: str | None = None
    product_id: str | None = None
    quantity: int | None = None
    unit_price: Decimal | None = None
    discount_rate: Decimal | None = None


class SalesDataValidator:
    """Applies every §6 rule to raw sales records.

    Constructed with the valid ID sets rather than a database handle or a file path:
    the validator is a pure function of its inputs, so a test can hand it two sets
    and a list and get a deterministic answer with nothing mocked. The sets come
    from `ReferenceDataTransformer` (§7.0 step 4), which is why the transformer has
    to run before validation.
    """

    def __init__(
        self,
        customer_ids: set[str],
        product_ids: set[str],
        *,
        max_quantity: int = DEFAULT_MAX_QUANTITY,
        today: date | None = None,
        period: tuple[int, int] | None = None,
    ) -> None:
        self.customer_ids = customer_ids
        self.product_ids = product_ids
        self.max_quantity = max_quantity
        # Injected rather than read at call time so a test can pin "today" and the
        # future-date rule stays deterministic as the calendar moves.
        self.today = today or date.today()
        # None means "the filename did not state a period", in which case the
        # period rule is skipped rather than guessed at.
        self.period = period

    def validate(
        self, records: list[RawSalesRecord]
    ) -> tuple[list[ValidSalesRecord], list[RejectedRecord]]:
        """Split records into the trustworthy and the quarantined.

        Reads as a table of contents on purpose: the ordering of the four rule
        families below *is* the precedence rule, so the code and D-021 cannot drift
        apart without the difference being visible here.
        """
        valid: list[ValidSalesRecord] = []
        rejected: list[RejectedRecord] = []

        # Only records that pass reserve their order_id. A row rejected for a bad
        # price never reaches fact_sales, so it never occupies the grain, so it
        # cannot make a later row with the same id a duplicate.
        reserved_order_ids: set[int] = set()

        for record in records:
            parsed, defects = self._check_types(record)
            defects += self._check_ranges(record, parsed)
            defects += self._check_foreign_keys(record, parsed)
            defects += self._check_duplicates(parsed, reserved_order_ids)

            if defects:
                rejected.append(self._reject(record, defects))
                continue

            reserved_order_ids.add(parsed.order_id)  # type: ignore[arg-type]
            valid.append(self._accept(record, parsed))

        logger.info(
            "validated %d records: %d valid, %d rejected",
            len(records),
            len(valid),
            len(rejected),
        )
        return valid, rejected

    # ------------------------------------------------------------------
    # Tier 1 and 2 — presence, then parseability
    # ------------------------------------------------------------------

    def _check_types(self, record: RawSalesRecord) -> tuple[_Parsed, list[_Defect]]:
        """Confirm each field is present and convertible, and clean the ID fields.

        Returns whatever parsed successfully alongside the defects, because later
        tiers need the values and a second parse would be a second chance to
        disagree with this one.
        """
        parsed = _Parsed()
        defects: list[_Defect] = []

        # --- order_id -------------------------------------------------
        if not record.order_id.strip():
            defects.append(self._missing("order_id"))
        else:
            try:
                parsed.order_id = int(record.order_id.strip())
            except ValueError:
                defects.append(
                    _Defect(
                        TIER_PARSE,
                        "order_id",
                        "BAD_INT_ORDER_ID",
                        f"order_id {record.order_id!r} is not an integer",
                    )
                )

        # --- order_date -----------------------------------------------
        if not record.order_date.strip():
            defects.append(self._missing("order_date"))
        else:
            try:
                parsed.order_date = datetime.strptime(
                    record.order_date.strip(), "%Y-%m-%d"
                ).date()
            except ValueError:
                defects.append(
                    _Defect(
                        TIER_PARSE,
                        "order_date",
                        "BAD_DATE_FORMAT",
                        f"order_date {record.order_date!r} is not YYYY-MM-DD",
                    )
                )

        # --- customer_id / product_id ---------------------------------
        # Cleaned here, not rejected: stripping and upper-casing an ID cannot change
        # which entity it refers to, so the repair is safe (§6.2). Guessing at an
        # unknown ID would change the record's meaning, so that stays a rejection.
        parsed.customer_id = self._normalized_key(record, "customer_id", defects)
        parsed.product_id = self._normalized_key(record, "product_id", defects)

        # --- quantity -------------------------------------------------
        if not record.quantity.strip():
            defects.append(self._missing("quantity"))
        else:
            try:
                parsed.quantity = int(record.quantity.strip())
            except ValueError:
                defects.append(
                    _Defect(
                        TIER_PARSE,
                        "quantity",
                        "BAD_INT_QUANTITY",
                        f"quantity {record.quantity!r} is not an integer",
                    )
                )

        # --- unit_price -----------------------------------------------
        if not record.unit_price.strip():
            defects.append(self._missing("unit_price"))
        else:
            raw_price = record.unit_price.strip()
            if _CURRENCY_MARKERS & set(raw_price):
                defects.append(
                    _Defect(
                        TIER_PARSE,
                        "unit_price",
                        "NON_NUMERIC_CURRENCY",
                        f"unit_price {record.unit_price!r} carries currency "
                        f"formatting; expected a bare decimal",
                    )
                )
            else:
                parsed.unit_price = self._to_decimal(
                    raw_price, "unit_price", "BAD_DECIMAL_PRICE", defects
                )

        # --- discount_rate --------------------------------------------
        if not record.discount_rate.strip():
            defects.append(self._missing("discount_rate"))
        else:
            parsed.discount_rate = self._to_decimal(
                record.discount_rate.strip(),
                "discount_rate",
                "BAD_DECIMAL_DISCOUNT",
                defects,
            )

        return parsed, defects

    # ------------------------------------------------------------------
    # Tier 3 — ranges and periods, for values that parsed
    # ------------------------------------------------------------------

    def _check_ranges(
        self, record: RawSalesRecord, parsed: _Parsed
    ) -> list[_Defect]:
        """Range rules, skipped per-field wherever parsing produced nothing."""
        defects: list[_Defect] = []

        if parsed.order_date is not None:
            # These two always co-occur for a file whose period is in the past: any
            # date after today is also outside January 2026. DATE_IN_FUTURE is
            # appended first so it takes the primary slot (catalogue §5).
            if parsed.order_date > self.today:
                defects.append(
                    _Defect(
                        TIER_RANGE,
                        "order_date",
                        "DATE_IN_FUTURE",
                        f"order_date {parsed.order_date.isoformat()} is after "
                        f"today ({self.today.isoformat()})",
                    )
                )
            if self.period is not None:
                year, month = self.period
                if (parsed.order_date.year, parsed.order_date.month) != (year, month):
                    defects.append(
                        _Defect(
                            TIER_RANGE,
                            "order_date",
                            "DATE_OUT_OF_PERIOD",
                            f"order_date {parsed.order_date.isoformat()} is "
                            f"outside the file's period {year:04d}-{month:02d}",
                        )
                    )

        if parsed.quantity is not None:
            if parsed.quantity <= 0:
                defects.append(
                    _Defect(
                        TIER_RANGE,
                        "quantity",
                        "QTY_NOT_POSITIVE",
                        f"quantity {parsed.quantity} must be greater than zero",
                    )
                )
            elif parsed.quantity > self.max_quantity:
                defects.append(
                    _Defect(
                        TIER_RANGE,
                        "quantity",
                        "QTY_EXCEEDS_THRESHOLD",
                        f"quantity {parsed.quantity} exceeds the "
                        f"{self.max_quantity} outlier threshold",
                    )
                )

        if parsed.unit_price is not None:
            if parsed.unit_price <= 0:
                defects.append(
                    _Defect(
                        TIER_RANGE,
                        "unit_price",
                        "PRICE_NOT_POSITIVE",
                        f"unit_price {parsed.unit_price} must be greater than zero",
                    )
                )
            # Rejected, never rounded (Q3): silently rounding money changes the
            # number without telling anyone, and the rounding policy belongs to the
            # transformer's single final rounding step, not to input repair.
            if parsed.unit_price.as_tuple().exponent < -2:
                defects.append(
                    _Defect(
                        TIER_RANGE,
                        "unit_price",
                        "PRICE_PRECISION",
                        f"unit_price {record.unit_price.strip()} has more than "
                        f"two decimal places",
                    )
                )

        if parsed.discount_rate is not None:
            # DISCOUNT_EQ_ONE is the specific case of being outside [0, 1), so it
            # replaces the general code rather than joining it (D-017, D-021).
            if parsed.discount_rate == 1:
                defects.append(
                    _Defect(
                        TIER_RANGE,
                        "discount_rate",
                        "DISCOUNT_EQ_ONE",
                        "discount_rate is exactly 1.0, which posts zero revenue",
                    )
                )
            elif not (0 <= parsed.discount_rate < 1):
                defects.append(
                    _Defect(
                        TIER_RANGE,
                        "discount_rate",
                        "DISCOUNT_OUT_OF_RANGE",
                        f"discount_rate {parsed.discount_rate} is outside [0, 1)",
                    )
                )

        return defects

    # ------------------------------------------------------------------
    # Tier 4 — referential integrity
    # ------------------------------------------------------------------

    def _check_foreign_keys(
        self, record: RawSalesRecord, parsed: _Parsed
    ) -> list[_Defect]:
        """Both IDs must resolve against the reference sets.

        Checked on the *cleaned* key, so ' c007 ' resolves to C007 rather than being
        rejected for a defect that was already repaired.
        """
        defects: list[_Defect] = []

        if parsed.customer_id is not None and parsed.customer_id not in self.customer_ids:
            defects.append(
                _Defect(
                    TIER_FOREIGN_KEY,
                    "customer_id",
                    "UNKNOWN_CUSTOMER",
                    f"customer_id {parsed.customer_id!r} is not in the customer "
                    f"reference set",
                )
            )

        if parsed.product_id is not None and parsed.product_id not in self.product_ids:
            defects.append(
                _Defect(
                    TIER_FOREIGN_KEY,
                    "product_id",
                    "UNKNOWN_PRODUCT",
                    f"product_id {parsed.product_id!r} is not in the product "
                    f"reference set",
                )
            )

        return defects

    # ------------------------------------------------------------------
    # Tier 5 — uniqueness across the file
    # ------------------------------------------------------------------

    def _check_duplicates(
        self, parsed: _Parsed, reserved_order_ids: set[int]
    ) -> list[_Defect]:
        """Guard the fact grain: one order_id, one fact row.

        The only file-scoped rule here, and the only one whose verdict depends on
        earlier records — which is why it is the last tier. Without it, re-sent lines
        double-count revenue.
        """
        if parsed.order_id is None or parsed.order_id not in reserved_order_ids:
            return []

        return [
            _Defect(
                TIER_FILE_SCOPE,
                "order_id",
                "DUPLICATE_ORDER_ID",
                f"order_id {parsed.order_id} already appeared in this file",
            )
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _missing(field: str) -> _Defect:
        return _Defect(
            TIER_MISSING,
            field,
            "MISSING_FIELD",
            f"{field} is required but absent or empty",
        )

    def _normalized_key(
        self, record: RawSalesRecord, field: str, defects: list[_Defect]
    ) -> str | None:
        """Strip and upper-case an ID, logging the repair when one happens."""
        original = getattr(record, field)
        if not original.strip():
            defects.append(self._missing(field))
            return None

        cleaned = original.strip().upper()
        if cleaned != original:
            logger.info(
                "KEY_NORMALIZED %s row %d: %s %r -> %r",
                record.source_file,
                record.row_num,
                field,
                original,
                cleaned,
            )
        return cleaned

    @staticmethod
    def _to_decimal(
        raw: str, field: str, code: str, defects: list[_Defect]
    ) -> Decimal | None:
        """Parse a decimal, treating NaN and Infinity as unparseable.

        `Decimal("nan")` and `Decimal("Infinity")` succeed, and either would sail
        through the comparisons below to produce nonsense money. Rejecting them here
        keeps every arithmetic guarantee downstream honest.
        """
        try:
            value = Decimal(raw)
        except InvalidOperation:
            defects.append(
                _Defect(TIER_PARSE, field, code, f"{field} {raw!r} is not a decimal")
            )
            return None

        if not value.is_finite():
            defects.append(
                _Defect(
                    TIER_PARSE,
                    field,
                    code,
                    f"{field} {raw!r} is not a finite decimal",
                )
            )
            return None

        return value

    @staticmethod
    def _ordered(defects: list[_Defect]) -> list[_Defect]:
        """Sort by tier, then by CSV column position.

        A stable sort, so defects appended in a deliberate order within one field and
        tier — DATE_IN_FUTURE before DATE_OUT_OF_PERIOD — keep that order.
        """
        return sorted(defects, key=lambda d: (d.tier, _FIELD_INDEX[d.field]))

    def _reject(
        self, record: RawSalesRecord, defects: list[_Defect]
    ) -> RejectedRecord:
        """Build the single rejection record for a row, primary code first."""
        ordered = self._ordered(defects)

        return RejectedRecord(
            row_num=record.row_num,
            source_file=record.source_file,
            raw_payload=self._payload(record),
            reason_code=ordered[0].code,
            reason_detail="; ".join(f"{d.code}: {d.detail}" for d in ordered),
            rejected_at=datetime.now(),
        )

    @staticmethod
    def _accept(record: RawSalesRecord, parsed: _Parsed) -> ValidSalesRecord:
        """Promote a clean row, carrying provenance forward (D-020)."""
        return ValidSalesRecord(
            row_num=record.row_num,
            source_file=record.source_file,
            order_id=parsed.order_id,  # type: ignore[arg-type]
            order_date=parsed.order_date,  # type: ignore[arg-type]
            customer_id=parsed.customer_id,  # type: ignore[arg-type]
            product_id=parsed.product_id,  # type: ignore[arg-type]
            quantity=parsed.quantity,  # type: ignore[arg-type]
            unit_price=parsed.unit_price,  # type: ignore[arg-type]
            discount_rate=parsed.discount_rate,  # type: ignore[arg-type]
        )

    @staticmethod
    def _payload(record: RawSalesRecord) -> dict[str, Any]:
        """The row exactly as it came off disk, for the audit table.

        Stored un-repaired: the point of quarantine is to show what arrived, so
        somebody can fix it at the source.
        """
        return {field: getattr(record, field) for field in FIELD_ORDER}


def period_from_filename(filename: str) -> tuple[int, int] | None:
    """Extract the (year, month) a file claims to cover, or None if it does not say.

    Returning None rather than a default is deliberate: an inferred period would
    reject correct rows for being outside a month nobody stated.
    """
    match = _PERIOD_IN_FILENAME.search(filename)
    if match is None:
        return None
    year, month = int(match["year"]), int(match["month"])
    if not 1 <= month <= 12:
        return None
    return year, month
