#!/usr/bin/env python3
"""
HiBid Auction Scraper — Prefect Flow

Scrapes auction items from HiBid and stores raw JSON payloads in PostgreSQL.
"""

import logging
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from prefect import flow, runtime, task

from auction_discovery import AuctionDiscoveryScraper
from config import Config
from database import Database
from police_scraper import PoliceAuctionsScraper
from scraper import HiBidScraper
from transform import find_dbt_project, run_dbt


def setup_logging(level: str = "INFO") -> None:
    """Configure logging for the scraper."""
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format=log_format,
        handlers=[logging.StreamHandler(sys.stdout)],
    )


# Every scraper yields (item_id, payload, category) and shares the fetch layer,
# so one task drives any of them.
SCRAPERS = {
    "hibid": HiBidScraper,
    "hibid_auctions": AuctionDiscoveryScraper,
    "police_auctions": PoliceAuctionsScraper,
}

# Sources that search by radius from a postal code. The rest either sweep a
# single warehouse or are scoped to one auction.
RADIUS_SOURCES = ("hibid", "hibid_auctions")

# How much of an auction's advertised lot count a catalogue scrape has to land
# before it counts as a capture. Not 1.0: sellers pull lots between discovery
# and the scrape, and a run that legitimately finds 891 of 900 is complete.
MIN_CATALOG_COMPLETENESS = 0.9


@task(name="scrape_source")
def scrape_source(config: Config, sys_run_name: str, source: str = "hibid") -> dict:
    """Scrape a source and store its raw payloads."""
    logger = logging.getLogger(__name__)

    # A catalogue scrape is scoped by auction id and needs no origin; only a
    # radius search does.
    if source == "hibid" and not (config.zip_code or config.auction_id):
        raise ValueError(
            "zip_code is required — pass it as a flow parameter or set ZIP_CODE"
        )
    if source == "hibid_auctions" and not config.zip_code:
        raise ValueError("zip_code is required for auction discovery")

    db = Database(config, source=source)
    scraper = SCRAPERS[source](config)

    start_time = datetime.now(timezone.utc)
    run_id = None

    try:
        db.connect()

        # Resume a large catalogue where the last run stopped rather than
        # re-reading its front pages.
        if config.auction_id:
            resume_from = db.get_catalog_progress(int(config.auction_id))
            config.catalog_start_page = max(resume_from + 1, 1)
            if resume_from:
                logger.info(
                    f"Auction {config.auction_id}: resuming at page "
                    f"{config.catalog_start_page} (last good page {resume_from})"
                )

        # zip_code and radius_miles are not the same kind of fact, and a
        # catalogue scrape is where that shows. The radius describes how the
        # search was run, so it is meaningless here. The postal code describes
        # who it was run for, and silver measures every lot's distance from it —
        # so a catalogue run that omitted it would land lots that can never be
        # placed, and fct_lots drops those.
        is_radius = source in RADIUS_SOURCES and not config.auction_id
        run_id = db.start_scrape_run(
            config.zip_code if source in RADIUS_SOURCES else None,
            config.radius_miles if is_radius else None,
            config.test_mode,
            sys_run_name,
            auction_id=int(config.auction_id) if config.auction_id else None,
        )
        logger.info(f"Started scrape run #{run_id}")

        items_inserted = 0

        for item_id, raw_json, category in scraper.scrape_all():
            db.insert_item(
                item_id=item_id,
                raw_json=raw_json,
                category=category,
                sys_run_name=sys_run_name,
            )
            items_inserted += 1

        # Get buffered rows on disk before the run is marked complete, so
        # items_inserted always reflects what is actually in raw.
        db.flush()

        scraper_stats = scraper.get_stats()

        # Progress is banked before anything can fail, because a partial pass
        # still moved the catalogue forward and that is precisely what used to
        # be thrown away.
        if config.auction_id and not config.test_mode:
            db.save_catalog_progress(
                int(config.auction_id), scraper_stats.last_page, items_inserted
            )

        # Only a run that came back with nothing is worth failing now. Fetch
        # errors no longer condemn the whole pass: the pages that did land are
        # real lots, and the resume point means the next run continues rather
        # than repeating them.
        if config.auction_id and not config.test_mode:
            if scraper_stats.errors and not items_inserted:
                raise RuntimeError(
                    f"Auction {config.auction_id}: {scraper_stats.errors} fetch "
                    f"errors and no lots landed"
                )
            if not items_inserted:
                raise RuntimeError(
                    f"Auction {config.auction_id}: catalogue returned no lots"
                )

            # A short catalogue is no longer a failure. Whether the auction is
            # captured is now a question about how much of it we hold in total
            # (auction_manifest counts distinct lots across every run), not
            # about whether one pass got it all. Failing here instead threw
            # away partial progress and sent large auctions back to page 1
            # forever — a 4,296-lot catalogue sat at 28% after three runs.
            expected = config.auction_lot_count or 0
            if expected and items_inserted < expected * MIN_CATALOG_COMPLETENESS:
                logger.info(
                    f"Auction {config.auction_id}: partial pass, "
                    f"{items_inserted} of {expected} lots — progress saved at "
                    f"page {scraper_stats.last_page} for the next run"
                )

        db.complete_scrape_run(
            run_id=run_id,
            items_found=scraper_stats.items_found,
            items_inserted=items_inserted,
            errors=scraper_stats.errors,
            status="completed",
        )

        end_time = datetime.now(timezone.utc)
        duration = (end_time - start_time).total_seconds()

        result = {
            "run_id": run_id,
            "status": "completed",
            "duration_seconds": duration,
            "items_found": scraper_stats.items_found,
            "items_inserted": items_inserted,
            "errors": scraper_stats.errors,
        }

        logger.info(f"Scrape completed: {result}")
        return result

    except Exception as e:
        logger.exception(f"Scrape failed: {e}")
        if run_id:
            scraper_stats = scraper.get_stats()
            if config.auction_id and not config.test_mode:
                try:
                    db.flush()
                    db.save_catalog_progress(
                        int(config.auction_id), scraper_stats.last_page, 0
                    )
                except psycopg2.Error as save_error:
                    logger.error(f"Could not save catalogue progress: {save_error}")
            db.complete_scrape_run(
                run_id=run_id,
                items_found=scraper_stats.items_found,
                items_inserted=0,
                errors=scraper_stats.errors + 1,
                status="failed",
            )
        raise

    finally:
        scraper.close()
        db.close()


@task(name="build_silver")
def build_silver(
    config: Config,
    sys_run_name: Optional[str] = None,
    select: Optional[str] = None,
) -> dict:
    """
    Run dbt over what was just scraped.

    `sys_run_name` scopes the incremental models to a single run, which is what
    keeps a transform proportional to the new data. Leaving it None selects
    dbt's catch-up mode instead — every run not already in silver — which is
    what the orchestrator needs, since it produces one run per auction and
    transforming them one at a time would rebuild the downstream tables
    repeatedly for no gain.
    """
    logger = logging.getLogger(__name__)

    project_dir = find_dbt_project(config.dbt_project_dir)
    logger.info(f"Building silver from dbt project at {project_dir}")

    return run_dbt(
        project_dir, command="run", target_run=sys_run_name, select=select
    )


@flow(name="scrape_auctions")
def scrape_auctions(
    zip_code: str = "m8w3b7",
    radius_miles: int = 31,
    categories: str = "",
    test_mode: bool = False,
    test_limit: int = 20,
    build_downstream: bool = True,
) -> dict:
    """
    Sweep open lots by radius, on demand.

    Superseded for scheduled work by discover_auctions + scrape_closing_auctions,
    which cover the same ground once per auction instead of once per day, and
    without the blind spot: a radius search returns only lots open at that
    instant, so auctions mid-close come back partial and some never appear at
    all. Kept for finding lots without knowing the auction — the search-term
    work in TODO #6 builds on this path.
    """
    setup_logging()
    logger = logging.getLogger(__name__)

    search_categories = [c.strip() for c in categories.split(",") if c.strip()]

    config = Config.from_env()
    config.zip_code = zip_code
    config.radius_miles = radius_miles
    config.search_categories = search_categories
    config.test_mode = test_mode
    config.test_limit = test_limit
    # Radius search, never a catalogue — a stray AUCTION_ID in the environment
    # would otherwise silently narrow the sweep to one auction.
    config.auction_id = ""

    sys_run_name = runtime.flow_run.name
    logger.info(
        f"Starting scrape_auctions flow: run={sys_run_name}, zip={zip_code}, "
        f"radius={radius_miles}, test_mode={test_mode}"
    )

    result = scrape_source(config, sys_run_name=sys_run_name, source="hibid")

    if not build_downstream:
        logger.info("Skipping silver build (build_downstream=False)")
        return result

    # Bronze is already durable at this point, so a transform failure costs the
    # freshness of silver but never the scrape itself. It still fails the flow,
    # because silently stale silver is worse than a visible red run.
    result["transform"] = build_silver(config, sys_run_name=sys_run_name)
    return result


@flow(name="discover_auctions")
def discover_auctions(
    zip_code: str = "m8w3b7",
    radius_miles: int = 31,
    horizon_days: int = 14,
    test_mode: bool = False,
    test_limit: int = 20,
    build_downstream: bool = True,
) -> dict:
    """
    List the auctions near home and when they close.

    One request per page, and page one alone reaches about six days ahead, so
    this is the cheap half of the pipeline: it decides what the expensive half
    does without reading a single lot.

    31 miles is 49.9 km — as close to the 50 km question as HiBid's whole-mile
    radius can be asked. It honours it loosely and publishes no distance, so
    `auction_schedule` applies the real cutoff against postal centroids.
    """
    setup_logging()
    logger = logging.getLogger(__name__)

    config = Config.from_env()
    config.zip_code = zip_code
    config.radius_miles = radius_miles
    config.discovery_horizon_days = horizon_days
    config.test_mode = test_mode
    config.test_limit = test_limit
    config.auction_id = ""

    sys_run_name = runtime.flow_run.name
    logger.info(
        f"Starting discover_auctions flow: run={sys_run_name}, zip={zip_code}, "
        f"radius={radius_miles}mi, horizon={horizon_days}d"
    )

    result = scrape_source(config, sys_run_name=sys_run_name, source="hibid_auctions")

    if not build_downstream:
        logger.info("Skipping silver build (build_downstream=False)")
        return result

    # Only the auction models: discovery says nothing about lots, and the
    # orchestrator reads auction_schedule immediately after this returns.
    result["transform"] = build_silver(config, select="stg_hibid_auctions+")
    return result


@task(name="select_auctions_to_scrape")
def select_auctions_to_scrape(
    config: Config, days_ahead: int, max_km: float
) -> list[dict]:
    """
    In-radius auctions closing soon that have not been captured yet.

    What counts as "not captured" is not decided here — it is
    `silver_enhanced.auction_manifest`, which reads raw.scrape_runs and is the
    single written form of the rule that an auction is scraped once. Restating
    the condition in this query is how the ledger and the scheduler would come
    to disagree, so this only picks a window out of it.

    That rule is what caps the cost: an auction stays open for a median of four
    days, so without it a daily run would re-read the same catalogue four times.

    The window runs from now rather than from tomorrow on purpose. Auctions
    closing today are normally picked up by yesterday's run; if that run was
    missed they are still here, and a late capture beats none at all.
    """
    logger = logging.getLogger(__name__)

    conn = psycopg2.connect(
        host=config.db_host,
        port=config.db_port,
        user=config.db_user,
        password=config.db_password,
        dbname=config.db_name,
    )
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT m.auction_id,
                       m.event_name,
                       m.event_city,
                       m.lot_count,
                       m.distance_km,
                       m.close_at,
                       m.scrape_state,
                       -- Carried along so the scrape can inline it on lots
                       -- when the catalogue page renders no auction of its own.
                       payload.raw_json AS auction_payload
                FROM silver_enhanced.auction_manifest m
                LEFT JOIN LATERAL (
                    SELECT ha.raw_json
                    FROM raw.hibid_auctions ha
                    WHERE ha.item_id = m.auction_id::text
                    ORDER BY ha.scraped_at DESC
                    LIMIT 1
                ) payload ON true
                -- 'retry' is a previous attempt that failed, so it is still
                -- owed a capture and rides the same window as a fresh one.
                WHERE m.scrape_state IN ('pending', 'retry')
                  AND m.distance_km <= %(max_km)s
                  AND m.close_at <= now() + make_interval(days => %(days_ahead)s)
                ORDER BY m.close_at
                """,
                {"max_km": max_km, "days_ahead": days_ahead},
            )
            rows = [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()

    total_lots = sum(r["lot_count"] or 0 for r in rows)
    logger.info(
        f"{len(rows)} auctions to scrape (~{total_lots} lots) "
        f"closing within {days_ahead}d and {max_km}km"
    )
    for r in rows:
        logger.info(
            f"  [{r['scrape_state']}] {r['auction_id']} {r['event_name'][:40]!r} "
            f"{r['lot_count']} lots, {r['distance_km']}km, closes {r['close_at']}"
        )
    return rows


@flow(name="scrape_closing_auctions")
def scrape_closing_auctions(
    zip_code: str = "m8w3b7",
    days_ahead: int = 2,
    max_km: float = 50.0,
    auction_delay_seconds: int = 20,
    test_mode: bool = False,
    test_limit: int = 20,
    build_downstream: bool = True,
) -> dict:
    """
    Scrape every in-radius auction closing soon, once each.

    Visiting an auction's own catalogue rather than a radius search is what
    makes the capture complete: a radius search only returns lots that are open
    at that instant, so an auction part-way through its staggered close reads as
    a fraction of its true size.

    One scrape run per auction, so a failure costs that auction and not the
    day. Silver is built once at the end over all of them.
    """
    setup_logging()
    logger = logging.getLogger(__name__)

    config = Config.from_env()
    config.test_mode = test_mode
    config.test_limit = test_limit

    base_run_name = runtime.flow_run.name
    logger.info(
        f"Starting scrape_closing_auctions flow: run={base_run_name}, "
        f"days_ahead={days_ahead}, max_km={max_km}"
    )

    targets = select_auctions_to_scrape(config, days_ahead=days_ahead, max_km=max_km)

    scraped, failed = [], []

    for index, target in enumerate(targets):
        auction_id = target["auction_id"]

        # The scraper pauses between pages but nothing paused between
        # catalogues, so a sweep went straight from finishing one auction into
        # the next and HiBid started degrading responses — well-formed pages
        # holding fewer lots than the auction has. That is the throttling
        # signature, and it cost 9 of 11 failures on the first full sweep.
        if index:
            logger.info(f"Pausing {auction_delay_seconds}s before the next catalogue")
            time.sleep(auction_delay_seconds)

        # raw.scrape_runs is unique on (source, sys_run_name), and one flow run
        # produces many scrapes — so the auction id is what makes each run row
        # distinct, and keeps it traceable back to the flow run that made it.
        auction_config = Config.from_env()
        auction_config.test_mode = test_mode
        auction_config.test_limit = test_limit
        auction_config.auction_id = str(auction_id)
        auction_config.auction_payload = target.get("auction_payload")
        auction_config.auction_lot_count = target.get("lot_count")
        # Not used to search — the catalogue is already the scope — but it is
        # the origin silver measures every lot's distance from.
        auction_config.zip_code = zip_code

        try:
            result = scrape_source(
                auction_config,
                sys_run_name=f"{base_run_name}-{auction_id}",
                source="hibid",
            )
            scraped.append({"auction_id": auction_id, **result})
        except Exception as e:
            # One bad catalogue must not cost the rest of the day's auctions;
            # it stays uncaptured and the next run will retry it, because the
            # selection query only skips auctions with a *completed* run.
            logger.error(f"Auction {auction_id} failed: {e}")
            failed.append({"auction_id": auction_id, "error": str(e)})

    summary = {
        "auctions_selected": len(targets),
        "auctions_scraped": len(scraped),
        "auctions_failed": len(failed),
        "items_inserted": sum(s.get("items_inserted", 0) for s in scraped),
        "failures": failed,
    }
    logger.info(f"Auction sweep complete: {summary}")

    if not build_downstream:
        logger.info("Skipping silver build (build_downstream=False)")
        return summary

    if scraped:
        # No target_run: this flow produced one run per auction, so dbt's
        # catch-up mode picks all of them up in a single pass.
        summary["transform"] = build_silver(config)

    return summary


@flow(name="scrape_police_auctions")
def scrape_police_auctions(
    test_mode: bool = False,
    test_limit: int = 20,
    build_downstream: bool = True,
) -> dict:
    """Scrape Police Auctions Canada into raw, then rebuild silver for the run."""
    setup_logging()
    logger = logging.getLogger(__name__)

    config = Config.from_env()
    config.test_mode = test_mode
    config.test_limit = test_limit

    sys_run_name = runtime.flow_run.name
    logger.info(f"Starting scrape_police_auctions flow: run={sys_run_name}")

    result = scrape_source(config, sys_run_name=sys_run_name, source="police_auctions")

    if not build_downstream:
        logger.info("Skipping silver build (build_downstream=False)")
        return result

    result["transform"] = build_silver(config, sys_run_name=sys_run_name)
    return result


if __name__ == "__main__":
    scrape_auctions()
