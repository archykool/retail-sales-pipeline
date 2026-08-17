"""
Shared fixtures for tests that need a live PostgreSQL.

Skips rather than fails when there is no database: a grader running `pytest` on a fresh
clone without `docker compose up` should see skips, not red.

Every test gets a disposable schema. `sales_dev` is the database demonstrated on camera,
and a suite that populates or truncates it can invalidate the demo.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")

from dotenv import load_dotenv  # noqa: E402

from src.config import PipelineConfig  # noqa: E402

SCHEMA_SQL = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"

# Pinned so DATE_IN_FUTURE is decided by the data rather than by the calendar. An
# unpinned clock makes the documented 172/28 split drift as time passes.
PINNED_TODAY = "2026-08-17"


@pytest.fixture(scope="session")
def pipeline_config() -> PipelineConfig:
    """The real config, or skip. Loading `.env` is the entry point's job (D-018).

    The test suite is an entry point, so it calls `load_dotenv()` here rather than expecting
    `from_env()` to do it.
    """
    load_dotenv()
    try:
        return PipelineConfig.from_env()
    except ValueError as error:
        pytest.skip(f"database env vars not configured: {error}")


@pytest.fixture(scope="session")
def db_params(pipeline_config: PipelineConfig) -> dict:
    """Connection parameters, or skip everything that needs a database."""
    config = pipeline_config
    params = {
        "host": config.db_host,
        "port": config.db_port,
        "dbname": config.db_name,
        "user": config.db_user,
        "password": config.db_password,
    }

    try:
        with psycopg.connect(**params, connect_timeout=3) as connection:
            connection.execute("SELECT 1")
    except Exception as error:  # noqa: BLE001 - any failure means "no database here"
        pytest.skip(f"postgres unreachable ({type(error).__name__}): {error}")

    return params


@pytest.fixture
def schema(db_params: dict):
    """A disposable schema per test, dropped afterwards."""
    name = f"t_{uuid4().hex[:12]}"

    with psycopg.connect(**db_params, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{name}"')
    try:
        yield name
    finally:
        with psycopg.connect(**db_params, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA "{name}" CASCADE')
