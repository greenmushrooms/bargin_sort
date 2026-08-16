#!/usr/bin/env python3
"""
HiBid Auction Scraper — Prefect Flow

Scrapes auction items from HiBid and stores raw JSON payloads in PostgreSQL.
"""

import logging
import sys
from datetime import datetime, timezone

from prefect import flow, runtime, task

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


# Both scrapers yield (item_id, payload, category) and share the fetch layer,
# so one task drives either.
SCRAPERS = {
    "hibid": HiBidScraper,
    "police_auctions": PoliceAuctionsScraper,
}


@task(name="scrape_source")
def scrape_source(config: Config, sys_run_name: str, source: str = "hibid") -> dict:
    """Scrape a source and store its raw payloads."""
    logger = logging.getLogger(__name__)

    if source == "hibid" and not config.zip_code:
        raise ValueError(
            "zip_code is required — pass it as a flow parameter or set ZIP_CODE"
        )

    db = Database(config, source=source)
    scraper = SCRAPERS[source](config)

    start_time = datetime.now(timezone.utc)
    run_id = None

    try:
        db.connect()

        # Radius search is a HiBid concept; a single-warehouse source records
        # neither, which is why the columns are nullable on the run table.
        run_id = db.start_scrape_run(
            config.zip_code if source == "hibid" else None,
            config.radius_miles if source == "hibid" else None,
            config.test_mode,
            sys_run_name,
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
def build_silver(config: Config, sys_run_name: str) -> dict:
    """Run dbt for just the run that was scraped."""
    logger = logging.getLogger(__name__)

    project_dir = find_dbt_project(config.dbt_project_dir)
    logger.info(f"Building silver from dbt project at {project_dir}")

    return run_dbt(project_dir, command="run", target_run=sys_run_name)


@flow(name="scrape_auctions")
def scrape_auctions(
    zip_code: str = "m8w3b7",
    radius_miles: int = 50,
    categories: str = "",
    test_mode: bool = False,
    test_limit: int = 20,
    build_downstream: bool = True,
) -> dict:
    """Scrape HiBid into raw, then rebuild silver for that run."""
    setup_logging()
    logger = logging.getLogger(__name__)

    search_categories = [c.strip() for c in categories.split(",") if c.strip()]

    config = Config.from_env()
    config.zip_code = zip_code
    config.radius_miles = radius_miles
    config.search_categories = search_categories
    config.test_mode = test_mode
    config.test_limit = test_limit

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
