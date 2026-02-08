"""
Configuration management for HiBid Scraper.

Loads settings from environment variables or .env file.
"""

import os
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv

# Load .env file if present
load_dotenv()


@dataclass
class Config:
    """Scraper configuration settings."""

    # Required
    zip_code: str

    # Search parameters
    radius_miles: int = 50
    search_categories: list[str] = None

    # Mode settings
    test_mode: bool = False
    test_limit: int = 20

    # Database
    database_url: str = "sqlite:///hibid_auctions.db"

    # Rate limiting
    request_delay_min: int = 2
    request_delay_max: int = 5

    # Logging
    log_level: str = "INFO"

    def __post_init__(self):
        if self.search_categories is None:
            self.search_categories = []

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables."""

        zip_code = os.getenv("ZIP_CODE")
        if not zip_code:
            raise ValueError("ZIP_CODE environment variable is required")

        # Parse search categories
        categories_str = os.getenv("SEARCH_CATEGORIES", "")
        categories = [c.strip() for c in categories_str.split(",") if c.strip()]

        return cls(
            zip_code=zip_code,
            radius_miles=int(os.getenv("RADIUS_MILES", "50")),
            search_categories=categories,
            test_mode=os.getenv("TEST_MODE", "false").lower() == "true",
            test_limit=int(os.getenv("TEST_LIMIT", "20")),
            database_url=os.getenv("DATABASE_URL", "sqlite:///hibid_auctions.db"),
            request_delay_min=int(os.getenv("REQUEST_DELAY_MIN", "2")),
            request_delay_max=int(os.getenv("REQUEST_DELAY_MAX", "5")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )

    def is_sqlite(self) -> bool:
        """Check if using SQLite database."""
        return self.database_url.startswith("sqlite:")

    def is_postgres(self) -> bool:
        """Check if using PostgreSQL database."""
        return self.database_url.startswith("postgresql:")

    def get_sqlite_path(self) -> Optional[str]:
        """Extract SQLite file path from database URL."""
        if self.is_sqlite():
            # Format: sqlite:///path/to/db.db
            return self.database_url.replace("sqlite:///", "")
        return None
