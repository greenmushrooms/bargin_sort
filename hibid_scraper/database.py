"""
Database management for HiBid Scraper.

Stores raw JSON payloads in SQLite (PostgreSQL-compatible schema).
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Any

from config import Config

logger = logging.getLogger(__name__)


# =============================================================================
# DATABASE BACKEND SWAP INSTRUCTIONS
# =============================================================================
# To switch from SQLite to PostgreSQL:
#
# 1. Install psycopg2: uv add psycopg2-binary
#    Or: uv sync --extra postgres
#
# 2. Update DATABASE_URL in .env:
#    DATABASE_URL=postgresql://user:password@localhost:5432/hibid_auctions
#
# 3. Replace the Database class below with PostgresDatabase class
#    (uncomment the PostgreSQL implementation at the bottom of this file)
#
# 4. The schema is already PostgreSQL-compatible:
#    - Uses TEXT for JSON (JSONB in PostgreSQL for better performance)
#    - Uses INTEGER PRIMARY KEY (SERIAL in PostgreSQL)
#    - Index syntax is compatible
# =============================================================================


class Database:
    """SQLite database handler for storing raw auction JSON payloads."""

    def __init__(self, config: Config):
        self.config = config
        self.db_path = config.get_sqlite_path()
        self.conn: Optional[sqlite3.Connection] = None

    def connect(self) -> None:
        """Establish database connection."""
        logger.info(f"Connecting to SQLite database: {self.db_path}")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        """Close database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None
            logger.info("Database connection closed")

    def _init_schema(self) -> None:
        """Initialize database schema."""
        cursor = self.conn.cursor()

        # Main table for raw auction item payloads
        # Schema designed for PostgreSQL compatibility:
        # - item_id: Use VARCHAR in PostgreSQL (TEXT works in both)
        # - raw_json: Use JSONB in PostgreSQL for indexing/querying
        # - timestamps: Use TIMESTAMP WITH TIME ZONE in PostgreSQL
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS auction_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL UNIQUE,
                raw_json TEXT NOT NULL,
                scraped_at TEXT NOT NULL,
                zip_code TEXT NOT NULL,
                radius_miles INTEGER NOT NULL,
                category TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Indexes for common queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_item_id ON auction_items(item_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_scraped_at ON auction_items(scraped_at)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_zip_code ON auction_items(zip_code)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_category ON auction_items(category)
        """)

        # Table to track scrape runs for auditing
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scrape_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                zip_code TEXT NOT NULL,
                radius_miles INTEGER NOT NULL,
                test_mode INTEGER NOT NULL,
                items_found INTEGER DEFAULT 0,
                items_added INTEGER DEFAULT 0,
                items_updated INTEGER DEFAULT 0,
                errors INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running'
            )
        """)

        self.conn.commit()
        logger.info("Database schema initialized")

    def start_scrape_run(self, zip_code: str, radius_miles: int, test_mode: bool) -> int:
        """Record the start of a scrape run. Returns run ID."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO scrape_runs (started_at, zip_code, radius_miles, test_mode)
            VALUES (?, ?, ?, ?)
            """,
            (datetime.now(timezone.utc).isoformat(), zip_code, radius_miles, int(test_mode)),
        )
        self.conn.commit()
        return cursor.lastrowid

    def complete_scrape_run(
        self,
        run_id: int,
        items_found: int,
        items_added: int,
        items_updated: int,
        errors: int,
        status: str = "completed",
    ) -> None:
        """Record the completion of a scrape run."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE scrape_runs
            SET completed_at = ?, items_found = ?, items_added = ?,
                items_updated = ?, errors = ?, status = ?
            WHERE id = ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                items_found,
                items_added,
                items_updated,
                errors,
                status,
                run_id,
            ),
        )
        self.conn.commit()

    def upsert_item(
        self,
        item_id: str,
        raw_json: dict,
        zip_code: str,
        radius_miles: int,
        category: Optional[str] = None,
    ) -> tuple[bool, bool]:
        """
        Insert or update an auction item.

        Returns: (is_new, is_updated) tuple
        """
        cursor = self.conn.cursor()
        scraped_at = datetime.now(timezone.utc).isoformat()
        json_str = json.dumps(raw_json)

        # Check if item exists
        cursor.execute("SELECT id, raw_json FROM auction_items WHERE item_id = ?", (item_id,))
        existing = cursor.fetchone()

        if existing:
            # Update if JSON changed
            if existing["raw_json"] != json_str:
                cursor.execute(
                    """
                    UPDATE auction_items
                    SET raw_json = ?, scraped_at = ?, zip_code = ?,
                        radius_miles = ?, category = ?
                    WHERE item_id = ?
                    """,
                    (json_str, scraped_at, zip_code, radius_miles, category, item_id),
                )
                self.conn.commit()
                return (False, True)  # Not new, but updated
            return (False, False)  # Not new, not updated
        else:
            # Insert new item
            cursor.execute(
                """
                INSERT INTO auction_items
                (item_id, raw_json, scraped_at, zip_code, radius_miles, category)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (item_id, json_str, scraped_at, zip_code, radius_miles, category),
            )
            self.conn.commit()
            return (True, False)  # New item

    def get_item(self, item_id: str) -> Optional[dict]:
        """Retrieve an item by ID."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM auction_items WHERE item_id = ?", (item_id,)
        )
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None

    def get_recent_items(self, limit: int = 100) -> list[dict]:
        """Get most recently scraped items."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT * FROM auction_items
            ORDER BY scraped_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_item_count(self) -> int:
        """Get total number of items in database."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM auction_items")
        return cursor.fetchone()["count"]

    def get_run_stats(self, run_id: int) -> Optional[dict]:
        """Get statistics for a scrape run."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM scrape_runs WHERE id = ?", (run_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None


# =============================================================================
# POSTGRESQL IMPLEMENTATION (uncomment when ready to switch)
# =============================================================================
#
# import psycopg2
# from psycopg2.extras import RealDictCursor, Json
#
# class PostgresDatabase:
#     """PostgreSQL database handler for storing raw auction JSON payloads."""
#
#     def __init__(self, config: Config):
#         self.config = config
#         self.conn = None
#
#     def connect(self) -> None:
#         """Establish database connection."""
#         logger.info(f"Connecting to PostgreSQL database")
#         self.conn = psycopg2.connect(self.config.database_url)
#         self._init_schema()
#
#     def close(self) -> None:
#         """Close database connection."""
#         if self.conn:
#             self.conn.close()
#             self.conn = None
#             logger.info("Database connection closed")
#
#     def _init_schema(self) -> None:
#         """Initialize database schema."""
#         with self.conn.cursor() as cursor:
#             cursor.execute("""
#                 CREATE TABLE IF NOT EXISTS auction_items (
#                     id SERIAL PRIMARY KEY,
#                     item_id VARCHAR(255) NOT NULL UNIQUE,
#                     raw_json JSONB NOT NULL,
#                     scraped_at TIMESTAMP WITH TIME ZONE NOT NULL,
#                     zip_code VARCHAR(10) NOT NULL,
#                     radius_miles INTEGER NOT NULL,
#                     category VARCHAR(255),
#                     created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
#                 )
#             """)
#             cursor.execute("""
#                 CREATE INDEX IF NOT EXISTS idx_item_id ON auction_items(item_id)
#             """)
#             cursor.execute("""
#                 CREATE INDEX IF NOT EXISTS idx_scraped_at ON auction_items(scraped_at)
#             """)
#             cursor.execute("""
#                 CREATE INDEX IF NOT EXISTS idx_zip_code ON auction_items(zip_code)
#             """)
#             cursor.execute("""
#                 CREATE INDEX IF NOT EXISTS idx_category ON auction_items(category)
#             """)
#             # PostgreSQL-specific: GIN index for JSONB queries
#             cursor.execute("""
#                 CREATE INDEX IF NOT EXISTS idx_raw_json_gin
#                 ON auction_items USING GIN (raw_json)
#             """)
#
#             cursor.execute("""
#                 CREATE TABLE IF NOT EXISTS scrape_runs (
#                     id SERIAL PRIMARY KEY,
#                     started_at TIMESTAMP WITH TIME ZONE NOT NULL,
#                     completed_at TIMESTAMP WITH TIME ZONE,
#                     zip_code VARCHAR(10) NOT NULL,
#                     radius_miles INTEGER NOT NULL,
#                     test_mode BOOLEAN NOT NULL,
#                     items_found INTEGER DEFAULT 0,
#                     items_added INTEGER DEFAULT 0,
#                     items_updated INTEGER DEFAULT 0,
#                     errors INTEGER DEFAULT 0,
#                     status VARCHAR(50) DEFAULT 'running'
#                 )
#             """)
#         self.conn.commit()
#         logger.info("Database schema initialized")
#
#     # ... (implement remaining methods with psycopg2 syntax)
#     # Key differences from SQLite:
#     # - Use %s instead of ? for placeholders
#     # - Use Json() wrapper for JSONB columns
#     # - Use RealDictCursor for dict-like row access
