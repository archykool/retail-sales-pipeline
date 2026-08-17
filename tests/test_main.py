"""
Tests for the entry point (Step 10+).

`main()` returns an int rather than calling `sys.exit`, which is what makes these possible
without catching `SystemExit`. The exit code is the contract a scheduler depends on, so it
is the thing most worth pinning: a pipeline that fails and returns 0 is worse than one that
does not run at all, because nothing notices.

These use `--dry-run` throughout, so none of them touch a database.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import main as entry_point

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


@pytest.fixture(autouse=True)
def quiet_logging():
    """Keep the entry point's logging config from leaking into other test modules.

    `main()` calls `basicConfig(force=True)`, which replaces the root handlers for the whole
    process. Restoring them afterwards keeps this file from changing how anything else logs.
    """
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    yield
    root.handlers[:], root.level = saved


# ======================================================================
# Argument parsing
# ======================================================================


def test_defaults() -> None:
    args = entry_point.parse_args([])

    assert args.dry_run is False
    assert args.file is None
    assert args.log_level == "INFO"


def test_dry_run_flag() -> None:
    assert entry_point.parse_args(["--dry-run"]).dry_run is True


def test_file_is_parsed_as_a_path() -> None:
    args = entry_point.parse_args(["--file", "data/raw/other.csv"])

    assert args.file == Path("data/raw/other.csv")


def test_invalid_log_level_is_refused_by_argparse() -> None:
    """A typo in --log-level should fail immediately, not silently pick a default."""
    with pytest.raises(SystemExit) as exit_info:
        entry_point.parse_args(["--log-level", "TRACE"])

    assert exit_info.value.code == 2  # argparse's usage-error code


# ======================================================================
# --file overriding config
# ======================================================================


def test_file_argument_replaces_only_the_sales_file(pipeline_config, monkeypatch) -> None:
    """`--file` swaps one field on a frozen config and leaves the rest alone.

    The config stays the single source of truth for which file a run processes, so the run
    log cannot disagree with the pipeline about what was loaded.
    """
    monkeypatch.setattr(entry_point.PipelineConfig, "from_env", lambda: pipeline_config)
    override = RAW_DIR / "other.csv"

    config = entry_point.build_config(override)

    assert config.sales_file == override
    assert config.customers_file == pipeline_config.customers_file
    assert config.db_name == pipeline_config.db_name


def test_no_file_argument_leaves_config_untouched(pipeline_config, monkeypatch) -> None:
    monkeypatch.setattr(entry_point.PipelineConfig, "from_env", lambda: pipeline_config)

    assert entry_point.build_config(None) == pipeline_config


# ======================================================================
# Exit codes — the reason this file exists
# ======================================================================


def test_dry_run_returns_zero(monkeypatch, tmp_path: Path, pipeline_config) -> None:
    import dataclasses

    monkeypatch.setattr(
        entry_point.PipelineConfig,
        "from_env",
        lambda: dataclasses.replace(
            pipeline_config,
            sales_file=RAW_DIR / "sales_2026_01.csv",
            customers_file=RAW_DIR / "customers.json",
            products_file=RAW_DIR / "products.json",
            rejected_dir=tmp_path,
        ),
    )

    assert entry_point.main(["--dry-run"]) == 0


def test_missing_input_file_returns_one(monkeypatch, tmp_path: Path, pipeline_config) -> None:
    """The §9 exit criterion, as a test rather than a manual check."""
    import dataclasses

    monkeypatch.setattr(
        entry_point.PipelineConfig,
        "from_env",
        lambda: dataclasses.replace(pipeline_config, rejected_dir=tmp_path),
    )

    assert entry_point.main(["--dry-run", "--file", str(tmp_path / "nope.csv")]) == 1


def test_missing_env_var_returns_one(monkeypatch) -> None:
    """A configuration failure is a failure, not a run with defaults (D-019).

    `load_dotenv` is neutered so the real `.env` cannot repopulate the variable — which is
    the same accident that caught this for real during Step 8b.
    """
    monkeypatch.setattr(entry_point, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("DB_HOST", raising=False)

    assert entry_point.main(["--dry-run"]) == 1


def test_failure_reports_on_stderr_and_logs_the_traceback(monkeypatch, capsys) -> None:
    """Both channels: a readable line for the person, a traceback for diagnosis.

    Asserted through stderr rather than `caplog`, and the reason is worth knowing.
    `configure_logging` calls `basicConfig(force=True)`, which removes *every* existing root
    handler — including the one pytest installs for `caplog`. So `caplog.text` comes back
    empty even though the logging worked perfectly.

    That is the same property, seen from the other side, that makes `basicConfig` an
    entry-point-only call: it overwrites whatever its host had configured. Here the host is
    pytest; in production it would be whatever imported the package. `src/` must never do
    this, which `test_no_module_under_src_configures_logging` enforces.
    """
    monkeypatch.setattr(entry_point, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("DB_HOST", raising=False)

    assert entry_point.main(["--dry-run"]) == 1

    stderr = capsys.readouterr().err
    assert "ERROR: ValueError: Required env var missing: DB_HOST" in stderr
    assert "run failed" in stderr          # the logged line
    assert "Traceback" in stderr           # logger.exception, not logger.error


# ======================================================================
# Logging setup belongs here, not in src/
# ======================================================================


def test_configure_logging_does_not_stack_handlers() -> None:
    """`force=True`, so a second call replaces handlers instead of double-logging.

    Without it, anything calling `main()` twice in one interpreter — a test, or a future
    scheduler loop — emits every line twice.
    """
    entry_point.configure_logging("INFO")
    first = len(logging.getLogger().handlers)

    entry_point.configure_logging("DEBUG")

    assert len(logging.getLogger().handlers) == first
    assert logging.getLogger().level == logging.DEBUG


def test_no_module_under_src_configures_logging() -> None:
    """A library that configures logging has made a decision for its host.

    Checked at the source level because the failure is silent: it works fine until an
    application imports the package and finds its own log format overwritten.
    """
    src = Path(__file__).resolve().parent.parent / "src"

    offenders = [
        path.name
        for path in src.glob("*.py")
        if "basicConfig" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_no_module_under_src_prints() -> None:
    """`src/` logs; the entry point talks to the terminal (§13)."""
    src = Path(__file__).resolve().parent.parent / "src"

    offenders = [
        path.name
        for path in src.glob("*.py")
        if "print(" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
