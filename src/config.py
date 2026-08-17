"""
Pipeline configuration, read from environment variables.

This module is the single source of truth for paths and database credentials.
It fails loudly at startup (not mid-run) if required values are missing,
and masks sensitive data in __repr__ to prevent accidental log leaks.
"""

from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class PipelineConfig:
    """Database and file paths for the ETL pipeline.

    All values come from environment variables. Required values raise immediately
    if missing; optional values have sensible defaults. The entry point (main.py)
    owns calling load_dotenv() to populate the environment before from_env() runs.
    """

    # Database
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str

    # File paths
    sales_file: Path
    customers_file: Path
    products_file: Path
    rejected_dir: Path

    # Validator thresholds
    max_quantity: int

    # Loader tuning
    load_batch_size: int

    @classmethod
    def from_env(cls) -> "PipelineConfig":
        """Load config from environment variables.

        Required env vars (raise if missing):
            DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
            SALES_FILE, CUSTOMERS_FILE, PRODUCTS_FILE
            REJECTED_DIR

        Optional env vars (have defaults):
            MAX_QUANTITY (default: 1000)
            LOAD_BATCH_SIZE (default: 1000)

        Returns:
            Fully initialized PipelineConfig.

        Raises:
            ValueError: if a required env var is missing or malformed.
        """

        # Required database settings
        db_host = os.getenv("DB_HOST")
        if not db_host:
            raise ValueError("Required env var missing: DB_HOST")

        db_port_str = os.getenv("DB_PORT")
        if not db_port_str:
            raise ValueError("Required env var missing: DB_PORT")
        try:
            db_port = int(db_port_str)
        except ValueError as e:
            raise ValueError(f"DB_PORT must be an integer, got: {db_port_str}") from None

        db_name = os.getenv("DB_NAME")
        if not db_name:
            raise ValueError("Required env var missing: DB_NAME")

        db_user = os.getenv("DB_USER")
        if not db_user:
            raise ValueError("Required env var missing: DB_USER")

        db_password = os.getenv("DB_PASSWORD")
        if not db_password:
            raise ValueError("Required env var missing: DB_PASSWORD")

        # Required file paths
        sales_file_str = os.getenv("SALES_FILE")
        if not sales_file_str:
            raise ValueError("Required env var missing: SALES_FILE")
        sales_file = Path(sales_file_str)

        customers_file_str = os.getenv("CUSTOMERS_FILE")
        if not customers_file_str:
            raise ValueError("Required env var missing: CUSTOMERS_FILE")
        customers_file = Path(customers_file_str)

        products_file_str = os.getenv("PRODUCTS_FILE")
        if not products_file_str:
            raise ValueError("Required env var missing: PRODUCTS_FILE")
        products_file = Path(products_file_str)

        rejected_dir_str = os.getenv("REJECTED_DIR")
        if not rejected_dir_str:
            raise ValueError("Required env var missing: REJECTED_DIR")
        rejected_dir = Path(rejected_dir_str)

        # Optional validator thresholds
        max_quantity_str = os.getenv("MAX_QUANTITY", "1000")
        try:
            max_quantity = int(max_quantity_str)
        except ValueError as e:
            raise ValueError(f"MAX_QUANTITY must be an integer, got: {max_quantity_str}") from None

        # Optional loader tuning
        load_batch_size_str = os.getenv("LOAD_BATCH_SIZE", "1000")
        try:
            load_batch_size = int(load_batch_size_str)
        except ValueError as e:
            raise ValueError(f"LOAD_BATCH_SIZE must be an integer, got: {load_batch_size_str}") from None

        return cls(
            db_host=db_host,
            db_port=db_port,
            db_name=db_name,
            db_user=db_user,
            db_password=db_password,
            sales_file=sales_file,
            customers_file=customers_file,
            products_file=products_file,
            rejected_dir=rejected_dir,
            max_quantity=max_quantity,
            load_batch_size=load_batch_size,
        )

    def __repr__(self) -> str:
        """Return a string representation with the password masked.

        Passwords never appear in logs or error messages, even during debugging.
        """
        return (
            f"PipelineConfig("
            f"db_host={self.db_host!r}, "
            f"db_port={self.db_port}, "
            f"db_name={self.db_name!r}, "
            f"db_user={self.db_user!r}, "
            f"db_password='***', "
            f"sales_file={self.sales_file}, "
            f"customers_file={self.customers_file}, "
            f"products_file={self.products_file}, "
            f"rejected_dir={self.rejected_dir}, "
            f"max_quantity={self.max_quantity}, "
            f"load_batch_size={self.load_batch_size}"
            f")"
        )
