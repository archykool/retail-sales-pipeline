"""
Sequencing the run, and nothing else.

This file is the only one that knows the whole story (§3.1). It imports every layer and
calls them in §7.0's order; it computes nothing. If a number appears here that was not
returned by something else, business logic has drifted into the orchestrator — which is
one of the rejection triggers in §13, and the easiest one to commit by accident, because
"just add up the rows here" always looks like the shortest path.

The ordering it enforces is the least obvious thing in the design: **the transformer runs
twice.** Reference data has to be transformed *before* validation, because the validator's
foreign-key check consumes the ID sets the transformer produces. Sales data is transformed
*after*, because there is no point computing money for a record about to be rejected. Two
calls to the same layer, on either side of a third — and no import between them, because
`pipeline.py` sequences the calls rather than the modules calling each other.

Dry-run **opens no database connection at all** (§8.1). Not a connection with the writes
skipped — no connection. That distinction is the whole value of the mode: it proves the
logic runs on a machine with no database reachable, which is a different claim from proving
the load works, and the two catch different failures.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from uuid import UUID, uuid4

from .config import PipelineConfig
from .extractors import CSVExtractor, JSONExtractor
from .loaders import (
    DatabaseConnection,
    FactPreviewWriter,
    PostgresLoader,
    RejectedRecordWriter,
)
from .models import (
    Customer,
    FactSalesRecord,
    PipelineResult,
    Product,
    RawSalesRecord,
    RejectedRecord,
    ValidSalesRecord,
)
from .transformers import ReferenceDataTransformer, SalesDataTransformer
from .validators import SalesDataValidator, period_from_filename

logger = logging.getLogger(__name__)


class SalesPipeline:
    """Runs one file through every stage, in the one order that works.

    Takes a `PipelineConfig` rather than individual paths because the whole point of
    D-007 is that a run is described by its environment. `--file` at Step 10+ works by
    handing this a config with a different `sales_file`, not by adding a parameter here —
    `PipelineConfig` is frozen, so `dataclasses.replace` produces the variant.
    """

    def __init__(
        self,
        config: PipelineConfig,
        *,
        dry_run: bool = False,
        today: date | None = None,
    ) -> None:
        self.config = config
        self.dry_run = dry_run
        # Injected for the same reason the validator takes it (D-021 boundary cases):
        # DATE_IN_FUTURE compares against the clock, so a test that cannot pin "today"
        # starts failing next year for reasons unrelated to the code.
        self.today = today or date.today()

    def run(self) -> PipelineResult:
        """Execute the run and return its summary.

        **Raises rather than returning a FAILED result.** A failure has already rolled the
        transaction back (§7.4), and swallowing the exception to populate a status field
        would hide the traceback from the caller that needs it. `main.py` logs it and exits
        1, which is what makes the pipeline cron-able. So `status` here only ever holds
        SUCCESS — the same conclusion D-025 reached about the database column, for the same
        underlying reason: under one transaction, failure is not a state anything survives
        to record.
        """
        run_id = uuid4()
        started_at = datetime.now()
        source_file = self.config.sales_file.name

        logger.info(
            "run %s starting: file=%s dry_run=%s", run_id, source_file, self.dry_run
        )

        # §7.0 steps 1-2: everything off disk, still untrusted.
        raw_sales = self._stage(
            "extract sales", lambda: CSVExtractor(self.config.sales_file).extract()
        )
        raw_customers = self._stage(
            "extract customers", lambda: JSONExtractor(self.config.customers_file).extract()
        )
        raw_products = self._stage(
            "extract products", lambda: JSONExtractor(self.config.products_file).extract()
        )

        # §7.0 steps 3-4: the transformer's first pass. Must precede validation.
        reference = ReferenceDataTransformer()
        customers = self._stage(
            "transform customers", lambda: reference.to_customers(raw_customers)
        )
        products = self._stage(
            "transform products", lambda: reference.to_products(raw_products)
        )

        # §7.0 step 5.
        valid, rejected = self._validate(raw_sales, reference, customers, products)

        # §7.0 step 6: the transformer's second pass.
        facts = self._stage("transform sales", lambda: SalesDataTransformer().to_facts(valid))

        # §7.0 step 7. Local files first, so a failed load still leaves its diagnosis.
        self._write_local_artifacts(rejected, facts)

        rows_loaded = 0
        if self.dry_run:
            logger.info("dry run: skipping the database entirely, no connection opened")
            result = self._summarise(
                run_id, raw_sales, valid, rejected, rows_loaded, started_at
            )
        else:
            result, rows_loaded = self._load(
                run_id, source_file, started_at, raw_sales, valid, rejected, facts,
                customers, products,
            )

        logger.info(
            "run %s finished: extracted=%d valid=%d rejected=%d loaded=%d in %.2fs",
            run_id,
            result.rows_extracted,
            result.rows_valid,
            result.rows_rejected,
            result.rows_loaded,
            result.duration_seconds,
        )
        return result

    # ------------------------------------------------------------------
    # Stages
    # ------------------------------------------------------------------

    def _validate(
        self,
        raw_sales: list[RawSalesRecord],
        reference: ReferenceDataTransformer,
        customers: list[Customer],
        products: list[Product],
    ) -> tuple[list[ValidSalesRecord], list[RejectedRecord]]:
        """Build the validator from the transformer's output, then run it.

        The ID sets come from step 3, which is why the transformer had to run first. The
        period comes from the filename, which is why `DATE_OUT_OF_PERIOD` is possible at
        all — and `None` when the filename does not state one, so the rule is skipped
        rather than guessed at.
        """
        validator = SalesDataValidator(
            reference.customer_ids(customers),
            reference.product_ids(products),
            max_quantity=self.config.max_quantity,
            today=self.today,
            period=period_from_filename(self.config.sales_file.name),
        )
        valid, rejected = self._stage("validate", lambda: validator.validate(raw_sales))
        logger.info(
            "validation split: %d valid, %d rejected of %d extracted",
            len(valid),
            len(rejected),
            len(raw_sales),
        )
        return valid, rejected

    def _write_local_artifacts(
        self, rejected: list[RejectedRecord], facts: list[FactSalesRecord]
    ) -> None:
        """Write the rejected CSV always; the fact preview only on a dry run.

        The rejected file is unconditional because it is the run's diagnosis, and a run
        that fails at the database should not also lose its explanation of what was wrong
        with the data.

        The preview is dry-run only. Calling a file a *preview* of a load that already
        happened is a misnomer — after a real run, `fact_sales` is the better artifact and
        the `-- PREVIEW COMPARISON` join reads it directly. This also makes the demo
        sequence natural: dry-run writes the preview, the real run loads, and the two are
        compared side by side.
        """
        self._stage(
            "write rejected csv",
            lambda: RejectedRecordWriter(self.config.rejected_dir).write(rejected),
        )
        if self.dry_run:
            self._stage(
                "write fact preview",
                lambda: FactPreviewWriter(self.config.rejected_dir).write(facts),
            )

    def _load(
        self,
        run_id: UUID,
        source_file: str,
        started_at: datetime,
        raw_sales: list[RawSalesRecord],
        valid: list[ValidSalesRecord],
        rejected: list[RejectedRecord],
        facts: list[FactSalesRecord],
        customers: list[Customer],
        products: list[Product],
    ) -> tuple[PipelineResult, int]:
        """Everything that touches the database, inside exactly one transaction (§7.4).

        The order is not arrangeable: clear the previous load first (§7.3), then dimensions,
        because the surrogate keys the facts need do not exist until the dimension rows do
        (§7.2), then staging, facts and rejections.

        The run log is written last and *inside* the block, so it commits with the data it
        describes. That is also why a failed run leaves no log row (D-025).
        """
        with DatabaseConnection.from_config(self.config) as connection:
            loader = PostgresLoader(connection, batch_size=self.config.load_batch_size)

            self._stage("create tables", loader.create_tables)
            self._stage(
                "clear previous load",
                lambda: loader.delete_previous_load(
                    source_file, [fact.order_id for fact in facts]
                ),
            )

            customer_keys = self._stage(
                "upsert customers", lambda: loader.upsert_dim_customers(customers)
            )
            product_keys = self._stage(
                "upsert products", lambda: loader.upsert_dim_products(products)
            )

            self._stage("load staging", lambda: loader.load_staging(valid, run_id))
            rows_loaded = self._stage(
                "load facts",
                lambda: loader.load_facts(facts, run_id, customer_keys, product_keys),
            )
            self._stage("load rejected", lambda: loader.load_rejected(rejected, run_id))

            result = self._summarise(
                run_id, raw_sales, valid, rejected, rows_loaded, started_at
            )
            loader.write_run_log(
                result,
                source_file=source_file,
                started_at=started_at,
                finished_at=datetime.now(),
            )

        return result, rows_loaded

    # ------------------------------------------------------------------
    # Helpers — these must stay free of arithmetic on the data
    # ------------------------------------------------------------------

    def _stage(self, name: str, action):
        """Run one stage, logging its elapsed time and how much it produced.

        Timing every stage rather than only the run makes the log the first place to look
        when a run gets slow, without adding a profiler. `len()` where the result supports
        it, so the log says what happened rather than only that something did.
        """
        started = datetime.now()
        outcome = action()
        elapsed = (datetime.now() - started).total_seconds()

        size = len(outcome) if hasattr(outcome, "__len__") else None
        if size is None:
            logger.info("stage %-22s %.3fs", name, elapsed)
        else:
            logger.info("stage %-22s %.3fs  n=%d", name, elapsed, size)
        return outcome

    def _summarise(
        self,
        run_id: UUID,
        raw_sales: list[RawSalesRecord],
        valid: list[ValidSalesRecord],
        rejected: list[RejectedRecord],
        rows_loaded: int,
        started_at: datetime,
    ) -> PipelineResult:
        """Assemble the run summary from counts other layers produced.

        Every number here is a `len()` of something handed back by another stage or a value
        returned by the loader. Nothing is recomputed — if this method ever needs to *derive*
        a figure, the derivation belongs in the layer that owns the data.

        `dry_run` is read from the flag, never inferred from `rows_loaded == 0`. A real run
        over a file whose every row was rejected also loads nothing, and reporting that as a
        dry run would tell the caller the database is untouched when it has just been
        rewritten.
        """
        return PipelineResult(
            run_id=run_id,
            rows_extracted=len(raw_sales),
            rows_valid=len(valid),
            rows_rejected=len(rejected),
            rows_loaded=rows_loaded,
            duration_seconds=(datetime.now() - started_at).total_seconds(),
            dry_run=self.dry_run,
            status="SUCCESS",
        )
