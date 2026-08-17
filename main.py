"""
Command-line entry point. Wires configuration to the pipeline and nothing else.

**This file exists to translate an exception into an exit code.** `SalesPipeline.run()`
raises on failure rather than returning a FAILED result, because swallowing the error would
hide the traceback from whoever needs it. But a scheduler does not catch exceptions — it
reads exit codes. Turning one into the other is the entire reason this is a separate file
from `pipeline.py`, and it is why `main.py` has no other job.

Three responsibilities the pipeline layers deliberately do not have:

- **`load_dotenv()` is called here and nowhere else** (D-018). `PipelineConfig.from_env()`
  reads `os.environ` only, so the same code path serves a developer with a `.env` file and a
  container with injected variables. A library that loads `.env` on import has decided
  something for its host.
- **`logging.basicConfig()` is called here and nowhere else.** Modules inside `src/` get a
  logger and log to it; configuring handlers and levels for the whole process is the
  application's choice, not a library's.
- **`print()` happens here and nowhere else.** `src/` logs; the entry point talks to the
  person who typed the command. §8.1's requirement that dry-run "prints the summary" is
  satisfied on this side of that line.

No business logic. If this file ever computes something about the data, it is in the wrong
file — every number below comes from the `PipelineResult` the pipeline returned.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.config import PipelineConfig
from src.pipeline import SalesPipeline

logger = logging.getLogger("main")

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)-18s %(message)s"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Load a retail sales file into the PostgreSQL star schema.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run extract, validate and transform, write both CSVs, and open no database "
        "connection at all",
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=None,
        metavar="PATH",
        help="sales CSV to process, overriding SALES_FILE from the environment",
    )
    parser.add_argument(
        "--log-level",
        choices=LOG_LEVELS,
        default="INFO",
        help="logging verbosity (default: INFO)",
    )
    return parser.parse_args(argv)


def configure_logging(level: str) -> None:
    """Set up logging for the process.

    `force=True` so a second call in the same interpreter — which is what a test does —
    replaces the handlers instead of silently adding another and double-logging every line.
    """
    logging.basicConfig(
        level=getattr(logging, level),
        format=LOG_FORMAT,
        datefmt="%H:%M:%S",
        force=True,
    )


def build_config(sales_file: Path | None) -> PipelineConfig:
    """Read config from the environment, with `--file` replacing one field.

    `dataclasses.replace` rather than a parameter threaded into `SalesPipeline`:
    `PipelineConfig` is frozen, and the config stays the single source of truth for which
    file a run processes. A pipeline that could be told a different filename than its config
    holds would have two answers to that question, and the run log records only one of them.
    """
    config = PipelineConfig.from_env()
    if sales_file is not None:
        config = dataclasses.replace(config, sales_file=sales_file)
    return config


def print_summary(result, config: PipelineConfig) -> None:
    """Report the run to the person who typed the command.

    Every value is read straight off the `PipelineResult`. Nothing is recomputed here — the
    summary is a rendering, not a calculation.
    """
    mode = "DRY RUN (no database connection opened)" if result.dry_run else "LOAD"

    print()
    print("=" * 62)
    print(f"  {mode}")
    print("=" * 62)
    print(f"  run_id           {result.run_id}")
    print(f"  source file      {config.sales_file.name}")
    print(f"  database         {'-' if result.dry_run else config.db_name}")
    print("-" * 62)
    print(f"  rows extracted   {result.rows_extracted}")
    print(f"  rows valid       {result.rows_valid}")
    print(f"  rows rejected    {result.rows_rejected}")
    print(f"  rows loaded      {result.rows_loaded}")
    print("-" * 62)
    print(f"  duration         {result.duration_seconds:.2f}s")
    print(f"  status           {result.status}")
    print(f"  rejected dir     {config.rejected_dir}")
    print("=" * 62)
    print()


def main(argv: list[str] | None = None) -> int:
    """Return the process exit code: 0 on success, 1 on any failure.

    Returns rather than calling `sys.exit`, so a test can assert on the code without
    catching `SystemExit`. The `__main__` block below does the exiting.
    """
    args = parse_args(argv)
    configure_logging(args.log_level)

    # The one call, at the one place that is allowed to make this decision (D-018).
    load_dotenv()

    try:
        config = build_config(args.file)
        result = SalesPipeline(config, dry_run=args.dry_run).run()
    except Exception as error:
        # Not a bare except: the traceback goes to the log for diagnosis, and a single
        # readable line goes to stderr for the person watching. Swallowing either would
        # make a failed run look like a quiet one.
        logger.exception("run failed")
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print_summary(result, config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
