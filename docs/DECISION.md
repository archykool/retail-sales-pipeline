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

**The argument that actually settles it is §3.1, not warehousing convention.**

`FactSalesRecord` carries natural keys — `customer_id`, `product_id` — and it has to.
A surrogate key does not exist until its dimension row has been inserted, so producing
one means querying `dim_customers`. If `SalesDataTransformer` did that, `transformers.py`
would need a database connection, which means importing `loaders.py`, which is the
back-edge the one-way dependency rule forbids. There is no way to have the transformer
emit surrogate keys and keep §3.1 intact.

So surrogate resolution can only live in the loader, where the connection already is.
That places the `{business_id: surrogate_key}` maps in `PostgresLoader` (§7.2) not as a
matter of taste but as the only position consistent with the architecture — and it makes
the boundary testable: `test_transformers.py` asserts the module's source contains no
`psycopg` import and no `loaders` import, and `test_fact_carries_natural_keys_not_surrogate_keys`
asserts the fact record has no `customer_key` attribute.

That is the version worth saying out loud. "Surrogate keys are a warehousing convention"
is a claim about fashion; "the dependency rule leaves surrogate resolution nowhere else
to go" is a conclusion, and it connects the schema decision to the structural claim the
whole walkthrough rests on.

**Trade-off I accept.** Debugging is slightly less pleasant — `fact_sales` shows `product_key = 7` rather than `SKU-1042`, so eyeballing raw fact rows requires a join. That join is also what `preview_fact_sales.csv` needs in order to be compared to the table by eye (Step 7b), so the cost shows up twice.

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

### The spec has been corrected by implementation five times

Not a list of mistakes — a list of things a spec-first process caught that neither the
spec nor the code would have caught alone. Each was found because writing the code forced
a question the document had answered vaguely, or not at all:

1. **Step 3+ ordering.** §9 said "generator… then catalogue", which contradicts D-012's
   entire argument. Written that way round, the catalogue becomes a transcript of the
   generator's output and can no longer disagree with it. Reversed.
2. **`stg_sales` vs `ValidSalesRecord`.** §5 declared `source_file` and `row_num` columns
   that no model carried past validation, so those columns had no source. Fixed by
   D-020 — provenance now survives validation.
3. **The unowned preview file.** §8.1 and D-011 both require
   `data/rejected/preview_fact_sales.csv`, and no step in §9 produced it. It would have
   surfaced at Step 10 as "dry-run doesn't work", and the tempting fix — writing the CSV
   inside `pipeline.py` — is a rejection trigger. Assigned to Step 7b instead.
4. **The PDF step anchors.** Six were inferred and never verified; two were wrong. Docker
   Compose is PDF Step 9, not Step 1, and the PDF has **no configuration step at all**.
   Two commits had already landed under the wrong labels. §9 renumbered against the
   document, §9.0 added as a two-way map.
5. **The build-order column contradicting itself.** Inserting Step 7b left §9.0 claiming
   two different steps were both built twelfth — a table that had started describing an
   intention rather than the sequence.

There is a sixth of a different kind, worth separating: the transformer produced three
money columns that do not sum to each other (D-024). The spec was not wrong there — §7.1
says exactly what to do — but nothing had noticed that following it makes
`gross ≠ discount + net` on three rows, which would have failed a `CHECK` constraint at
Step 8 and been diagnosed as a bug in the arithmetic rather than a property of rounding.

**This is the argument that the spec is load-bearing rather than decorative.** A document
nobody implements against is never wrong, because nothing tests it. Five corrections in
eleven steps is the evidence that this one was being read closely enough to break — and
each correction is written down with its reasoning, which is why the ADR log has a D-020
and a D-024 at all.

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

## D-018 — from_env() reads only from environment variables, not files
**Context**: Initial acceptance/verification failed because the required environment variables were not set in the shell.

**Decision**: from_env() does not touch load_dotenv(); loading .env is deferred strictly to application entry points.

**Consequences**: The .env file is relegated to a local development convenience. In production, variables are injected directly by the container runtime, keeping the code execution path identical—which is the exact prerequisite for the dev/prod switching in §8.1. The trade-off is that unit tests targeting from_env() must explicitly monkeypatch the environment, adding a couple of extra lines compared to reading directly from a file.

**Say on camera.** "Config reads from the environment, not from files—the .env file is just a local convenience, requiring zero changes in production."

---

## D-019 — Database credentials are all required, no defaults

**Status:** Accepted — a deviation from SPEC §9 Step 2, accepted after review

**Context.** SPEC §9 Step 2 asked for configuration read from the environment "with sane
defaults for all DB settings". The implementation made all five — `DB_HOST`, `DB_PORT`,
`DB_NAME`, `DB_USER`, `DB_PASSWORD` — required, raising at startup when any is absent.

**Options.**
1. **Defaults for everything** (`localhost`, `5432`, `postgres`, …), as the spec asked.
2. **All five required**, failing loudly at startup.
3. Defaults for the harmless ones (host, port) and required for the rest.

**Decision.** Option 2. The spec was wrong and is amended by this ADR rather than the
other way round.

**Consequences.** With defaults, a typo in `DB_NAME` does not fail — it connects
somewhere else, and because `create_tables()` is idempotent DDL it *creates the schema
there* and loads into it. The run reports success. The tables exist. They are in the
wrong database, and nothing in the output says so.

The deeper problem is that defaults make three different situations indistinguishable: a
variable that was never set, a `.env` that failed to load, and a correctly configured
environment all produce a working connection to whatever the default points at. Failing
at startup collapses that ambiguity — if the pipeline runs at all, the configuration was
explicit.

This is also what makes the dev/prod switch in §8.1 trustworthy. Repointing `DB_NAME`
from `sales_dev` to `sales_prod` is the entire deployment procedure, so a silently
defaulted `DB_NAME` is a silently defaulted *environment*.

Cost, and it is a real one: a fresh clone does nothing until `.env` is filled in from
`.env.example`. There is no zero-configuration path for someone who just wants to see it
run. That trade is accepted — the README covers the setup, and a five-line `.env` is
cheaper than a load into the wrong database.

Related: `from_env()` reads `os.environ` only and never calls `load_dotenv()` (D-018).
The two decisions are the same instinct — configuration is explicit or it is a bug.

**Say on camera.**

---

## D-020 — Provenance survives validation

**Status:** Accepted

**Context.** `RawSalesRecord` and `RejectedRecord` both carry `row_num` and
`source_file`; the first draft of `ValidSalesRecord` and `FactSalesRecord` carried
neither. So a record knew where it came from right up until it passed validation, and
then forgot.

That is backwards. §5's `stg_sales` declares both columns, so they had no source to be
populated from — and the rows that keep full provenance were the ones already quarantined
with their raw payload attached, while the rows that go on to become revenue lost it.

**Options.**
1. **Provenance on rejected records only.** Enough to fix a bad file, which is the
   obvious use case.
2. **Carry `row_num` and `source_file` through every sales state.**
3. **Reconstruct it when needed** by joining `fact_sales` back to the source file on
   `order_id`.

**Decision.** Option 2. Two extra fields on two dataclasses, positioned first to match
the convention already set by `RawSalesRecord`.

**Consequences.** Option 3 is the one that looks cheapest and fails exactly when it is
needed. Reconstruction requires a usable `order_id`, and the rows hardest to locate are
the ones whose `order_id` was the defect — `BAD_INT_ORDER_ID` has nothing to join on. A
recovery mechanism that works only for records that were fine anyway is not a recovery
mechanism.

The concrete payoff is at §8.2. When the reconciliation does not balance, the question is
never "did it balance" but "which row", and `row_num` plus `source_file` answers it
directly: open that file, go to that line. Without them the failure is real but
unlocalisable, which is the same weakness D-022 identifies in the NaN case.

`source_file` is deliberately `path.name` and not the full path. A full path would differ
between machines, so the same file would look like a different source on another checkout
and §7.3's `DELETE FROM stg_sales WHERE source_file = %s` would stop matching prior runs.
Keeping it to the bare filename means **one key serves three purposes**: the idempotency
key, the provenance record, and the correlation key between the rejected CSV and
`etl_rejected_sales` — which is why `run_id` does not need to appear in that CSV. `run_id`
belongs to a run, not to a record; putting it on `RejectedRecord` would give the data
model an orchestration concept.

**Say on camera.**

---

## D-021 — One rejected row, one record: primary `reason_code` by precedence tier

**Status:** Accepted

**Context.** D-004 collects every failure reason for a row rather than stopping at
the first, and `RejectedRecord` carries exactly one `reason_code` plus a
`reason_detail` listing all of them — one row in, one rejection record out, which
is what keeps §8.2's row conservation (`extracted == valid + rejected`) arithmetic
rather than approximate.

That forces a question the spec did not answer: when a row breaks three rules at
once, which code becomes *the* code? Four rows in `bad_records_catalogue.md` (72,
108, 113, 117) break more than one rule, and Step 5's exit criterion is that the
validator agrees with the catalogue on *which code fired on which row*. Without a
written rule the two cannot be compared at all.

**Options.**
1. **No primary code** — carry a list of codes, drop the single-code column.
   Honest, but `GROUP BY reason_code` (D-013) stops working, and the rejected
   table stops being a data-quality report.
2. **First rule that fires, in source order** — trivial to implement. The primary
   code then depends on the order methods happen to be written in, so reordering
   the validator silently changes historical reporting.
3. **Column order only** — the leftmost bad column wins. Stable, but ranks a
   malformed date above an unparseable price for no reason other than CSV layout.
4. **Precedence tiers by knowability**, ties broken on column order.

**Decision.** Option 4. Five tiers: missing field → parse failure → range/value →
foreign key → file-scoped duplicate. Plus suppression rules, so a more specific
code replaces a general one describing the same failure rather than both being
recorded (`NON_NUMERIC_CURRENCY` over `BAD_DECIMAL_PRICE`; `DISCOUNT_EQ_ONE` over
`DISCOUNT_OUT_OF_RANGE`). Full table in `bad_records_catalogue.md` §3.

**Consequences.** The tiers are not a convention chosen for tidiness — they follow
what is actually knowable at each stage. You cannot range-check a number that
failed to parse, and you cannot foreign-key-check a field that was empty. So the
primary code is always the *earliest* thing that went wrong, which is also the
thing a person fixing the file should address first: correcting the parse error on
row 108 may well make its quantity check moot.

The cost is that precedence is a rule I have to keep in sync across two artifacts —
the catalogue asserts it and the validator implements it. That duplication is
deliberate (it is the same defence-in-depth argument as D-010), but a change to
the tiers means editing both.

Suppression carries a subtler cost: `reason_detail` no longer lists literally every
predicate that would have failed, only the surviving diagnoses. A row priced
`"$45.00"` reports one defect, not two. That is the correct count of *problems*
even though it is not the count of *failed checks*.

**Say on camera.**

---

## D-022 — Reject non-finite decimals at the parse boundary

**Status:** Accepted

**Context.** `Decimal("nan")`, `Decimal("Infinity")`, and `Decimal("-Infinity")` all
parse successfully — they are valid `Decimal` values, not parse errors. The obvious
validator therefore accepts them, and then IEEE 754 governs what happens next: every
comparison against NaN returns `False`, including `NaN == NaN`.

**Options.**
1. **No check.** What a straightforward implementation does, because nothing in §6
   mentions NaN and `Decimal()` raises no error.
2. **Reject at the parse boundary**, under the existing `BAD_DECIMAL_PRICE` /
   `BAD_DECIMAL_DISCOUNT` codes.
3. **Guard downstream** — in the transformer's arithmetic, or with a database `CHECK`,
   per D-010's defence-in-depth argument.

**Decision.** Option 2. `_to_decimal()` treats a parsed-but-non-finite value exactly
as it treats an unparseable one. No new reason code: the row genuinely is "not a
usable decimal", and `reason_detail` distinguishes the two causes in words.

**Consequences.** Option 1's failure chain is worth following all the way, because
every link is silent:

1. `nan <= 0` is `False`, so `PRICE_NOT_POSITIVE` does not fire.
2. `nan > max_quantity` is `False`, so no threshold rule fires.
3. `nan.as_tuple().exponent` is not a negative integer, so `PRICE_PRECISION` does not fire.
4. The row passes validation and is counted as valid. Row conservation still balances.
5. It reaches `fact_sales`, and `SUM(net_sales)` for the whole table becomes NaN.
6. §8.2's control total compares the staging sum to the fact sum — `NaN = NaN` is
   `False`, so the reconciliation reports a mismatch.
7. The mismatch is unlocalisable. Every per-row check also compares against NaN and
   also returns `False`, so nothing identifies which row poisoned the total.

That is the specific reason this belongs at the parse boundary rather than downstream:
it is the last point at which the offending row is still identifiable. Once the value
is inside an aggregate, the evidence is gone.

Option 3 also does not work as a safety net here, and that is worth checking at Step 8:
PostgreSQL's `numeric` type accepts `NaN` and orders it *above* all other values, so
`CHECK (unit_price > 0)` would pass it. D-010's claim that the `CHECK` constraints
duplicate the Python validator has a hole exactly at this case — the constraint is not
a second line of defence against non-finite input, only against sign and range.

Cost: one extra branch per decimal parse, and two reason codes that now cover two
distinct causes each.

**Say on camera.**

---

## D-023 — `reason_detail` stays a joined string; the code list is accepted debt

**Status:** Accepted debt, not accepted design

**Context.** A rejected row carries one primary `reason_code` plus `reason_detail`,
which joins every surviving defect into a single string (D-021). A consumer that wants
to filter on a *non-primary* code has to parse prose to do it.

This surfaced as a real bug at Step 5. The separator was `"; "` and one detail message
— "carries currency formatting; expected a bare decimal" — contained a semicolon, so
anything splitting `reason_detail` saw a defect that did not exist. It failed silently
in both directions: nothing raised, and the phantom code looked plausible.

**Options.**
1. **Remove the semicolon from that message.** Fixes the instance, leaves the class
   open for the next person who writes a detail containing the separator, and fails
   silently again when they do.
2. **Change the separator to `" | "`** — a sequence prose will not produce.
3. **Add `reason_codes: tuple[str, ...]` to `RejectedRecord`.** Nothing parses
   anything; the codes are structured data.

**Decision.** Option 2 now. Option 3 is the correct fix and is **deliberately
deferred.**

Option 3 is right and we are not doing it because of where it reaches: `models.py`
(Step 3, committed), the rejected-record CSV writer (Step 7a), and the
`etl_rejected_sales` DDL (Step 8). Three steps, two of them not yet built, to fix a
defect whose entire current impact is readability. `reason_code` is a separate field
and is unaffected, so `GROUP BY reason_code` — the actual analytics requirement, R23 —
works correctly today.

**Trigger for paying the debt:** any consumer that needs to filter or aggregate on a
non-primary code. The moment a query wants "every row where `UNKNOWN_PRODUCT` fired,
primary or not", the string parse becomes load-bearing and Option 3 stops being
optional. Until then this is a documented shortcut, not an oversight.

**Consequences.** Option 2 makes the string reliably splittable, which means the debt
is survivable rather than actively misleading — the difference between a shortcut and a
trap. It does not make the string *structured*, so anyone splitting it is still relying
on a formatting convention rather than a data contract, and that convention now lives
in one named constant (`DETAIL_SEPARATOR`) instead of being inlined at the join site.

The honest cost of deferring: a future consumer will hit this, and the fix will be
more expensive then than now, because Steps 7a and 8 will have shipped and the DDL
will need a migration rather than an edit.

**Say on camera.**

---

## D-024 — Per-column correctness over additivity; the three cents are by design

**Status:** Accepted

**Context.** §7.1 computes three measures from each valid row and rounds each once, at
the end, to two decimal places:

```
gross_sales     = quantity * unit_price
discount_amount = gross_sales * discount_rate
net_sales       = gross_sales - discount_amount
```

Three rows in the committed dataset — 34, 76 and 118, all `qty=5, price=34.95,
rate=0.10` — produce two exact half-cents at once:

```
gross        = 174.75            exact
discount_raw =  17.475  -> 17.48   half-cent, rounds up
net_raw      = 157.275  -> 157.28  half-cent, rounds up
               17.48 + 157.28 = 174.76, but gross is 174.75
```

Each row is off by one cent, so across the dataset
`SUM(gross) - SUM(discount) - SUM(net) = -0.03`.

**This is not what `Decimal` was for, and `Decimal` does not fix it.** D-006 chose
`Decimal` over `float` to eliminate *binary representation* error — the reason
`0.1 + 0.2 != 0.3` in float arithmetic. That problem is solved completely and
permanently: `Decimal("0.1")` is exactly one tenth.

Rounding is a different problem with no clean fix. Quantizing to two places maps
infinitely many exact values onto a grid of cents, and that map cannot preserve both
per-value accuracy and additivity. Given `a = b + c` exactly, `round(b) + round(c)`
need not equal `round(a)`. No numeric type changes this; it is arithmetic, not
representation. Conflating the two is easy and leads to hunting for a bug that is not
there.

**Options.**
1. **Round each measure independently** — each column is the correctly rounded value
   of its own exact quantity; the additive identity can be off by a cent per row.
2. **Derive `net = round(gross) - round(discount)`** — identity always holds. Row 34's
   `net_sales` becomes 157.27 when the exact value is 157.275. Also contradicts §7.1
   explicitly.
3. **Make `net_sales` a generated column in SQL** — identity by construction, changes
   §5 and moves a business rule into the DDL.

**Decision.** Option 1. The three columns are aggregated independently — `SUM(net_sales)`
is the revenue figure, `SUM(discount_amount)` answers "what did discounting cost us" —
so each must be the correctly rounded value of its own exact quantity. Option 2
corrupts the number everyone queries to repair an identity nobody queries.

**Consequences.**

- `SUM(gross) - SUM(discount) - SUM(net) = -0.03` for this dataset, contributed by rows
  34, 76 and 118 at one cent each. Recorded as a number in
  `docs/bad_records_catalogue.md` §2, not merely as a principle — a known discrepancy
  is a design property, an unrecorded one is an open bug, and the only difference
  between them is whether it is written down.
- **No `CHECK (gross_sales = discount_amount + net_sales)` in `schema.sql`.** It would
  reject three valid rows. Its absence is marked with a comment pointing here, because
  otherwise someone adds it later as an obvious improvement and three good rows start
  failing for a reason nobody remembers.
- The Step 13 `-- RECONCILIATION` block asserts this identity equals **-0.03**, not
  zero. Demonstrating a predicted non-zero is stronger evidence than demonstrating a
  zero: a zero can come from two errors cancelling, or from a check that silently is
  not running. A specific predicted non-zero can only come from the arithmetic being
  exactly as documented.
- If the dataset is ever regenerated with different prices or rates, the constant
  changes. It is a property of this data, not of the code, and the reconciliation
  comment says so.

**Say on camera.**

---

## Open questions (Q2 and Q3 resolve before Step 5; Q4 and Q5 before Step 8)

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
