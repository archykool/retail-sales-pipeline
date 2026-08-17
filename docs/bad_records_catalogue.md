# Bad-record catalogue — the test oracle

**Status:** written before `validators.py` exists, on purpose (D-012).

This file is the independent expectation the validator is measured against. Every
defect below was planted deliberately at a known row number. At Step 5 the
validator's output must match this table **exactly** — same rows, same primary
reason codes, same totals. "The validator caught 28 bad rows" proves nothing on
its own; it has to agree with a number written down before the validator existed.

`scripts/generate_demo_data.py` implements this document. If the two disagree,
this document is the specification and the generator is the bug.

---

## 1. Dataset shape

| File | Contents |
|---|---|
| `data/raw/sales_2026_01.csv` | header + 200 data rows (`row_num` 2–201) |
| `data/raw/customers.json` | 20 customers, `C001`–`C020` |
| `data/raw/products.json` | 15 products, `P001`–`P015` |

CSV columns, in order: `order_id, order_date, customer_id, product_id, quantity, unit_price, discount_rate`

**Deterministic construction.** The generator takes no randomness that affects
row identity. Every clean row derives its values from its own row number:

- `order_id` = `1000 + (row_num - 2)` → row 2 is `1000`, row 201 is `1199`
- `order_date` = `2026-01-{((row_num - 2) % 31) + 1}` (always inside the file's period)
- `customer_id` = `C{((row_num - 2) % 20) + 1}`, `product_id` = `P{((row_num - 2) % 15) + 1}`
- `quantity` cycles `[1, 2, 3, 4, 5, 8, 12]` and `unit_price` cycles
  `[9.99, 12.50, 19.99, 25.00, 34.95, 59.99, 129.99]`, both on a **7**-row cycle
- `discount_rate` cycles `[0.00, 0.05, 0.10, 0.15, 0.20, 0.25]` on a **6**-row cycle

**Row 40 is the golden row.** The 7-row and 6-row cycles are deliberately not
divisible by one another, so they drift against each other and row 40 lands on
`quantity=4, unit_price=25.00, discount_rate=0.10` → gross `100.00`, discount
`10.00`, net `90.00`. That is exactly the hand-checked row SPEC §8.2 asks for, so
the arithmetic can be verified against a real row of the shipped file on camera
rather than only inside a unit test. Equal or divisible cycle lengths would lock
the two in phase and that combination would never appear.

That determinism is what makes row numbers quotable here. Planted rows override
the derived values at the row numbers listed in §3 and §4.

**Row numbering.** `row_num` 1 is the header. Data starts at 2, matching what a
human sees in Excel, so a row number in this table can be typed straight into
the Go-To-Line box of a spreadsheet.

---

## 2. Expected totals — the numbers Step 5 must reproduce

| Quantity | Expected |
|---|---|
| `rows_extracted` | 200 |
| `rows_valid` | 172 |
| `rows_rejected` | 28 |
| Rejected share | 14.0% |
| Rows carrying a *cleaned* defect (still valid) | 2 |
| Rows with something deliberate about them | 30 (15.0%) |

Row conservation: `200 == 172 + 28`.

Per-code rejection counts, counted by **primary** `reason_code`:

| `reason_code` | Count | Rows |
|---|---|---|
| `MISSING_FIELD` | 3 | 5, 99, 104 |
| `BAD_INT_ORDER_ID` | 2 | 9, 108 |
| `BAD_INT_QUANTITY` | 1 | 14 |
| `BAD_DATE_FORMAT` | 1 | 18 |
| `BAD_DECIMAL_PRICE` | 1 | 23 |
| `BAD_DECIMAL_DISCOUNT` | 1 | 27 |
| `QTY_NOT_POSITIVE` | 3 | 32, 36, 117 |
| `PRICE_NOT_POSITIVE` | 2 | 41, 45 |
| `DISCOUNT_OUT_OF_RANGE` | 2 | 50, 54 |
| `DISCOUNT_EQ_ONE` | 1 | 59 |
| `UNKNOWN_CUSTOMER` | 2 | 63, 113 |
| `UNKNOWN_PRODUCT` | 1 | 68 |
| `DATE_IN_FUTURE` | 1 | 72 |
| `DATE_OUT_OF_PERIOD` | 1 | 77 |
| `QTY_EXCEEDS_THRESHOLD` | 1 | 81 |
| `NON_NUMERIC_CURRENCY` | 2 | 86, 90 |
| `PRICE_PRECISION` | 1 | 95 |
| `DUPLICATE_ORDER_ID` | 2 | 122, 160 |
| **Total** | **28** | |

### Expected measure totals — and the three cents (D-024)

The 172 valid rows produce these, to the cent:

| Measure | Total |
|---|---|
| `SUM(gross_sales)` | 58328.37 |
| `SUM(discount_amount)` | 7221.33 |
| `SUM(net_sales)` | **51107.07** |
| `SUM(gross) - SUM(discount) - SUM(net)` | **-0.03** ← expected, not a bug |

**Rows 34, 76 and 118 each contribute one cent** to that -0.03. All three are
`qty=5, price=34.95, rate=0.10`, which produces two exact half-cents at once:

```
gross        = 174.75            exact
discount_raw =  17.475  -> 17.48   rounds up
net_raw      = 157.275  -> 157.28  rounds up
               17.48 + 157.28 = 174.76,  gross = 174.75,  difference -0.01
```

Each column is the correctly rounded value of its own exact quantity, which is what
matters because each is summed independently. Additivity and per-column accuracy
cannot both survive rounding — that is a property of rounding, not a defect, and
`Decimal` does not change it (D-024 explains why: `Decimal` fixes binary
representation error, which is a different problem).

So `-0.03` is the correct answer and the Step 13 reconciliation asserts it as such.
A predicted non-zero is stronger evidence than a zero: a zero can be produced by two
errors cancelling or by a check that is not actually running, whereas -0.03 can only
be produced by the arithmetic being exactly as documented.

The constant belongs to *this dataset*. Regenerating the demo data with different
prices or rates changes it.

---

## 3. Primary-code precedence (D-021)

One bad row produces **one** `RejectedRecord` (see `RejectedRecord`'s docstring),
so when a row breaks several rules something has to decide which code is *the*
code. Without a written rule the validator and this catalogue cannot agree on
rows 72, 108, 113, and 117.

**Tier order** — the first tier that fires supplies the primary code:

1. `MISSING_FIELD` — nothing else can be checked on an absent value
2. **Parse failures** — `BAD_INT_ORDER_ID`, `BAD_INT_QUANTITY`, `BAD_DATE_FORMAT`,
   `NON_NUMERIC_CURRENCY`, `BAD_DECIMAL_PRICE`, `BAD_DECIMAL_DISCOUNT`
3. **Range and value failures** — `QTY_NOT_POSITIVE`, `QTY_EXCEEDS_THRESHOLD`,
   `PRICE_NOT_POSITIVE`, `PRICE_PRECISION`, `DISCOUNT_EQ_ONE`,
   `DISCOUNT_OUT_OF_RANGE`, `DATE_IN_FUTURE`, `DATE_OUT_OF_PERIOD`
4. **Foreign keys** — `UNKNOWN_CUSTOMER`, `UNKNOWN_PRODUCT`
5. **File-scoped** — `DUPLICATE_ORDER_ID`

Within a tier, ties break on **CSV column order**: `order_id`, `order_date`,
`customer_id`, `product_id`, `quantity`, `unit_price`, `discount_rate`.

The tiers are not arbitrary: you cannot range-check a number you failed to parse,
and you cannot foreign-key-check a field that was empty. Precedence follows what
is actually knowable at each stage.

### Suppression rules

Some codes describe the *same* failure at different precision. The more specific
one fires and the general one is **not** also recorded:

| Specific code | Suppresses | Why |
|---|---|---|
| `NON_NUMERIC_CURRENCY` | `BAD_DECIMAL_PRICE` | `"$45.00"` is one defect diagnosed precisely, not two |
| `DISCOUNT_EQ_ONE` | `DISCOUNT_OUT_OF_RANGE` | `1.00` is out of range; the dedicated code says *why it matters* (D-017) |
| `MISSING_FIELD` | every other check on that field | an absent value has no type, range, or referent |
| any parse failure | every tier-3+ check on that field | an unparsed value has no comparable magnitude |

`reason_detail` lists all *surviving* defects, semicolon-separated, primary first.

---

## 4. Planted rejections (28 rows)

`also fires` lists additional codes recorded in `reason_detail` after the primary.

| row | field | planted value | primary `reason_code` | also fires | note |
|---:|---|---|---|---|---|
| 5 | `customer_id` | `` (empty) | `MISSING_FIELD` | — | `UNKNOWN_CUSTOMER` suppressed — nothing to look up |
| 9 | `order_id` | `ORD-1007` | `BAD_INT_ORDER_ID` | — | duplicate check impossible without a parsed id |
| 14 | `quantity` | `three` | `BAD_INT_QUANTITY` | — | word instead of digits |
| 18 | `order_date` | `15/01/2026` | `BAD_DATE_FORMAT` | — | UK/EU day-first order; not `YYYY-MM-DD` |
| 23 | `unit_price` | `abc` | `BAD_DECIMAL_PRICE` | — | no currency markers, so not `NON_NUMERIC_CURRENCY` |
| 27 | `discount_rate` | `ten percent` | `BAD_DECIMAL_DISCOUNT` | — | prose in a numeric column |
| 32 | `quantity` | `0` | `QTY_NOT_POSITIVE` | — | zero-quantity line posts no revenue |
| 36 | `quantity` | `-3` | `QTY_NOT_POSITIVE` | — | negative; a return would be its own record type |
| 41 | `unit_price` | `0.00` | `PRICE_NOT_POSITIVE` | — | free item, or a missing price defaulted to zero |
| 45 | `unit_price` | `-19.99` | `PRICE_NOT_POSITIVE` | — | sign error |
| 50 | `discount_rate` | `-0.10` | `DISCOUNT_OUT_OF_RANGE` | — | a negative discount is a surcharge |
| 54 | `discount_rate` | `1.50` | `DISCOUNT_OUT_OF_RANGE` | — | 150% off; likely a percentage entered as a rate |
| 59 | `discount_rate` | `1.00` | `DISCOUNT_EQ_ONE` | — | exactly 100% off → `net_sales = 0` (D-017) |
| 63 | `customer_id` | `C999` | `UNKNOWN_CUSTOMER` | — | not in `customers.json` |
| 68 | `product_id` | `P999` | `UNKNOWN_PRODUCT` | — | not in `products.json` |
| 72 | `order_date` | `2026-09-15` | `DATE_IN_FUTURE` | `DATE_OUT_OF_PERIOD` | see §5 — these two always co-occur here |
| 77 | `order_date` | `2025-12-28` | `DATE_OUT_OF_PERIOD` | — | past, not future: isolates the period rule |
| 81 | `quantity` | `5000` | `QTY_EXCEEDS_THRESHOLD` | — | over `MAX_QUANTITY` (1000, env-configurable) |
| 86 | `unit_price` | `$45.00` | `NON_NUMERIC_CURRENCY` | — | currency symbol; `BAD_DECIMAL_PRICE` suppressed |
| 90 | `unit_price` | `1,299.00` | `NON_NUMERIC_CURRENCY` | — | thousands separator; same suppression |
| 95 | `unit_price` | `19.999` | `PRICE_PRECISION` | — | parses fine, 3 dp — rejected, never rounded (Q3) |
| 99 | `order_date` | `` (empty) | `MISSING_FIELD` | — | `BAD_DATE_FORMAT` suppressed |
| 104 | `unit_price` | `` (empty) | `MISSING_FIELD` | — | `PRICE_NOT_POSITIVE` suppressed |
| 108 | `order_id`, `quantity` | `ORD-X`, `-2` | `BAD_INT_ORDER_ID` | `QTY_NOT_POSITIVE` | tier 2 beats tier 3 |
| 113 | `customer_id`, `product_id` | `C999`, `P999` | `UNKNOWN_CUSTOMER` | `UNKNOWN_PRODUCT` | same tier; customer wins on column order |
| 117 | `quantity`, `unit_price`, `discount_rate` | `0`, `0.00`, `1.00` | `QTY_NOT_POSITIVE` | `PRICE_NOT_POSITIVE`, `DISCOUNT_EQ_ONE` | three defects, **one** rejected record |
| 122 | `order_id` | `1001` | `DUPLICATE_ORDER_ID` | — | repeats row 3's id; **row 3 stays valid**, row 122 is rejected |
| 160 | `order_id` | `1005` | `DUPLICATE_ORDER_ID` | — | repeats row 7's id; row 7 stays valid |

### Duplicate policy

First occurrence wins. Both duplicates above collide with a **clean, accepted**
row, so the rule is unambiguous in this dataset.

The related edge — *does a rejected row's `order_id` enter the seen-set?* —
answers **no**: only rows that pass validation reserve an `order_id`, because the
grain being protected is what actually lands in `fact_sales`, and a row rejected
for a bad price never occupies it. That case is deliberately **not** planted here
(it would make the counts above ambiguous); it is asserted directly in the
validator's unit tests at Step 5 instead.

---

## 5. `DATE_IN_FUTURE` and `DATE_OUT_OF_PERIOD` always co-occur

The file's period is January 2026 and today is later than that, so any date after
today is necessarily also outside January 2026. There is no value that fires
`DATE_IN_FUTURE` alone, which is why row 72 lists both and row 77 (a past date
outside the period) is what isolates the period rule.

Worth saying on camera: two rules that look independent are coupled by the
dataset, and the multi-reason `reason_detail` is what makes the coupling visible
instead of hiding one behind the other.

**This table assumes today is later than 2026-01-31.** Row 72's resolution depends on
the clock: `2026-09-15` is only a future date while the run happens before it. Were
the pipeline run before that date, row 72 would resolve as `DATE_OUT_OF_PERIOD` alone,
`DATE_IN_FUTURE`'s count would drop to zero, and §2's per-code table would be wrong —
while the 172/28 split stayed correct, since the row is rejected either way. The
validator's tests pin `today` to `2026-08-17` for exactly this reason, so the suite
does not start failing next year over a date rather than a defect.

---

## 6. Cleaned, not rejected (2 rows — both stay valid)

Per SPEC §6.2, cosmetic key defects are repaired and logged. These rows appear in
`fact_sales`, **not** in `etl_rejected_sales`, and are counted in the 172.

| row | field | planted value | cleaned to | logged code |
|---:|---|---|---|---|
| 130 | `customer_id` | `· C007 ·` (leading + trailing space) | `C007` | `KEY_NORMALIZED` |
| 141 | `product_id` | `· p012 ·` (spaces + lowercase) | `P012` | `KEY_NORMALIZED` |

(`·` marks a literal space for readability; the CSV contains plain spaces.)

The judgment line: stripping whitespace and upper-casing an ID cannot change
*which* entity is referenced, so it is safe. Guessing at an unknown customer
would change the meaning of the record, so it is not. Cosmetic defects get
cleaned; semantic ones get rejected.

**Code naming.** Row 141 carries whitespace *and* wrong case, so the code covering
both is named for the action rather than for one of the defects: SPEC §6.2 defines
`KEY_NORMALIZED` (renamed from the earlier `WHITESPACE_KEY`, which understated its
scope). One code covers both repairs because both are the same judgment — a
cosmetic fix that cannot change which entity the ID refers to.

---

## 7. `SCHEMA_MISMATCH` is not in this file

`SCHEMA_MISMATCH` fails the **whole file** at the header, before any row is
processed (D-004). It therefore cannot be a row in a file whose other 199 rows
are expected to process normally, and planting it would make every count above
unreachable.

SPEC Step 4's exit criterion — "a corrupted header raises `SchemaMismatchError`" —
is met with a header written to a `tmp_path` fixture inside the extractor tests,
keeping `data/raw/` at the three files SPEC §3 lists. Flagging it because it is
the one code in §6 with no row in this catalogue, and that absence should be
deliberate rather than look like an oversight.

The code covers three file-level shape errors, all of which abort: a CSV header
with missing or extra columns, a CSV **row** carrying more fields than the header
(no way to know which value belongs to which column, so it cannot be parsed
positionally at all), and reference JSON that is not an array of objects.

**`SCHEMA_MISMATCH` never appears in `etl_rejected_sales`.** It aborts the run, so
no row is ever written under it — unlike every other code in §6, which lands in the
rejected table. If someone asks to see one, the evidence is the raised exception and
the tests that pin it, not a database row. Worth saying plainly rather than letting
it look like a gap in the audit trail: the whole point of the code is that the run
does not reach the point where rejections are recorded.

---

## 8. Reference data requirements

The generator must satisfy these, or the table above breaks:

- `C001`–`C020` exist; **`C999` does not** (rows 63, 113)
- `P001`–`P015` exist; **`P999` does not** (rows 68, 113)
- `C007` exists (row 130 cleans to it)
- `P012` exists (row 141 cleans to it)
- every `customer_id` and `product_id` on the 170 clean rows resolves
- `customers.json`: `customer_id`, `customer_name`, `region`, `segment`, `signup_date`
- `products.json`: `product_id`, `product_name`, `category`, `list_price`

Regions and segments repeat across customers so the Step 13 "revenue by region"
query returns more than one row per group. Categories repeat across products for
the same reason.
