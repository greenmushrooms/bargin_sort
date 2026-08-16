"""
Configuration management for HiBid Scraper.

Loads settings from environment variables or .env file.
"""

import os
from dataclasses import dataclass
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
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "bargin_sort"
    db_password: str = ""
    db_name: str = "bargin_sort"

    # FlareSolverr fallback for when Cloudflare 403s a direct request.
    # Empty string disables the fallback.
    flaresolverr_url: str = "http://flaresolverr:8191/v1"
    flaresolverr_timeout_ms: int = 60000

    # dbt project location. Empty means "look beside, then above, this module".
    dbt_project_dir: str = ""

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

        # Not required here: the flow always overwrites it from its own
        # `zip_code` parameter, so demanding it from the environment only made
        # a deployment crash before it ever reached the scrape. Validated at
        # the point of use instead, in scrape_hibid().
        zip_code = os.getenv("ZIP_CODE", "")

        # Parse search categories
        categories_str = os.getenv("SEARCH_CATEGORIES", "")
        categories = [c.strip() for c in categories_str.split(",") if c.strip()]

        return cls(
            zip_code=zip_code,
            radius_miles=int(os.getenv("RADIUS_MILES", "50")),
            search_categories=categories,
            test_mode=os.getenv("TEST_MODE", "false").lower() == "true",
            test_limit=int(os.getenv("TEST_LIMIT", "20")),
            db_host=os.getenv("DB_HOST", "localhost"),
            db_port=int(os.getenv("DB_PORT", "5432")),
            db_user=os.getenv("DB_USER", "bargin_sort"),
            db_password=os.getenv("DB_PASSWORD", ""),
            db_name=os.getenv("DB_NAME", "bargin_sort"),
            flaresolverr_url=os.getenv(
                "FLARESOLVERR_URL", "http://flaresolverr:8191/v1"
            ).strip(),
            flaresolverr_timeout_ms=int(os.getenv("FLARESOLVERR_TIMEOUT_MS", "60000")),
            dbt_project_dir=os.getenv("DBT_PROJECT_DIR", "").strip(),
            request_delay_min=int(os.getenv("REQUEST_DELAY_MIN", "2")),
            request_delay_max=int(os.getenv("REQUEST_DELAY_MAX", "5")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )

    def get_connection_string(self) -> str:
        """Build PostgreSQL connection string from parts."""
        return f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"
