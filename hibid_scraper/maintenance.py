#!/usr/bin/env python3
"""
Bronze retention — Prefect flow.

Drops whole monthly partitions of bronze.raw_auction_items older than the
retention window, plus the matching scrape_runs rows.

Dropping a partition is a DROP TABLE, so cost does not scale with row count and
no VACUUM is left behind. Only partitions entirely older than the cutoff go, so
the current month is never truncated part-way through.

Silver is a projection of bronze, so after history is dropped the downstream
models still describe lots whose raw rows are gone. Re-run dbt to bring them
back in line:

    dbt run --full-refresh --profiles-dir .
"""

import logging
import sys

from prefect import flow, task

from config import Config
from database import Database, SCHEMA

RETENTION_MONTHS = 6


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


@task(name="apply_bronze_retention")
def apply_bronze_retention(config: Config, months: int, dry_run: bool) -> dict:
    """Drop bronze partitions older than `months`."""
    logger = logging.getLogger(__name__)

    db = Database(config)
    try:
        db.connect()
        with db.conn.cursor() as cursor:
            cursor.execute(
                "SELECT (CURRENT_DATE - (%s || ' months')::interval)::date",
                (months,),
            )
            cutoff = cursor.fetchone()[0]

            # Ask which partitions would go before touching anything, so a dry
            # run and a real run report the same thing.
            cursor.execute(
                f"""
                SELECT c.relname
                FROM pg_class c
                JOIN pg_inherits i ON i.inhrelid = c.oid
                JOIN pg_class p    ON p.oid = i.inhparent
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s
                  AND p.relname = 'raw_auction_items'
                  AND c.relname ~ '^raw_auction_items_[0-9]{{4}}_[0-9]{{2}}$'
                  AND to_date(right(c.relname, 7), 'YYYY_MM') < date_trunc('month', %s::date)
                ORDER BY c.relname
                """,
                (SCHEMA, cutoff),
            )
            targets = [row[0] for row in cursor.fetchall()]

            if dry_run:
                db.conn.rollback()
                logger.info(f"Dry run — would drop {len(targets)}: {targets or '(none)'}")
                return {
                    "cutoff": str(cutoff),
                    "dropped_partitions": [],
                    "would_drop": targets,
                    "deleted_runs": 0,
                    "dry_run": True,
                }

            cursor.execute(f"SELECT {SCHEMA}.drop_partitions_before(%s)", (cutoff,))
            dropped = [row[0] for row in cursor.fetchall()]

            cursor.execute(f"SELECT {SCHEMA}.delete_runs_before(%s)", (cutoff,))
            deleted_runs = cursor.fetchone()[0]

        db.conn.commit()

        result = {
            "cutoff": str(cutoff),
            "dropped_partitions": dropped,
            "deleted_runs": deleted_runs,
            "dry_run": False,
        }
        logger.info(f"Retention applied: {result}")
        return result

    finally:
        db.close()


@flow(name="bronze_retention")
def bronze_retention(months: int = RETENTION_MONTHS, dry_run: bool = False) -> dict:
    """Drop bronze data older than `months`. Defaults to a 6 month window."""
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info(f"Bronze retention: months={months}, dry_run={dry_run}")

    config = Config.from_env()
    result = apply_bronze_retention(config, months=months, dry_run=dry_run)

    if result["dropped_partitions"]:
        logger.warning(
            "Bronze history was dropped — run `dbt run --full-refresh` so "
            "silver stops reporting lots whose raw rows no longer exist"
        )
    return result


if __name__ == "__main__":
    bronze_retention()
