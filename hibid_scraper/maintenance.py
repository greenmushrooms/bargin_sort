#!/usr/bin/env python3
"""
Raw-layer retention — Prefect flow.

Drops whole monthly partitions of each raw source table older than the
retention window, plus the matching scrape_runs rows.

Dropping a partition is a DROP TABLE, so cost does not scale with row count and
no VACUUM is left behind. Only partitions entirely older than the cutoff go, so
the current month is never truncated part-way through.

Silver is a projection of raw, so after history is dropped the downstream
models still describe lots whose raw rows are gone. Re-run dbt to bring them
back in line:

    dbt run --full-refresh --profiles-dir .
"""

import logging
import sys

import requests
from prefect import flow, task

from config import Config
from database import Database, SCHEMA

# Every landing table under raw that retention applies to.
SOURCE_TABLES = ("hibid", "police_auctions")

RETENTION_MONTHS = 6


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


@task(name="apply_raw_retention")
def apply_raw_retention(config: Config, months: int, dry_run: bool) -> dict:
    """Drop raw partitions older than `months`."""
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
            targets = []
            for table in SOURCE_TABLES:
                cursor.execute(
                    f"""
                    SELECT c.relname
                    FROM pg_class c
                    JOIN pg_inherits i ON i.inhrelid = c.oid
                    JOIN pg_class p    ON p.oid = i.inhparent
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = %s
                      AND p.relname = %s
                      AND c.relname ~ ('^' || %s || '_[0-9]{{4}}_[0-9]{{2}}$')
                      AND to_date(right(c.relname, 7), 'YYYY_MM')
                          < date_trunc('month', %s::date)
                    ORDER BY c.relname
                    """,
                    (SCHEMA, table, table, cutoff),
                )
                targets.extend(row[0] for row in cursor.fetchall())

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

            dropped = []
            for table in SOURCE_TABLES:
                cursor.execute(
                    f"SELECT {SCHEMA}.drop_partitions_before(%s, %s)", (table, cutoff)
                )
                dropped.extend(row[0] for row in cursor.fetchall())

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


@task(name="reap_flaresolverr_sessions")
def reap_flaresolverr_sessions(config: Config, source: str, dry_run: bool) -> dict:
    """
    Destroy leftover FlareSolverr browser sessions belonging to `source`.

    A session is a live Chrome and FlareSolverr never reaps one on its own, so
    any session the scraper failed to destroy survives until the container is
    restarted. The fetcher no longer strands them, but orphans already banked
    outlive any number of clean runs, and Chrome stops starting entirely once
    enough pile up — the 2026-08-22 outage began at forty-two.

    Deliberately NOT a fetcher-startup sweep. sessions.list returns bare ids
    and nothing else: no age, no owner. Discovery fans out seven or eight
    concurrent catalogue scrapes that each hold a live `hibid-` session, and a
    prefix sweep at startup cannot tell those from orphans — it would kill its
    own siblings mid-page. Hence the guard below: reap only when this source
    has no scrape in flight, which is the one moment every `hibid-` session is
    known to be garbage.
    """
    logger = logging.getLogger(__name__)

    db = Database(config)
    try:
        db.connect()
        with db.conn.cursor() as cursor:
            cursor.execute(
                f"SELECT count(*) FROM {SCHEMA}.scrape_runs "
                "WHERE source = %s AND status = 'running'",
                (source,),
            )
            in_flight = cursor.fetchone()[0]
    finally:
        db.close()

    if in_flight:
        logger.warning(
            f"{in_flight} {source} scrape(s) still running — skipping reap, "
            "their sessions are indistinguishable from orphans"
        )
        return {"skipped": True, "in_flight": in_flight, "destroyed": []}

    timeout = (config.flaresolverr_timeout_ms / 1000) + 30
    listing = requests.post(
        config.flaresolverr_url, json={"cmd": "sessions.list"}, timeout=timeout
    )
    listing.raise_for_status()
    orphans = [
        s for s in listing.json().get("sessions", []) if s.startswith(f"{source}-")
    ]

    if dry_run:
        logger.info(f"Dry run — would destroy {len(orphans)}: {orphans or '(none)'}")
        return {"skipped": False, "in_flight": 0, "would_destroy": orphans}

    destroyed = []
    for session_id in orphans:
        try:
            requests.post(
                config.flaresolverr_url,
                json={"cmd": "sessions.destroy", "session": session_id},
                timeout=timeout,
            ).raise_for_status()
            destroyed.append(session_id)
        except requests.RequestException as e:
            logger.warning(f"Could not destroy {session_id}: {e}")

    logger.info(f"Reaped {len(destroyed)}/{len(orphans)} orphaned sessions")
    return {"skipped": False, "in_flight": 0, "destroyed": destroyed}


@flow(name="flaresolverr_reap")
def flaresolverr_reap(source: str = "hibid", dry_run: bool = False) -> dict:
    """Close FlareSolverr browser sessions left behind by earlier scrapes."""
    setup_logging()
    logging.getLogger(__name__).info(
        f"FlareSolverr reap: source={source}, dry_run={dry_run}"
    )
    return reap_flaresolverr_sessions(Config.from_env(), source=source, dry_run=dry_run)


@flow(name="raw_retention")
def raw_retention(months: int = RETENTION_MONTHS, dry_run: bool = False) -> dict:
    """Drop raw data older than `months`. Defaults to a 6 month window."""
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info(f"Raw retention: months={months}, dry_run={dry_run}")

    config = Config.from_env()
    result = apply_raw_retention(config, months=months, dry_run=dry_run)

    if result["dropped_partitions"]:
        logger.warning(
            "Raw history was dropped — run `dbt run --full-refresh` so "
            "silver stops reporting lots whose raw rows no longer exist"
        )
    return result


if __name__ == "__main__":
    raw_retention()
