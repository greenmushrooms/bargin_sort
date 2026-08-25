"""
Connection pooling for the site's reads and writes.

A pool rather than a connection per request because the refresh handler holds
one for the length of a browser fetch — seconds, not milliseconds — and a
single shared connection would serialise the whole site behind it.

Separate from the scraper's `Database`: that class exists to write bronze
correctly (partitions, run rows, batching) and is used only for that, on the
refresh path. Everything else here is ordinary reads and small upserts.
"""

import logging
import threading
from contextlib import contextmanager
from pathlib import Path

from psycopg2 import extensions, pool
from psycopg2.extras import RealDictCursor

from settings import scraper_config

logger = logging.getLogger(__name__)

SQL_DIR = Path(__file__).resolve().parent / "sql"

# Applied at startup, in order. Every file is written to be re-runnable.
MIGRATIONS = (
    "001_web_schema.sql",
    "002_wishlist_match.sql",
    "003_brand_tier.sql",
    "004_lot_event.sql",
)

_pool: pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def init_pool() -> None:
    """Open the pool and apply the web schema. Safe to call twice."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            return
        config = scraper_config()
        _pool = pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=8,
            host=config.db_host,
            port=config.db_port,
            user=config.db_user,
            password=config.db_password,
            dbname=config.db_name,
        )
    _migrate()


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None


@contextmanager
def connection():
    """Borrow a connection, returning it to the pool no matter what."""
    if _pool is None:
        raise RuntimeError("connection pool not initialised; call init_pool() first")
    conn = _pool.getconn()
    try:
        yield conn
    finally:
        # Roll back anything a failed handler left open. Returning a connection
        # mid-transaction poisons the next borrower with locks it never took —
        # and psycopg2 opens one on the first execute whether or not anything
        # is written, so a plain SELECT leaves a transaction behind too.
        if conn.get_transaction_status() != extensions.TRANSACTION_STATUS_IDLE:
            conn.rollback()
        _pool.putconn(conn)


@contextmanager
def cursor(commit: bool = False):
    """A dict cursor on a pooled connection."""
    with connection() as conn:
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                yield cur
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise


def query(sql: str, params: tuple = ()) -> list[dict]:
    with cursor() as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def query_one(sql: str, params: tuple = ()) -> dict | None:
    with cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None


def mutate_one(sql: str, params: tuple = ()) -> dict | None:
    """
    A write that returns a row — an upsert with RETURNING.

    Separate from `query_one` rather than a `commit=` flag on it, because the
    two are told apart by eye at the call site and the failure mode of getting
    it wrong is silent: an INSERT ... RETURNING through the read helper hands
    back the new row, looks entirely correct, and is rolled back when the
    connection goes home.
    """
    with cursor(commit=True) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None


def execute(sql: str, params: tuple = ()) -> int:
    with cursor(commit=True) as cur:
        cur.execute(sql, params)
        return cur.rowcount


def _migrate() -> None:
    """
    Apply the web schema DDL.

    At startup on every boot, the same way the scraper applies its own raw DDL
    on connect. The files are idempotent, the schema is three tables, and the
    alternative — remembering to run a migration step after a git pull — is the
    thing that makes a local site annoying enough to stop using.
    """
    with cursor(commit=True) as cur:
        for name in MIGRATIONS:
            cur.execute((SQL_DIR / name).read_text())
    logger.info("web schema ready (%d migration file(s))", len(MIGRATIONS))
