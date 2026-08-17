"""
Generates the synthetic demo dataset described by docs/bad_records_catalogue.md.

The catalogue is the specification and this script is its implementation, not the
other way round (D-012). Consequently this file deliberately does **not** record
which `reason_code` each planted defect should produce — that expectation lives in
the catalogue alone, so there is exactly one place to change it and exactly one
document the validator can be measured against.

Nothing here is random. Every clean row derives its values from its own row
number, so the catalogue can assert that row 122 duplicates row 3's `order_id` and
a reader can confirm it by opening the CSV rather than by re-running this script.
A seeded RNG would be reproducible too, but not *inspectable*.

Lives in scripts/ rather than src/, so it is outside the §3.1 dependency graph and
imports nothing from the pipeline: it exists to produce inputs, not to process
them. print() is therefore fine here — the "logging only inside src/" rule does
not reach a developer utility.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import date
from pathlib import Path

# CSV column order. The catalogue's tie-breaking rule for equal-precedence
# rejection codes refers to this order, so it is load-bearing, not cosmetic.
SALES_COLUMNS = [
    "order_id",
    "order_date",
    "customer_id",
    "product_id",
    "quantity",
    "unit_price",
    "discount_rate",
]

FIRST_DATA_ROW = 2  # row 1 is the header; row numbers match what Excel displays
TOTAL_DATA_ROWS = 200
LAST_DATA_ROW = FIRST_DATA_ROW + TOTAL_DATA_ROWS - 1  # 201

# Cycle lengths are coprime-ish on purpose: 7 against 6 means the quantity/price
# cycle and the discount cycle drift against each other, so row 40 lands on
# qty=4, price=25.00, discount=0.10 — the hand-checked golden row in SPEC §8.2.
# Equal or divisible lengths would lock the two cycles in phase and that
# combination would never occur in the file.
QUANTITIES = [1, 2, 3, 4, 5, 8, 12]
UNIT_PRICES = ["9.99", "12.50", "19.99", "25.00", "34.95", "59.99", "129.99"]
DISCOUNT_RATES = ["0.00", "0.05", "0.10", "0.15", "0.20", "0.25"]

CUSTOMER_COUNT = 20
PRODUCT_COUNT = 15

REGIONS = ["North", "South", "East", "West"]
SEGMENTS = ["Retail", "Wholesale", "Enterprise"]
CATEGORIES = ["Hardware", "Accessories", "Peripherals"]

CUSTOMER_NAMES = [
    "Alderman Supply", "Brightwater Retail", "Cinder & Co", "Draymoor Group",
    "Eastvale Trading", "Foxglove Stores", "Granite Wholesale", "Harrowgate Ltd",
    "Ivyhurst Partners", "Jarrow Mercantile", "Kestrel Outlets", "Larkspur Trading",
    "Millbrook Depot", "Northcliff Retail", "Oakhaven Supply", "Pinecrest Group",
    "Quarrywood Ltd", "Redmarsh Stores", "Stonebridge Co", "Thornfield Supply",
]

PRODUCT_NAMES = [
    "Mechanical Keyboard", "Wireless Mouse", "USB-C Hub", "27in Monitor",
    "Laptop Stand", "Webcam 1080p", "Noise-Cancel Headset", "Docking Station",
    "Ergonomic Chair Mat", "Cable Organiser", "Portable SSD 1TB", "Desk Lamp LED",
    "Monitor Arm", "Silicone Wrist Rest", "Bluetooth Speaker",
]

PRODUCT_LIST_PRICES = [
    "89.00", "25.00", "45.00", "249.00", "34.95", "59.99", "129.99", "199.00",
    "19.99", "9.99", "119.00", "39.50", "79.00", "12.50", "64.99",
]

# Planted defects, keyed by row number, per catalogue §4 and §6. Each entry
# overrides the derived clean value for the named field(s). The comment states the
# defect; the expected reason_code is the catalogue's business, not this file's.
PLANTED: dict[int, dict[str, str]] = {
    5: {"customer_id": ""},                                      # required field empty
    9: {"order_id": "ORD-1007"},                                 # non-integer order id
    14: {"quantity": "three"},                                   # word, not digits
    18: {"order_date": "15/01/2026"},                            # day-first, not ISO
    23: {"unit_price": "abc"},                                   # unparseable price
    27: {"discount_rate": "ten percent"},                        # prose in numeric column
    32: {"quantity": "0"},                                       # zero quantity
    36: {"quantity": "-3"},                                      # negative quantity
    41: {"unit_price": "0.00"},                                  # zero price
    45: {"unit_price": "-19.99"},                                # negative price
    50: {"discount_rate": "-0.10"},                              # negative discount
    54: {"discount_rate": "1.50"},                               # discount above 1
    59: {"discount_rate": "1.00"},                               # exactly 100% off
    63: {"customer_id": "C999"},                                 # customer not in reference
    68: {"product_id": "P999"},                                  # product not in reference
    72: {"order_date": "2026-09-15"},                            # future date
    77: {"order_date": "2025-12-28"},                            # outside file's period
    81: {"quantity": "5000"},                                    # over MAX_QUANTITY
    86: {"unit_price": "$45.00"},                                # currency symbol
    90: {"unit_price": "1,299.00"},                              # thousands separator
    95: {"unit_price": "19.999"},                                # three decimal places
    99: {"order_date": ""},                                      # required field empty
    104: {"unit_price": ""},                                     # required field empty
    108: {"order_id": "ORD-X", "quantity": "-2"},                # two defects, one row
    113: {"customer_id": "C999", "product_id": "P999"},          # both keys unknown
    117: {"quantity": "0", "unit_price": "0.00",                 # three defects, one row
          "discount_rate": "1.00"},
    122: {"order_id": "1001"},                                   # duplicates row 3
    160: {"order_id": "1005"},                                   # duplicates row 7
    # Cleaned, not rejected — these two stay valid (catalogue §6).
    130: {"customer_id": " C007 "},                              # surrounding whitespace
    141: {"product_id": " p012 "},                               # whitespace + lowercase
}

# Rows whose defect is repaired rather than rejected. Tracked separately because
# they count toward rows_valid, and conflating the two would break the catalogue's
# 172/28 split.
CLEANED_ROWS = {130, 141}


def derive_clean_row(row_num: int) -> dict[str, str]:
    """Build the defect-free row for a given row number.

    Every field is a pure function of row_num. That is what lets the catalogue
    name a row and a reader verify the claim without executing anything.
    """
    offset = row_num - FIRST_DATA_ROW
    return {
        "order_id": str(1000 + offset),
        "order_date": date(2026, 1, (offset % 31) + 1).isoformat(),
        "customer_id": f"C{(offset % CUSTOMER_COUNT) + 1:03d}",
        "product_id": f"P{(offset % PRODUCT_COUNT) + 1:03d}",
        "quantity": str(QUANTITIES[offset % len(QUANTITIES)]),
        "unit_price": UNIT_PRICES[offset % len(UNIT_PRICES)],
        "discount_rate": DISCOUNT_RATES[offset % len(DISCOUNT_RATES)],
    }


def build_sales_rows() -> list[dict[str, str]]:
    """Produce all 200 data rows, clean ones derived and planted ones overridden."""
    rows = []
    for row_num in range(FIRST_DATA_ROW, LAST_DATA_ROW + 1):
        row = derive_clean_row(row_num)
        row.update(PLANTED.get(row_num, {}))
        rows.append(row)
    return rows


def build_customers() -> list[dict[str, str]]:
    """Reference customers C001..C020, with regions and segments deliberately reused.

    Repetition matters: the Step 13 "revenue by region" query needs more than one
    customer per region or every group has a single member and the aggregate proves
    nothing.
    """
    customers = []
    for index in range(CUSTOMER_COUNT):
        customers.append({
            "customer_id": f"C{index + 1:03d}",
            "customer_name": CUSTOMER_NAMES[index],
            "region": REGIONS[index % len(REGIONS)],
            "segment": SEGMENTS[index % len(SEGMENTS)],
            "signup_date": date(2023 + (index % 3), (index % 12) + 1, 15).isoformat(),
        })
    return customers


def build_products() -> list[dict[str, str]]:
    """Reference products P001..P015, categories reused for the same reason as regions."""
    products = []
    for index in range(PRODUCT_COUNT):
        products.append({
            "product_id": f"P{index + 1:03d}",
            "product_name": PRODUCT_NAMES[index],
            "category": CATEGORIES[index % len(CATEGORIES)],
            "list_price": PRODUCT_LIST_PRICES[index],
        })
    return products


def write_sales_csv(path: Path, rows: list[dict[str, str]]) -> None:
    """Write the sales CSV.

    newline="" is mandatory on Windows (SPEC §14): without it csv.writer emits
    \\r\\r\\n and every record is followed by a blank line, which the extractor
    would then have to defend against.
    """
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SALES_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, records: list[dict[str, str]]) -> None:
    """Write reference data as a top-level JSON array.

    A bare array rather than {"customers": [...]}: JSONExtractor stays generic
    because there is no wrapper key for it to know about (D-016).
    """
    with path.open("w", encoding="utf-8") as handle:
        json.dump(records, handle, indent=2)
        handle.write("\n")


def verify(rows: list[dict[str, str]], customers: list[dict[str, str]],
           products: list[dict[str, str]]) -> None:
    """Assert the structural claims the catalogue makes about this dataset.

    These check that generation matched its specification — they say nothing about
    whether the validator agrees, which is Step 5's job and must stay independent.
    """
    assert len(rows) == TOTAL_DATA_ROWS, f"expected {TOTAL_DATA_ROWS} rows, got {len(rows)}"

    rejected_count = len(PLANTED) - len(CLEANED_ROWS)
    assert rejected_count == 28, f"catalogue promises 28 rejections, planted {rejected_count}"
    assert len(CLEANED_ROWS) == 2, "catalogue promises 2 cleaned-but-valid rows"

    customer_ids = {c["customer_id"] for c in customers}
    product_ids = {p["product_id"] for p in products}
    assert len(customer_ids) == CUSTOMER_COUNT, "duplicate customer_id in reference data"
    assert len(product_ids) == PRODUCT_COUNT, "duplicate product_id in reference data"

    # The unknown-key rows depend on these being genuinely absent.
    assert "C999" not in customer_ids, "C999 must not exist (rows 63, 113)"
    assert "P999" not in product_ids, "P999 must not exist (rows 68, 113)"
    # The cleaned rows depend on these being genuinely present.
    assert "C007" in customer_ids, "C007 must exist (row 130 cleans to it)"
    assert "P012" in product_ids, "P012 must exist (row 141 cleans to it)"

    # Exactly two duplicate order_ids, both planted. Rows 9 and 108 carry
    # non-numeric ids and are excluded from the count.
    numeric_ids = [r["order_id"] for r in rows if r["order_id"].isdigit()]
    duplicates = len(numeric_ids) - len(set(numeric_ids))
    assert duplicates == 2, f"expected 2 duplicate order_ids, found {duplicates}"

    # The golden row SPEC §8.2 asks to hand-check: 4 x 25.00 less 10% = 90.00.
    golden = rows[40 - FIRST_DATA_ROW]
    assert golden["quantity"] == "4", "row 40 should carry quantity 4"
    assert golden["unit_price"] == "25.00", "row 40 should carry unit_price 25.00"
    assert golden["discount_rate"] == "0.10", "row 40 should carry discount_rate 0.10"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("data") / "raw",
        help="directory to write the three input files into (default: data/raw)",
    )
    args = parser.parse_args()

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = build_sales_rows()
    customers = build_customers()
    products = build_products()
    verify(rows, customers, products)

    write_sales_csv(out_dir / "sales_2026_01.csv", rows)
    write_json(out_dir / "customers.json", customers)
    write_json(out_dir / "products.json", products)

    print(f"wrote {len(rows)} sales rows to {out_dir / 'sales_2026_01.csv'}")
    print(f"      {len(customers)} customers to {out_dir / 'customers.json'}")
    print(f"      {len(products)} products to {out_dir / 'products.json'}")
    print()
    print(f"planted {len(PLANTED)} anomalies: "
          f"{len(PLANTED) - len(CLEANED_ROWS)} to reject, {len(CLEANED_ROWS)} to clean")
    print(f"expected split: {TOTAL_DATA_ROWS - (len(PLANTED) - len(CLEANED_ROWS))} valid / "
          f"{len(PLANTED) - len(CLEANED_ROWS)} rejected")
    print("expectations are owned by docs/bad_records_catalogue.md")


if __name__ == "__main__":
    main()
