# Requirements coverage

For a reader confirming that every requirement in `Student_Project_Instructions.pdf` is
met, **without watching the video**. `SPEC.md` §10 is the developer-facing traceability
matrix; this is the grader-facing one, organised by the PDF's own structure rather than
by internal requirement IDs.

References are by **PDF step and printed heading**, which is what can be looked up in the
document directly.

Two things this table is designed to make obvious:

- **Three requirements have no step number.** The unnumbered Checkpoint between Steps 2
  and 3, and `PipelineConfig`, which appears only in the class-responsibility table on
  p.2. A reader ticking off "Steps 1 through 13" will not see them, and the Checkpoint is
  arguably the most substantial ask in the document.
- **The PDF asks for more edge cases than it lists.** Step 5 ends with "More edge case we
  should validate" and Step 2 with "Please Come up with more edge cases". §5 and §6 below
  separate the eleven it names from the nine added in response.

Legend: **(addition)** = not requested by the PDF. **(no PDF step)** = required by the
PDF but not assigned to a numbered step.

---

## 1. Learning goals (p.1)

| PDF item | Requirement | File | ADR | Verified by |
|---|---|---|---|---|
| p.1 goals | Separate responsibilities across classes | all of `src/` | D-014 | `SPEC.md` §3.1; `test_transformers.py::test_transformers_module_does_not_import_the_database_layer`, `test_loaders.py::test_loaders_module_does_not_import_the_validator` |
| p.1 goals | Use abstraction / inheritance | `src/extractors.py` — `Extractor(ABC)` | — | `test_extractors.py::test_extractor_base_class_cannot_be_instantiated`, `::test_concrete_extractors_are_extractors` |
| p.1 goals | Read and validate real-world messy data | `src/extractors.py`, `src/validators.py` | D-004, D-021 | `bad_records_catalogue.md` §2 — 172 valid / 28 rejected, matched row for row |
| p.1 goals | Build a dimensional model | `sql/schema.sql` | D-005 | Step 8a; six tables |
| p.1 goals | Load into PostgreSQL | `src/loaders.py` — `PostgresLoader` | D-001, D-008 | Step 8b; §8.2 reconciliation |
| p.1 goals | Handle bad records without losing them | `src/loaders.py` — `RejectedRecordWriter`; `etl_rejected_sales` | D-004, D-013 | `test_loaders.py::test_writes_the_real_twenty_eight_rejections` |
| p.1 goals | Answer business questions in SQL | `sql/analytics.sql` | — | Step 13 |

## 2. Business questions (p.1, Project Scenario)

| PDF item | Requirement | File | ADR | Verified by |
|---|---|---|---|---|
| Scenario | Revenue per day | `sql/analytics.sql` Q1 | — | Step 13 |
| Scenario | Top products by revenue | `sql/analytics.sql` Q2 | — | Step 13 |
| Scenario | Best-performing regions | `sql/analytics.sql` Q3 | — | Step 13 — regions repeat across customers by construction (`test_transformers.py::test_real_reference_data_repeats_regions_and_categories`) |
| Scenario | Highest-lifetime-value customer | `sql/analytics.sql` Q4 | — | Step 13 |
| Scenario | Which records failed and why | `sql/analytics.sql` Q5, grouped on `reason_code` | D-013, D-023 | Step 13; `reason_code` is a column, so `GROUP BY` needs no string parsing |

## 3. OOP class responsibilities (p.2 table)

The closest thing the PDF has to a file-by-file contract. Nine classes named.

| PDF item | Class | File | ADR | Verified by |
|---|---|---|---|---|
| p.2 table | `PipelineConfig` — reads paths and DB settings from environment variables **(no PDF step)** | `src/config.py` | D-007, D-018 | `python -c "...PipelineConfig.from_env()"`; password masked in `__repr__` |
| p.2 table | `Extractor` / `CSVExtractor` / `JSONExtractor` | `src/extractors.py` | D-016 | `test_extractors.py` — 200 / 20 / 15 counts |
| p.2 table | `SalesDataValidator` | `src/validators.py` | D-004, D-021, D-022 | `test_validators.py` — 28/28 primary codes vs the catalogue |
| p.2 table | `ReferenceDataTransformer` | `src/transformers.py` | D-016 | `test_transformers.py::test_real_reference_files_produce_the_expected_dimensions` |
| p.2 table | `SalesDataTransformer` | `src/transformers.py` | D-006, D-024 | `test_transformers.py` — three golden rows |
| p.2 table | `RejectedRecordWriter` | `src/loaders.py` | D-004 | `test_loaders.py` — header at zero rejects, no blank rows |
| p.2 table | `PostgresLoader` | `src/loaders.py` | D-005, D-009 | Step 8b |
| p.2 table | `SalesPipeline` | `src/pipeline.py` | D-011, D-014 | Step 10 |

## 4. Steps 1–13

| PDF item | Requirement | File | ADR | Verified by |
|---|---|---|---|---|
| Step 1 | Create the project structure | folder tree, `requirements.txt`, `README.md` | — | `pip install -r requirements.txt`; `git status` clean |
| Step 2 | Add demo input data — three files, good and bad rows | `data/raw/sales_2026_01.csv` (200 rows), `customers.json` (20), `products.json` (15) | D-012 | `test_extractors.py::test_csv_extracts_all_two_hundred_rows` |
| Step 2 | *(addition)* Generator + defect catalogue — the PDF asks for the files by hand only | `scripts/generate_demo_data.py`, `docs/bad_records_catalogue.md` | D-012 | `python scripts/generate_demo_data.py` self-checks; catalogue is the validator's oracle |
| **Checkpoint (unnumbered, between Steps 2 and 3)** | Inspect output before loading to Postgres | `--dry-run` → `data/rejected/preview_fact_sales.csv` | D-011 | `test_loaders.py::test_preview_of_the_real_172_valid_rows`; dry-run opens no connection |
| **Checkpoint** | A test environment before prod | `sales_dev` + `sales_prod` via `docker/initdb/01_create_databases.sql` | D-011 | Step 9; `\l` lists both |
| **Checkpoint** | "How to know if your answer is correct?" | `-- RECONCILIATION` in `sql/analytics.sql`; `etl_run_log` | D-024 | Four checks in `SPEC.md` §8.2; row conservation `200 == 172 + 28` |
| Step 3 *(marked OPTIONAL)* | Define data models — six dataclasses | `src/models.py` — seven, `PipelineResult` added | D-002, D-003 | `test_models.py` — all frozen, no field defaulted |
| Step 4 | Build extractor classes — abstract base + CSV + JSON. *Names ABSTRACTION* | `src/extractors.py` | D-016 | `test_extractors.py`; corrupted header raises `SchemaMismatchError` |
| Step 5 | Build the validator — valid → `ValidSalesRecord`, invalid → `RejectedRecord` with a clear reason | `src/validators.py` | D-004, D-013, D-021 | `test_validators.py` — exact catalogue agreement |
| Step 6 | Build the transformer — `Customer`/`Product` objects, and the three measure formulas | `src/transformers.py` | D-006, D-016, D-024 | `test_transformers.py::test_golden_rows`, `::test_intermediates_are_not_rounded` |
| Step 7 | Build the loader — `RejectedRecordWriter` and `PostgresLoader` | `src/loaders.py` | D-008 | Steps 7a, 8b |
| Step 7 | *(addition, no PDF step)* Fact preview writer — required by the Checkpoint, assigned to no step | `src/loaders.py` — `FactPreviewWriter` | D-011 | `test_loaders.py::test_preview_leading_columns_mirror_fact_sales_declared_order` |
| Step 8 | Add PostgreSQL schema — staging, two dimensions, fact, rejected audit | `sql/schema.sql` | D-005, D-010, D-024 | Step 8a; DDL applied twice succeeds |
| Step 8 | *(addition)* `etl_run_log` | `sql/schema.sql` | D-011 | Makes §8.2's per-run reconciliation expressible |
| Step 9 | Docker Compose for PostgreSQL | `docker-compose.yml` | D-007 | `docker compose up -d`; healthcheck passing |
| Step 10 | Pipeline orchestrator — seven-stage sequence, dry-run, run summary | `src/pipeline.py` | D-011, D-014 | Step 10; dry-run writes nothing to the database |
| **Step 11** | **Absent from the PDF** — document goes Step 10 → Step 12 | — | — | — |
| Step 11 slot | *(addition)* Entry point, CLI, logging | `main.py` | D-018 | Nonexistent file → exit code 1 |
| Step 12 | Run the full project — five expected tables in DBeaver | run transcript in `README.md` | D-009 | Two runs, identical `SUM(net_sales)` |
| Step 13 | Run analytics queries — "should verify: if the data is correct" | `sql/analytics.sql` | D-024 | Every query non-empty; reconciliation returns the expected `-0.03` |

## 5. Validation rules the PDF lists (Step 5)

Eleven checks, all implemented in `src/validators.py`, all asserted in
`tests/test_validators.py`, all with a stable `reason_code` (D-013).

| PDF item | Rule | `reason_code` | Verified by |
|---|---|---|---|
| Step 5 | Required fields present | `MISSING_FIELD` | catalogue rows 5, 99, 104 |
| Step 5 | `order_id` is an integer | `BAD_INT_ORDER_ID` | rows 9, 108 |
| Step 5 | `quantity` is an integer | `BAD_INT_QUANTITY` | row 14 |
| Step 5 | `order_date` parses as a date | `BAD_DATE_FORMAT` | row 18 |
| Step 5 | `unit_price` is a decimal | `BAD_DECIMAL_PRICE` | row 23 |
| Step 5 | `discount_rate` is a decimal | `BAD_DECIMAL_DISCOUNT` | row 27 |
| Step 5 | `quantity > 0` | `QTY_NOT_POSITIVE` | rows 32, 36, 117 |
| Step 5 | `unit_price > 0` | `PRICE_NOT_POSITIVE` | rows 41, 45 |
| Step 5 | `discount_rate` within range | `DISCOUNT_OUT_OF_RANGE` | rows 50, 54 |
| Step 5 | `customer_id` exists in reference data | `UNKNOWN_CUSTOMER` | rows 63, 113 |
| Step 5 | `product_id` exists in reference data | `UNKNOWN_PRODUCT` | row 68 |

## 6. Additional edge cases — the nine additions

The PDF invites these: Step 5 closes with "More edge case we should validate" and Step 2
with "Please Come up with more edge cases". All nine are **additions**.

| Requirement | `reason_code` | Why it matters | Verified by |
|---|---|---|---|
| *(addition)* Repeated `order_id` in one file | `DUPLICATE_ORDER_ID` | Protects the fact grain; without it a re-sent line double-counts revenue | catalogue rows 122, 160; `test_validators.py::test_a_rejected_row_does_not_reserve_its_order_id` |
| *(addition)* Wrong file shape | `SCHEMA_MISMATCH` | Fails the whole file before row processing — a bad header means every row is potentially mis-parsed. **Never appears in `etl_rejected_sales`:** it aborts the run, so the evidence is the exception and its tests (catalogue §7) | `test_extractors.py` — missing column, extra column, trailing comma, non-array JSON |
| *(addition)* `order_date` after today | `DATE_IN_FUTURE` | Classic data-entry symptom | catalogue row 72 |
| *(addition)* `order_date` outside the month in the filename | `DATE_OUT_OF_PERIOD` | Catches a wrong file dropped in the folder | row 77 |
| *(addition)* `discount_rate == 1.0` | `DISCOUNT_EQ_ONE` | 100% off posts zero revenue — suspicious, not free. The PDF's "between zero and one" does not say whether 1.0 is included | row 59; D-017 |
| *(addition)* `quantity` over threshold | `QTY_EXCEEDS_THRESHOLD` | Outlier guard, configurable via `MAX_QUANTITY` | row 81; `test_validators.py::test_max_quantity_is_configurable` |
| *(addition)* Currency-formatted price (`"$45.00"`, `"1,299.00"`) | `NON_NUMERIC_CURRENCY` | The most common real-world price defect | rows 86, 90 |
| *(addition)* ID with whitespace or wrong case | `KEY_NORMALIZED` | **Cleaned, not rejected** — stripping and upper-casing cannot change which entity an ID names. Guessing at an unknown customer would, so that stays a rejection | rows 130, 141 stay valid; `test_validators.py::test_key_normalisation_is_logged` |
| *(addition)* `unit_price` with more than two decimals | `PRICE_PRECISION` | Rejected, never silently rounded — rounding money changes the number without telling anyone | row 95 |

Plus one guard with no `reason_code` of its own:

| Requirement | Handling | Why it matters | Verified by |
|---|---|---|---|
| *(addition)* Non-finite decimals (`nan`, `Infinity`) | Rejected at the parse boundary under `BAD_DECIMAL_PRICE` / `BAD_DECIMAL_DISCOUNT` | `Decimal("nan")` parses successfully, and every IEEE-754 comparison against NaN returns `False` — so a NaN price passes *every* range check, reaches `fact_sales`, and turns `SUM(net_sales)` into NaN. The reconciliation would then report a mismatch it cannot localise | D-022; `test_validators.py::test_non_finite_prices_are_rejected` |

---

## 7. Deliberate exclusions

Stated so that absence reads as judgment rather than omission. Full list in `SPEC.md` §2.2.

| Not built | Why |
|---|---|
| Airflow / Prefect / Dagster | A cron-able `main.py` is the right size for a daily single-file batch (D-014) |
| ORM | The assignment tests SQL and OOP; an ORM hides both (D-001) |
| pydantic | Validation is the graded artifact and must be visible code (D-002) |
| SCD Type 2 | Out of scope, but the surrogate-key schema does not preclude it (D-005) |
| `CHECK (gross_sales = discount_amount + net_sales)` | Would reject three valid rows. Rounding cannot preserve both per-column accuracy and additivity (D-024) |
| `reason_codes` as a structured list | Correct fix, deliberately deferred. `reason_code` is unaffected so `GROUP BY` works; accepted debt with a stated trigger (D-023) |
