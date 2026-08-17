# Retail Sales Pipeline

A batch ETL pipeline that reads a daily retail sales CSV plus JSON reference data, validates
every record, transforms the survivors into star-schema facts, and loads them into PostgreSQL
running in Docker. Invalid records are quarantined with a typed reason code rather than
dropped, so every source row can be accounted for. Object-oriented by layer, with a one-way
dependency rule that is enforced by a test rather than asserted in a comment.

---

## Setup

```bash
docker compose up -d                      # postgres:16, creates sales_dev and sales_prod
copy .env.example .env                    # then fill in DB_PASSWORD etc.
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

No database setting has a default — a missing variable fails at startup rather than silently
connecting somewhere else ([D-019](docs/DECISION.md)).

The schema is applied automatically on the first real run. To inspect it beforehand:

```bash
docker exec -i sales_postgres psql -U postgres -d sales_dev -f - < sql/schema.sql
```

## Run

Dry run first. It executes extract → validate → transform, writes both CSVs, and **opens no
database connection at all** — so it proves the logic on a machine with no database running:

```bash
python main.py --dry-run
```

Then the real load:

```bash
python main.py
```

Options: `--file PATH` to process a different CSV, `--log-level DEBUG|INFO|WARNING|ERROR`.
Exit code is 0 on success and 1 on any failure.

Analytics and the reconciliation checks:

```bash
docker exec -i sales_postgres psql -U postgres -d sales_dev -f - < sql/analytics.sql
```

## The arithmetic, checked by hand

Three rows anyone can verify with a calculator. **Row 40 is a real row of
`data/raw/sales_2026_01.csv`** — not a fixture — so the figure below can be traced from the
file, through the preview CSV, to `fact_sales`.

| Source | qty | unit_price | discount_rate | gross | discount | net |
|---|---|---|---|---|---|---|
| **row 40** | 4 | 25.00 | 0.10 | **100.00** | **10.00** | **90.00** |
| crafted | 1 | 9.99 | 0.00 | 9.99 | 0.00 | 9.99 |
| crafted | 3 | 19.99 | 0.25 | 59.97 | 14.99 | 44.98 |

```
row 40:   gross    = 4 × 25.00              = 100.00
          discount = 100.00 × 0.10          =  10.00
          net      = 100.00 −  10.00        =  90.00

3 × 19.99: gross    = 59.97
           discount = 59.97 × 0.25 = 14.9925 → 14.99   (rounded once, at the end)
           net      = 59.97 − 14.9925 = 44.9775 → 44.98
```

Note the third row: `net` is computed from the **unrounded** discount. Rounding intermediates
would reintroduce exactly the error `Decimal` was chosen to avoid.

One consequence is visible in the reconciliation: `SUM(gross) − SUM(discount) − SUM(net)`
comes to **−0.03**, not zero. Rows 34, 76 and 118 are all `qty=5, price=34.95, rate=0.10`,
which produces two exact half-cents at once (`17.475` and `157.275`); both round up, so each
column is individually correct while the identity is off by a cent. That is a property of
rounding, not a defect, and `Decimal` does not change it — see
[D-024](docs/DECISION.md). The reconciliation asserts the predicted `−0.03` rather than zero,
because a zero can be produced by two errors cancelling.

## Idempotency

Running the same file twice must not double revenue. Verified live:

```
--- state BEFORE the two runs ---
 net_sales | fact_rows | run_log_rows
  51107.07 |       172 |            4

--- after run 1 ---
 net_sales | fact_rows | run_log_rows
  51107.07 |       172 |            5

--- after run 2 ---
 net_sales | fact_rows | run_log_rows
  51107.07 |       172 |            6
```

`SUM(net_sales)` and the fact row count are unchanged; `run_log_rows` climbs by one per run
because `etl_run_log` is the one table a reload does **not** replace — it records runs, not
data, and a history that erases itself is not a history ([D-025](docs/DECISION.md)). Every
row in it carries `rows_loaded = 172`; a dry run would appear as `0`, and none does.

Full reconciliation on the loaded data: `200 = 172 + 28`, `staged = facts = 172`,
0 orphan dimension references, 0 rows in both staging and rejections, 0 in neither, and all
172 measures recomputed independently in SQL agreeing with the Python transformer.

## Build order and PDF steps

Step labels are the assignment PDF's own. **The PDF's numbering is a contract, not a build
order** — it puts Docker Compose at Step 9, after the loader at Steps 7–8, and you can write
a loader before a database exists but you cannot test one. So the build ran in dependency
order with PDF-true labels. Two-way map in [SPEC §9.0](docs/SPEC.md).

| # | Step | PDF anchor | Commit |
|---|---|---|---|
| 1 | Step 1 — project structure | 1 | `4c46b0e` |
| 2 | Step 9 — Docker Compose | 9 | `072c1be`, `4dde8ca` |
| 3 | Step 1+ — configuration | none; class table p.2 | `f874028`, `0c85333`, `070a047` |
| 4 | Step 3 — data models | 3 *(optional)* | `d4b11c7` |
| 5 | Step 2 — demo data + catalogue | 2 | `17ce6ce` |
| 6 | Step 4 — extractors | 4 | `bd2dce6`, `7a29127` |
| 7 | Step 5 — validator | 5 | `6f05913` |
| 8 | Step 6 — transformers | 6 | `ddf3c5f` |
| 9 | Step 7a — rejected-record writer | 7 | `41d8eb4` |
| 10 | Step 7b — fact preview writer | none; required by §8.1 | `45b54a7` |
| 11 | Step 8a — schema DDL + connection | 8, and 7 | `bc38efa` |
| 12 | Step 8b — Postgres loader | 7 | `453e195` |
| 13 | Step 10 — orchestrator | 10 | `d8b343f` |
| 14 | Step 10+ — entry point, CLI, logging | **unconfirmed** | `6af07be` |
| 15 | Step 12 — full-project run | 12 | this README |
| 16 | Step 12+ — architecture guard | none | `6a88d8c` |
| 17 | Step 13 — analytics + reconciliation | 13 | `b5f5524` |

Anchors were verified against the PDF's printed step titles on 2026-08-17. **Step 10+ is
unconfirmed**: the PDF has no Step 11 and never makes `main.py` a numbered step, though its
Step 1 tree includes the file and its Step 12 assumes something runnable exists.

Two commit subjects appear twice — `feat: postgres via docker compose` and
`feat: environment-driven pipeline config` — because each step was staged in two passes. The
contents differ; the labels were reused.

## Documentation

| File | What it is |
|---|---|
| [docs/requirements_coverage.md](docs/requirements_coverage.md) | Grader-facing: every PDF requirement → file → ADR → what verifies it |
| [docs/SPEC.md](docs/SPEC.md) | The development contract: schema, validation rules, execution order, build plan |
| [docs/DECISION.md](docs/DECISION.md) | 26 ADRs, each with the alternatives that were rejected and why |
| [docs/bad_records_catalogue.md](docs/bad_records_catalogue.md) | The test oracle: every planted defect, its row number, and its expected reason code — written *before* the validator |

## Tests

```bash
pytest -q
```

283 tests. Those needing PostgreSQL **skip** rather than fail when it is unreachable, so a
fresh clone without `docker compose up` gives skips instead of red. Database tests run in
disposable schemas and never touch `sales_dev`.

The suite includes an architecture guard that reads every module's imports from the AST and
enforces the one-way dependency rule, because a back-edge is one added import line and
nothing about it fails at runtime.

## Deliberately not built

Each is a judgment, not an omission — full reasoning in [SPEC §2.2](docs/SPEC.md).

- **No SCD Type 2** — dimensions overwrite on conflict. The surrogate keys leave the door open.
- **No Airflow/Prefect** — one daily file; a cron-able `main.py` has less operational surface than the pipeline itself.
- **No cloud** — must run offline on a laptop during a recorded walkthrough.
- **No CDC or watermarks** — idempotent full-file reload per run instead (§7.3).
- **No ORM, no pydantic** — SQL and validation are the graded artifacts; both would hide them.
