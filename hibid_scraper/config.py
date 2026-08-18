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

    # Search parameters.
    #
    # HiBid's radius is whole miles, and the pipeline's question is "within
    # 50 km of home" — 31 mi is 49.9 km, the closest it can be asked. HiBid
    # honours it loosely (a ~60 km Hamilton auction still comes back) and
    # publishes no distance of its own, so the exact cutoff is applied in
    # silver_enhanced against postal centroids. Ask narrow, filter exactly.
    radius_miles: int = 31
    search_categories: list[str] = None

    # Scope a lot scrape to one auction's catalogue instead of a radius search.
    # Empty means radius search.
    auction_id: str = ""

    # The auction as discovery saw it, inlined onto each lot when the catalogue
    # page does not carry one of its own.
    #
    # Catalogue pages state the auction once at the top rather than on every
    # lot, and some omit it entirely — those lots would land unplaceable and be
    # dropped by silver. Discovery has already fetched every auction it
    # schedules, so the copy is free. The page's own copy still wins when it
    # exists: it carries buyerPremium, bidIncrements and paymentInfo, which the
    # discovery feed does not return.
    auction_payload: Optional[dict] = None

    # Page to resume catalogue pagination from, 1 being the start.
    #
    # A large catalogue cannot be taken in one pass, and restarting at page 1
    # every run just re-collects the front of it. The orchestrator carries the
    # last page that yielded lots so the next run continues past it.
    catalog_start_page: int = 1

    # Pages already banked for this auction, and how many it is expected to
    # have. The complement is the work for this pass.
    catalog_pages_done: Optional[list] = None
    catalog_expected_pages: int = 0

    # Most pages one pass will fetch for a single auction, so one 43-page
    # catalogue cannot monopolise a sweep of twenty auctions.
    catalog_page_budget: int = 15

    # How many lots discovery said this auction holds.
    #
    # The only independent check on whether a catalogue was captured whole. A
    # truncated page and a last page look identical, so without a count to
    # compare against, a partial scrape reports success. Approximate — sellers
    # pull lots — so it decides "materially short", never exact equality.
    auction_lot_count: Optional[int] = None

    # How far ahead auction discovery pages before it stops.
    discovery_horizon_days: int = 14

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
            radius_miles=int(os.getenv("RADIUS_MILES", "31")),
            search_categories=categories,
            auction_id=os.getenv("AUCTION_ID", "").strip(),
            discovery_horizon_days=int(os.getenv("DISCOVERY_HORIZON_DAYS", "14")),
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
