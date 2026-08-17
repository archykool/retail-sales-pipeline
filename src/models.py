"""
Data models representing the pipeline's state transitions.

Each model marks a boundary: untrusted strings → proven types → computed measures.
A function's type signature tells you where in the pipeline a record is.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class RawSalesRecord:
    """A sales record as it comes off disk: all fields are strings, nothing is validated.

    Built by CSVExtractor. Row numbers start at 2 (matching what a human sees in Excel).
    """
    row_num: int
    source_file: str
    order_id: str
    order_date: str
    customer_id: str
    product_id: str
    quantity: str
    unit_price: str
    discount_rate: str


@dataclass(frozen=True)
class Customer:
    """A customer from the reference JSON, typed and trusted."""
    customer_id: str
    customer_name: str
    region: str
    segment: str | None
    signup_date: date | None


@dataclass(frozen=True)
class Product:
    """A product from the reference JSON, typed and trusted."""
    product_id: str
    product_name: str
    category: str
    list_price: Decimal | None


@dataclass(frozen=True)
class ValidSalesRecord:
    """A sales record that passed validation: types are proven, foreign keys exist.

    Carries row_num and source_file forward from RawSalesRecord. Provenance must
    survive validation: stg_sales stores both columns, and when the §8.2
    reconciliation fails to balance they are the only way to locate the row in
    the original file.
    """
    row_num: int
    source_file: str
    order_id: int
    order_date: date
    customer_id: str
    product_id: str
    quantity: int
    unit_price: Decimal
    discount_rate: Decimal


@dataclass(frozen=True)
class FactSalesRecord:
    """A valid sales record with computed measures (the star schema fact row).

    Provenance is carried through to here as well, so a fact row can always be
    traced back to its line in the source file.
    """
    row_num: int
    source_file: str
    order_id: int
    order_date: date
    customer_id: str
    product_id: str
    quantity: int
    unit_price: Decimal
    discount_rate: Decimal
    gross_sales: Decimal
    discount_amount: Decimal
    net_sales: Decimal


@dataclass(frozen=True)
class RejectedRecord:
    """A record that failed validation: raw data, rejection reason code, and detail.

    One RejectedRecord per rejected row, not per defect. reason_code holds the
    primary code; reason_detail lists all defects as a semicolon-separated string.
    This preserves the row-conservation invariant: rows_extracted == rows_valid + rows_rejected.

    rejected_at is a datetime object set at rejection time (not at load time),
    so the pipeline owns the exact moment a record failed. The loader formats it
    to SQL via .isoformat(), and psycopg handles the TIMESTAMPTZ conversion.
    """
    row_num: int
    source_file: str
    raw_payload: dict[str, Any]
    reason_code: str
    reason_detail: str
    rejected_at: datetime


@dataclass(frozen=True)
class PipelineResult:
    """Summary of a pipeline run: counts, timing, and outcome."""
    run_id: UUID
    rows_extracted: int
    rows_valid: int
    rows_rejected: int
    rows_loaded: int
    duration_seconds: float
    dry_run: bool
    status: str
