"""
Database management for HiBid Scraper.

Writes the bronze layer of the medallion: raw JSONB payloads, append-only, one
row per lot per scrape. Nothing here updates or deletes, so silver and
silver_enhanced can always be rebuilt by replaying this table.

Schema: bronze (see sql/001_bronze.sql)
Tables: raw_auction_items (monthly partitions), scrape_runs
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor, Json, execute_values

from config import Config

logger = logging.getLogger(__name__)

SCHEMA = "bronze"

DDL_PATH = Path(__file__).parent / "sql" / "001_bronze.sql"

# Rows buffered before a write. A full scrape is ~30k rows and committing each
# one separately dominates the runtime.
INSERT_BATCH_SIZE = 500


class Database:
    """PostgreSQL database handler for storing raw auction JSON payloads."""

    def __init__(self, config: Config):
        self.config = config
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
        Apply the bronze DDL and make sure this month's partition exists.

        The DDL lives in sql/001_bronze.sql so the schema has a single
        definition that can also be applied by hand or from a migration runner.
        """
        ddl = DDL_PATH.read_text()
        with self.conn.cursor() as cursor:
            cursor.execute(ddl)
        self.conn.commit()
        logger.info("Bronze schema initialized")
        self._ensure_partition(datetime.now(timezone.utc))

    def _ensure_partition(self, when: datetime) -> None:
        """Create the monthly partition covering `when`, once per month seen."""
        month_key = when.strftime("%Y-%m")
        if month_key in self._ensured_months:
            return
        with self.conn.cursor() as cursor:
            cursor.execute(
                f"SELECT {SCHEMA}.ensure_month_partition(%s)", (when.date(),)
            )
            partition = cursor.fetchone()[0]
        self.conn.commit()
        self._ensured_months.add(month_key)
        logger.info(f"Bronze partition ready: {partition}")

    def start_scrape_run(self, zip_code: str, radius_miles: int, test_mode: bool, sys_run_name: str = "") -> int:
        """Record the start of a scrape run. Returns run ID."""
        with self.conn.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {SCHEMA}.scrape_runs (started_at, zip_code, radius_miles, test_mode, sys_run_name)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (datetime.now(timezone.utc), zip_code, radius_miles, test_mode, sys_run_name),
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
        zip_code: str,
        radius_miles: int,
        category: Optional[str] = None,
        sys_run_name: str = "",
    ) -> None:
        """
        Queue a raw auction item for insertion.

        No dedup — bronze keeps every observation of every lot, so a later run
        never overwrites what an earlier one saw. Rows are buffered and written
        in batches; call flush() to force the remainder out.
        """
        scraped_at = datetime.now(timezone.utc)
        self._ensure_partition(scraped_at)
        self._pending.append(
            (
                item_id,
                Json(raw_json),
                scraped_at,
                zip_code,
                radius_miles,
                category,
                sys_run_name,
            )
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
                INSERT INTO {SCHEMA}.raw_auction_items
                (item_id, raw_json, scraped_at, zip_code, radius_miles, category, sys_run_name)
                VALUES %s
                """,
                rows,
            )
        self.conn.commit()
        logger.debug(f"Flushed {len(rows)} rows to {SCHEMA}.raw_auction_items")
        return len(rows)

    def get_item(self, item_id: str) -> Optional[dict]:
        """Retrieve an item by ID."""
        with self.conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                f"SELECT * FROM {SCHEMA}.raw_auction_items WHERE item_id = %s", (item_id,)
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
                SELECT * FROM {SCHEMA}.raw_auction_items
                ORDER BY scraped_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_item_count(self) -> int:
        """Get total number of items in database."""
        with self.conn.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {SCHEMA}.raw_auction_items")
            return cursor.fetchone()[0]

    def get_run_stats(self, run_id: int) -> Optional[dict]:
        """Get statistics for a scrape run."""
        with self.conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(f"SELECT * FROM {SCHEMA}.scrape_runs WHERE id = %s", (run_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
