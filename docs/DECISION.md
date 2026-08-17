# DECISION.md — Architecture Decision Log

**Project:** Object-Oriented Sales Data Pipeline
**Format:** lightweight ADR. Context → Options → Decision → Consequences → *Say on camera*.

The last field is the point of this file. Each decision gets one sentence I can say in the video without reading. If I cannot compress a decision into one sentence, I do not yet understand it well enough to defend it.

**Status values:** `Accepted` · `Superseded by D-xxx` · `Revisit`

---

## D-001 — Raw SQL over an ORM

**Status:** Accepted

**Context.** Data has to get into PostgreSQL. Python offers SQLAlchemy Core, the SQLAlchemy ORM, or a driver with hand-written SQL.

**Options.**
1. **SQLAlchemy ORM** — models double as schema, migrations via Alembic, portable across databases.
2. **SQLAlchemy Core** — SQL expression builder, no object mapping.
3. **`psycopg` v3 + hand-written SQL** — full control, one thin dependency.

**Decision.** Option 3.

**Consequences.** I write my own DDL and `INSERT`s and gain no database portability. In exchange, `sql/schema.sql` is a readable artifact I can open in the video, and the `ON CONFLICT` upsert is visible rather than buried in ORM session semantics. `psycopg` v3 over v2 because v2 is maintenance-only and v3 has a cleaner context-manager API — which I use directly in `DatabaseConnection` (D-008).

**Trade-off I accept.** Swapping to MySQL later would mean rewriting the SQL. For a graded single-database exercise that portability is worth nothing.

**Say on camera.** "An ORM would have written the SQL for me, but the assignment is grading my SQL — so I kept it visible."

---

## D-002 — `dataclass` over `pydantic`

**Status:** Accepted

**Context.** Records need typed representations. Pydantic would parse, coerce, and validate automatically.

**Decision.** Standard-library `@dataclass`.

**Consequences.** Pydantic would have eliminated most of `validators.py` — which is precisely the problem. Validation is the graded artifact and the most interesting code in the project. Auto-validating away the assignment's Step 5 would leave me with nothing to explain and no control over rejection reasons. Pydantic also raises on failure; I need to *collect* failures and keep processing (D-004).

Cost: I hand-write type coercion. That is maybe 40 lines, and those 40 lines are exactly what an examiner wants to see.

**Say on camera.** "Pydantic would have done the validation for me and thrown on the first bad row — I need to catch every bad row and explain why it failed, so I wrote it myself."

---

## D-003 — Four sales models instead of one

**Status:** Accepted

**Context.** A sales record could be one mutable class that gains fields as it moves through the pipeline. The assignment marks this step optional.

**Decision.** `RawSalesRecord` → `ValidSalesRecord` → `FactSalesRecord`, plus `RejectedRecord`.

**Consequences.** More classes and some field repetition. In return, every state transition is enforced by the type system: a function taking `ValidSalesRecord` cannot be handed unvalidated strings, and no reader has to guess whether `quantity` is `str` or `int` at any given point. The model file becomes a one-page summary of the whole pipeline, which is a strong opening for the video.

**Say on camera.** "Each class marks a state change — untrusted strings, proven types, computed measures — so the type signature tells you where in the pipeline you are."

---

## D-004 — Collect all rejections; never fail fast on bad data

**Status:** Accepted

**Context.** On encountering an invalid record: raise immediately, skip silently, or quarantine and continue.

**Decision.** Quarantine and continue. `validate()` returns `(valid, rejected)` and raises only on programmer error. A single record accumulates *all* its failure reasons, not just the first.

**Consequences.** Every run processes the whole file, so the business gets same-day partial revenue plus a complete defect report instead of one error message. Someone fixing the source file sees all problems in one pass rather than rediscovering them one run at a time.

Risk accepted: a systematically broken file produces a large rejection set rather than an obvious crash. Mitigated by `SCHEMA_MISMATCH`, which *does* fail the file fast at the header — a wrong-shape file is a different failure class from a wrong-value row.

**Say on camera.** "A batch pipeline that dies on row 3 of 500 hides the other 497 problems — so bad rows get quarantined with reasons, and only a malformed header stops the run."

---

## D-005 — Star schema with surrogate keys

**Status:** Accepted

**Context.** The assignment allows star, snowflake, or normalized, provided the choice is justified. Dimensions could key on natural business IDs (`customer_id TEXT`) or generated surrogates (`customer_key SERIAL`).

**Options.**
1. **Normalized (3NF)** — no redundancy, but analytics queries need many joins.
2. **Snowflake** — dimensions split further (product → category table). Saves trivial space here.
3. **Star, natural keys** — simplest; `fact_sales.customer_id` joins straight to `dim_customers`.
4. **Star, surrogate keys** — integer FKs, natural key kept as a unique constraint.

**Decision.** Option 4.

**Consequences.** The load gains one step: after upserting dimensions, build `{business_id: surrogate_key}` maps and resolve fact rows through them. That is roughly 15 lines.

Why pay it: surrogate keys decouple the warehouse from source-system identifiers. If the CRM renumbers customers, or two source systems both use `C001` for different people, natural keys break the fact table and surrogates absorb it. They are also the prerequisite for SCD Type 2 — you cannot version a dimension row whose key is the business ID. I am not building SCD2 (out of scope), but the schema does not preclude it.

Snowflaking rejected: normalizing `category` out of `dim_products` would add a join to every product query to save a few hundred bytes. Wrong trade at this scale.

**Trade-off I accept.** Debugging is slightly less pleasant — `fact_sales` shows `product_key = 7` rather than `SKU-1042`, so eyeballing raw fact rows requires a join.

**Say on camera.** "Surrogate keys cost me a lookup dictionary at load time and buy independence from source-system IDs — it's also the only way to version a dimension later."

---

## D-006 — `Decimal` for money, round once

**Status:** Accepted

**Context.** `float` is the default numeric type and is fast.

**Decision.** `decimal.Decimal` throughout; PostgreSQL `NUMERIC(12,2)`; a single `ROUND_HALF_UP` to 2dp at the end of the computation chain.

**Consequences.** Binary floating point cannot represent `0.1` exactly, so `0.1 + 0.2 != 0.3`. Across thousands of order lines those errors accumulate and the control-total check in SPEC §8.2 stops balancing — the reconciliation *depends* on exact arithmetic.

The round-once rule matters as much as the type choice: rounding `discount_amount` before subtracting it from `gross_sales` reintroduces exactly the error `Decimal` was chosen to avoid. Round at the boundary, not in the middle.

`Decimal` is slower than `float`. At this data volume that is unmeasurable.

**Say on camera.** "Money never touches a float, and I round once at the end — rounding intermediate values would put the error right back in."

---

## D-007 — Environment variables for all configuration

**Status:** Accepted

**Context.** Paths and DB credentials have to come from somewhere: hardcoded, a committed config file, CLI arguments, or the environment.

**Decision.** Environment variables via `python-dotenv`, loaded into `PipelineConfig.from_env()`. `.env` gitignored; `.env.example` committed with placeholders. `__repr__` masks the password.

**Consequences.** Directly enables the dev/prod separation in D-011 — same code, different `DB_NAME`. It is also the honest answer to the safety constraint: no credential is ever in the repository, and there is no code path where one could be.

`from_env()` validates at startup and fails with a readable message rather than a `KeyError` deep inside a run.

**Say on camera.** "Config comes from the environment, so the same code loads dev or prod by changing one variable — and no password ever enters git."

---

## D-008 — `DatabaseConnection` as a context manager; one transaction per run

**Status:** Accepted

**Context.** Connection lifecycle and commit boundaries. Options: commit after each table, commit per batch, or one transaction for the entire load.

**Decision.** Context manager wrapping the whole load: commit on clean exit, rollback on any exception, close in `finally`.

**Consequences.** A crash between `dim_products` and `fact_sales` leaves the database exactly as it was, not with orphaned dimensions and no facts. A half-loaded warehouse is worse than an empty one because it looks fine to a downstream query.

Cost: the entire run must fit in one transaction. At this volume that is fine; at ten million rows I would need chunked commits with a resume watermark, which is a real limitation worth naming rather than hiding.

**Say on camera.** "One transaction for the whole load — if anything fails, the database rolls back to clean rather than sitting half-populated."

---

## D-009 — Idempotent reload keyed on `source_file` and `order_id`

**Status:** Accepted

**Context.** Re-running the same file must not double revenue. Options: append blindly, `TRUNCATE` everything, `ON CONFLICT DO NOTHING` on the fact, or delete-then-insert scoped to this file.

**Decision.** Delete this file's rows from `stg_sales`, `fact_sales`, and `etl_rejected_sales`, then insert fresh. Dimensions are upserted, never deleted.

**Consequences.** Reruns are safe, and *corrections* work: if a source file is reissued with a fixed row, the old version is replaced rather than sitting alongside the new one — which `ON CONFLICT DO NOTHING` would have allowed. `TRUNCATE` was rejected because it would destroy prior months' data on every run.

Dimensions survive deletion because they are shared across files; deleting them would break FKs from other months' facts.

**Verification:** run twice, compare `SUM(net_sales)`. This is the demo in SPEC §11 at 7:45.

**Say on camera.** "I delete this file's rows before reloading, so running the pipeline twice gives the same total — and a corrected file actually corrects the data instead of duplicating it."

---

## D-010 — Database `CHECK` constraints duplicate the Python validator

**Status:** Accepted

**Context.** Validation already happens in `SalesDataValidator`. Adding `CHECK (quantity > 0)` in DDL repeats it.

**Decision.** Keep both, deliberately.

**Consequences.** This is defence in depth, not redundancy through oversight. The Python validator produces *good error messages for humans*; the database constraint guarantees the invariant *no matter what code writes to the table* — including a future script, a manual `INSERT`, or a bug I introduce next week.

The useful property: if a `CHECK` ever fires, that is not a data problem, it is a signal that the validator has a hole. The constraint is a tripwire on my own code.

**Say on camera.** "The database constraints repeat the validator on purpose — if one ever fires, it means my validator has a bug."

---

## D-011 — Dry-run mode and a dev database before prod

**Status:** Accepted

**Context.** The assignment explicitly asks how you inspect output *before* loading, and how you know the result is correct.

**Decision.** Two mechanisms. `--dry-run` runs extract → validate → transform, writes the rejected CSV and a `preview_fact_sales.csv`, prints the summary, and never opens a database connection. Separately, dev and prod databases are selected by one environment variable (D-007).

**Consequences.** The full transform is inspectable as a flat file before anything is written. Dev/prod separation costs nothing extra because config was already externalized.

Honest limitation: dry-run cannot catch failures that only occur at the database boundary — FK violations, `NUMERIC` overflow, constraint rejections. That is what the dev database is for. Dry-run proves the *logic*; the dev load proves the *load*. Saying that distinction out loud is stronger than claiming dry-run covers everything.

**Say on camera.** "Dry-run proves the transform logic without touching the database; the dev database proves the load. They catch different failures, so I use both."

---

## D-012 — The bad-record catalogue is written before the validator

**Status:** Accepted

**Context.** The demo data needs intentional defects. The obvious order is to write the validator, run it, and record what it caught.

**Decision.** Invert it. `docs/bad_records_catalogue.md` — every planted defect with its row number and expected `reason_code` — is written in Step 4, before `validators.py` exists in Step 6.

**Consequences.** This makes the catalogue an independent oracle rather than a transcript of whatever the validator happened to do. If I write the validator first, "the validator caught 30 bad rows" only tells me the validator agrees with itself — it says nothing about the rows it missed. Writing the expectation first makes silent gaps visible: expected 32, caught 30, so two rules are broken or absent.

This matters more than usual here because a coding agent is writing the validator. The catalogue is how I check its work without reading every branch.

**Say on camera.** "I wrote down every defect I planted *before* writing the validator, so 'it caught 30' can be compared against 'there are 32' instead of the validator grading itself."

---

## D-013 — Rejections carry a stable `reason_code`, not just a message

**Status:** Accepted

**Context.** A rejection could be a free-text string: `"quantity must be positive, got -3"`.

**Decision.** Two fields — a stable enum-like `reason_code` plus a human `reason_detail`.

**Consequences.** `GROUP BY reason_code` becomes possible, which turns the rejected table into an actual data-quality report: "83% of this week's rejects are `UNKNOWN_PRODUCT`" points at a broken product-master feed, not at 400 individual typos. Free text cannot be aggregated without string parsing.

Cost: codes must stay stable once written, since renaming one breaks historical comparisons.

**Say on camera.** "Every rejection has a code as well as a message, so I can group them in SQL and see *which kind* of problem is growing."

---

## D-014 — No orchestrator, no cloud, no ORM, no SCD2

**Status:** Accepted

**Context.** Airflow, cloud warehousing, and slowly-changing dimensions would all look impressive.

**Decision.** All excluded. Enumerated as explicit non-goals in SPEC §2.2 rather than left unmentioned.

**Consequences.** Airflow for one daily file is more moving parts than the pipeline itself; the operational cost would exceed the pipeline's. Cloud adds credentials, billing, and network failure modes to a project that must run offline on a laptop during a video. SCD2 is genuinely valuable and genuinely out of scope — the surrogate keys in D-005 leave the door open, which is the right amount of investment.

The real risk being managed is scope creep: I have over-engineered a portfolio project before and the cost was not the wasted work, it was the loss of a clear story. A pipeline I can fully explain in ten minutes beats a more impressive one I can only partly explain.

**Say on camera.** "Airflow, cloud, and SCD2 are all deliberately out — for one daily file they'd add more operational surface than the pipeline has, and the schema already leaves room for SCD2 later."

---

## D-015 — AI-assisted development with per-step review gates

**Status:** Accepted

**Context.** The assignment is explicitly AI-assisted. The failure mode is generating a working repository nobody can explain — which is fatal here, since the grade is a video of me explaining it.

**Decision.** Spec-first, one file per prompt, one commit per step, human review before every commit. The agent writes; I decide. Any choice the agent makes that the spec did not dictate gets appended to this file **in my own words**.

**Consequences.** Slower than asking for the whole project at once. The gate that matters: *if I cannot narrate a block of code in 30 seconds, it does not get committed* — it gets rewritten or deleted, regardless of whether it works.

Writing the rationale myself rather than pasting the agent's explanation is the actual test. Reading a justification produces a feeling of understanding; reproducing it produces the real thing, and the video will expose which one I have.

**Say on camera.** "I used AI per file against a spec I wrote first, and my rule was that nothing gets committed if I can't explain it in thirty seconds — which is why I can walk you through any file here."

---

## D-016 — Extractors return raw data; the transformer builds `Customer` and `Product`

**Status:** Accepted — **reverses my v1 spec**

**Context.** SPEC v1 §4 defined `JSONExtractor` as "generic, parameterized by the model it builds" — i.e. the extractor emitted typed `Customer` / `Product` objects. The assignment PDF puts that construction in the transformer instead. The coding agent flagged the contradiction on its first read rather than resolving it silently.

**Options.**
1. **Extractor builds domain objects** (my v1). Fewer moving parts; JSON already carries types.
2. **Extractor returns `list[dict]`; transformer builds objects** (the PDF).

**Decision.** Option 2. My v1 was wrong, and not only because the PDF is the rubric.

**Consequences.** The v1 design broke the uniformity of the extractor layer: `CSVExtractor` returned untrusted strings while `JSONExtractor` returned trusted domain objects. That inconsistency undermines the rule the whole model design rests on — *everything that comes off disk is untrusted until something validates or transforms it*. JSON is not exempt: it can have missing fields, wrong types, or a customer with no region.

It also explains a hole I hadn't noticed. SPEC v1's Step 7 listed "reference-data transforms" with nothing concrete behind it. That emptiness was a symptom: the work had been moved upstream into the extractor, leaving the transformer with only half a job.

**Knock-on effect — the transformer now runs twice.** The validator's foreign-key check needs the valid ID sets, and those now come from the transformer's output. So the runtime order is extract → transform(reference) → validate → transform(sales). Modules do not import each other to achieve this; `pipeline.py` sequences the calls and every module depends only on `models.py`. The import graph is unchanged; only the call graph is.

**Say on camera.** "The transformer runs twice — reference data first, because the validator's foreign-key check needs the ID sets it produces. That's call order, not import order; the dependency graph is still one-way."

---

## D-017 — `discount_rate` range is `[0, 1)`

**Status:** Accepted

**Context.** The PDF says `discount_rate` is "between zero and one" without stating inclusivity. A literal reading permits `[0, 1]`.

**Decision.** `[0, 1)` — zero is valid, exactly `1.0` is rejected as `DISCOUNT_EQ_ONE`. Encoded in both the validator and the `CHECK` constraint.

**Consequences.** A 100%-off line produces `net_sales = 0`. That is either a data-entry error or a giveaway, and neither should post as a revenue row without someone looking at it. Rejecting it surfaces the row; accepting it hides a zero inside legitimate revenue where no aggregate will ever flag it.

The ambiguity is the point worth voicing. Noticing that "between zero and one" is underspecified, choosing a reading, and knowing it is a one-constant change is a better answer than either choice on its own.

**Say on camera.** "The doc says between zero and one but doesn't say whether one is included — I excluded it, because a hundred-percent-off line posts zero revenue and should be looked at. If the grader wants it inclusive it's one constant."

---

## Open questions (resolve before Step 4+)

| # | Question | Leaning |
|---|---|---|
| Q1 | ~~Is `discount_rate == 1.0` valid or rejected?~~ | **Resolved — see D-017.** Rejected; range is `[0, 1)`. |
| Q2 | Whitespace and casing in IDs — clean or reject? | Clean (strip, uppercase) and log it. Cleaning a cosmetic defect is safe; cleaning a *semantic* one (guessing an unknown customer) is not. That boundary is the interesting judgment. |
| Q3 | `unit_price` with 3+ decimals — reject or round? | Reject. Silently rounding money changes the number without telling anyone. |
| Q4 | Should `etl_run_log` be in scope? | Yes — it is ~10 lines and it makes the reconciliation query in SPEC §8.2 possible, which is the direct answer to the assignment's "how do you know it's correct." |
| Q5 | Does `stg_sales` hold raw strings or typed valid rows? | Typed valid rows, per the assignment's "staging table for valid sales records." Note in the video that a stricter definition of staging would keep raw text and validate downstream — knowing the alternative is worth mentioning. |

---

## Log

| Date | Change |
|---|---|
| (fill in) | Initial log, D-001..D-015, written during planning before any code |
