"""
Database management for HiBid Scraper.

Writes the raw layer of the medallion: JSONB payloads, append-only, one row per
lot per scrape. Nothing here updates or deletes, so silver and silver_enhanced
can always be rebuilt by replaying this table.

Schema: raw (see sql/003_raw_schema.sql)
Tables: raw.hibid (monthly partitions), raw.scrape_runs

One landing table per source. Only per-row facts are stored here — the job that
produced the row, the item, and the payload. Per-run parameters such as zip code
and radius live on raw.scrape_runs.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor, Json, execute_values

from config import Config

logger = logging.getLogger(__name__)

SCHEMA = "raw"

# Landing table per source; the table is named for the source.
DEFAULT_SOURCE = "hibid"

SQL_DIR = Path(__file__).parent / "sql"
# Applied on connect: the shared schema first, then each source's table.
DDL_FILES = (
    "003_raw_schema.sql",
    "005_police_auctions.sql",
    "006_hibid_auctions.sql",
    "007_catalog_progress.sql",
    "008_catalog_pages.sql",
    "009_catalog_page_attempts.sql",
    "010_catalog_end_page.sql",
    "011_wishlist_alerts.sql",
)

# Rows buffered before a write. A full scrape is ~30k rows and committing each
# one separately dominates the runtime.
INSERT_BATCH_SIZE = 500


class Database:
    """PostgreSQL database handler for storing raw auction JSON payloads."""

    def __init__(self, config: Config, source: str = DEFAULT_SOURCE):
        self.config = config
        self.source = source
        # One landing table per source, named for it.
        self.table = source
        self.conn = None
        self._pending: list[tuple] = []
        self._ensured_months: set[str] = set()

    def connect(self) -> None:
        """Establish database connection."""
        logger.info("Connecting to PostgreSQL database")
        self.conn = psycopg2.connect(
            host=self.config.db_host,
            port=self.config.db_port,
            user=self.config.db_user,
            password=self.config.db_password,
            dbname=self.config.db_name,
        )
        self._init_schema()

    def close(self) -> None:
        """Close database connection, writing out anything still buffered."""
        if self.conn:
            try:
                self.flush()
            except psycopg2.Error as e:
                logger.error(f"Could not flush buffered rows on close: {e}")
            self.conn.close()
            self.conn = None
            logger.info("Database connection closed")

    def _init_schema(self) -> None:
        """
        Apply the raw DDL and make sure this month's partition exists.

        The DDL lives in sql/003_raw_schema.sql so the schema has a single
        definition that can also be applied by hand or from a migration runner.
        """
        with self.conn.cursor() as cursor:
            for name in DDL_FILES:
                cursor.execute((SQL_DIR / name).read_text())
        self.conn.commit()
        logger.info("Raw schema initialized")
        self._ensure_partition(datetime.now(timezone.utc))

    def _ensure_partition(self, when: datetime) -> None:
        """Create the monthly partition covering `when`, once per month seen."""
        month_key = when.strftime("%Y-%m")
        if month_key in self._ensured_months:
            return
        with self.conn.cursor() as cursor:
            cursor.execute(
                f"SELECT {SCHEMA}.ensure_month_partition(%s, %s)",
                (self.table, when.date()),
            )
            partition = cursor.fetchone()[0]
        self.conn.commit()
        self._ensured_months.add(month_key)
        logger.info(f"Raw partition ready: {partition}")

    def start_scrape_run(
        self,
        zip_code: str,
        radius_miles: int,
        test_mode: bool,
        sys_run_name: str = "",
        auction_id: Optional[int] = None,
    ) -> int:
        """
        Record the start of a scrape run. Returns run ID.

        `auction_id` is what makes "scrape each auction once" enforceable: a
        completed run carrying one is the orchestrator's proof that auction has
        been captured. NULL for radius sweeps and discovery passes.
        """
        with self.conn.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {SCHEMA}.scrape_runs
                (source, sys_run_name, started_at, zip_code, radius_miles,
                 test_mode, auction_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    self.source,
                    sys_run_name,
                    datetime.now(timezone.utc),
                    zip_code,
                    radius_miles,
                    test_mode,
                    auction_id,
                ),
            )
            run_id = cursor.fetchone()[0]
        self.conn.commit()
        return run_id

    def complete_scrape_run(
        self,
        run_id: int,
        items_found: int,
        items_inserted: int,
        errors: int,
        status: str = "completed",
    ) -> None:
        """Record the completion of a scrape run."""
        with self.conn.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE {SCHEMA}.scrape_runs
                SET completed_at = %s, items_found = %s, items_inserted = %s,
                    errors = %s, status = %s
                WHERE id = %s
                """,
                (
                    datetime.now(timezone.utc),
                    items_found,
                    items_inserted,
                    errors,
                    status,
                    run_id,
                ),
            )
        self.conn.commit()

    def insert_item(
        self,
        item_id: str,
        raw_json: dict,
        category: Optional[str] = None,
        sys_run_name: str = "",
    ) -> None:
        """
        Queue a raw item for insertion.

        No dedup — raw keeps every observation of every lot, so a later run
        never overwrites what an earlier one saw. Rows are buffered and written
        in batches; call flush() to force the remainder out.

        Scrape parameters are not repeated here; they belong to the run and are
        recorded once by start_scrape_run().
        """
        scraped_at = datetime.now(timezone.utc)
        self._ensure_partition(scraped_at)
        self._pending.append(
            (sys_run_name, item_id, category, Json(raw_json), scraped_at)
        )
        if len(self._pending) >= INSERT_BATCH_SIZE:
            self.flush()

    def flush(self) -> int:
        """Write any buffered rows. Returns how many were written."""
        if not self._pending:
            return 0

        rows = self._pending
        self._pending = []
        with self.conn.cursor() as cursor:
            execute_values(
                cursor,
                f"""
                INSERT INTO {SCHEMA}.{self.table}
                (sys_run_name, item_id, category, raw_json, scraped_at)
                VALUES %s
                """,
                rows,
            )
        self.conn.commit()
        logger.debug(f"Flushed {len(rows)} rows to {SCHEMA}.{self.table}")
        return len(rows)

    def get_catalog_pages(
        self, auction_id: int
    ) -> tuple[set[int], set[int], dict[int, int], Optional[int]]:
        """
        Pages banked, pages failed, attempt counts, and where the catalogue ends.

        The complement of the banked set, up to expected_pages, is the work the
        next pass should do — so a page that failed is simply still outstanding
        rather than something that halted the scan. Attempt counts are what stop
        an unreadable page from starving pages never tried.
        """
        with self.conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT pages_done, pages_failed, page_attempts, end_page
                FROM {SCHEMA}.catalog_pages WHERE auction_id = %s
                """,
                (auction_id,),
            )
            row = cursor.fetchone()
        if not row:
            return set(), set(), {}, None
        attempts = {int(k): v for k, v in (row[2] or {}).items()}
        return set(row[0] or []), set(row[1] or []), attempts, row[3]

    def save_catalog_pages(
        self,
        auction_id: int,
        pages_done: set[int],
        pages_failed: set[int],
        expected_pages: int,
        attempted: Optional[set[int]] = None,
        end_page: Optional[int] = None,
    ) -> None:
        """
        Merge this pass's page results into the auction's progress.

        Union rather than replace: a pass only ever adds to what is known, so a
        weak pass can never lose ground the way the old high-water mark did.
        A page that succeeds is removed from the failed set, since a later
        success supersedes an earlier failure.
        """
        # Merge rather than replace: the JSONB || operator takes the right-hand
        # value per key, so the incremented counts must be computed here.
        _, _, existing, _ = self.get_catalog_pages(auction_id)
        bumped = {
            str(page): existing.get(page, 0) + 1
            for page in (attempted or (pages_done | pages_failed))
        }

        with self.conn.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {SCHEMA}.catalog_pages
                    (auction_id, pages_done, pages_failed, expected_pages,
                     page_attempts, end_page, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (auction_id) DO UPDATE SET
                    pages_done = ARRAY(
                        SELECT DISTINCT unnest(
                            {SCHEMA}.catalog_pages.pages_done || EXCLUDED.pages_done
                        ) ORDER BY 1
                    ),
                    pages_failed = ARRAY(
                        SELECT DISTINCT p FROM unnest(
                            {SCHEMA}.catalog_pages.pages_failed || EXCLUDED.pages_failed
                        ) AS p
                        WHERE p <> ALL (
                            {SCHEMA}.catalog_pages.pages_done || EXCLUDED.pages_done
                        )
                        ORDER BY 1
                    ),
                    expected_pages = GREATEST(EXCLUDED.expected_pages,
                                              {SCHEMA}.catalog_pages.expected_pages),
                    page_attempts = {SCHEMA}.catalog_pages.page_attempts ||
                                    EXCLUDED.page_attempts,
                    -- Lowest wins: a catalogue only ever shrinks as it closes.
                    end_page = LEAST(
                        NULLIF({SCHEMA}.catalog_pages.end_page, 0),
                        NULLIF(EXCLUDED.end_page, 0)
                    ),
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    auction_id,
                    sorted(pages_done),
                    sorted(pages_failed),
                    expected_pages,
                    Json(bumped),
                    end_page,
                    datetime.now(timezone.utc),
                ),
            )
        self.conn.commit()

    def get_catalog_progress(self, auction_id: int) -> int:
        """Last page of this auction's catalogue that yielded lots."""
        with self.conn.cursor() as cursor:
            cursor.execute(
                f"SELECT last_page FROM {SCHEMA}.catalog_progress WHERE auction_id = %s",
                (auction_id,),
            )
            row = cursor.fetchone()
        return row[0] if row else 0

    def save_catalog_progress(
        self, auction_id: int, last_page: int, lots_seen: int, reset: bool = False
    ) -> None:
        """
        Record where pagination reached, so the next run continues from there.

        Written even when the run failed its completeness check: a partial pass
        still moved the catalogue forward, and discarding that is what kept
        large auctions re-reading page 1 forever.
        """
        with self.conn.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {SCHEMA}.catalog_progress
                    (auction_id, last_page, lots_seen, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (auction_id) DO UPDATE
                    SET last_page  = CASE
                            WHEN %s THEN EXCLUDED.last_page
                            ELSE GREATEST({SCHEMA}.catalog_progress.last_page,
                                          EXCLUDED.last_page)
                        END,
                        lots_seen  = {SCHEMA}.catalog_progress.lots_seen + EXCLUDED.lots_seen,
                        updated_at = EXCLUDED.updated_at
                """,
                (auction_id, last_page, lots_seen, datetime.now(timezone.utc), reset),
            )
        self.conn.commit()

    def get_item(self, item_id: str) -> Optional[dict]:
        """Retrieve an item by ID."""
        with self.conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                f"SELECT * FROM {SCHEMA}.{self.table} WHERE item_id = %s", (item_id,)
            )
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None

    def get_recent_items(self, limit: int = 100) -> list[dict]:
        """Get most recently scraped items."""
        with self.conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                f"""
                SELECT * FROM {SCHEMA}.{self.table}
                ORDER BY scraped_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_item_count(self) -> int:
        """Get total number of items in database."""
        with self.conn.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{self.table}")
            return cursor.fetchone()[0]

    def get_run_stats(self, run_id: int) -> Optional[dict]:
        """Get statistics for a scrape run."""
        with self.conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(f"SELECT * FROM {SCHEMA}.scrape_runs WHERE id = %s", (run_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
