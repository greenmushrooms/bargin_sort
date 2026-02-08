#!/usr/bin/env python3
"""
HiBid Auction Scraper - Main Entry Point

Scrapes auction items from HiBid and stores raw JSON payloads in SQLite.
Designed to be run on-demand and easily integrated into orchestration systems.

Usage:
    python main.py                    # Run with .env configuration
    python main.py --test             # Run in test mode (20 items)
    python main.py --zip 12345        # Override zip code
    python main.py --radius 100       # Override search radius

Examples:
    # Test mode with specific zip code
    python main.py --test --zip 90210

    # Full scrape with custom radius
    python main.py --zip 78414 --radius 100

    # Scrape specific categories
    python main.py --categories "cars,trucks,coins---currency"
"""

import argparse
import logging
import sys
from datetime import datetime, timezone

from config import Config
from database import Database
from scraper import HiBidScraper


def setup_logging(level: str = "INFO") -> None:
    """Configure logging for the scraper."""
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("scraper.log", mode="a"),
        ],
    )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Scrape HiBid auctions and store raw JSON in SQLite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--zip",
        type=str,
        help="Override zip code from environment",
    )
    parser.add_argument(
        "--radius",
        type=int,
        choices=[10, 25, 50, 100, 250, 500],
        help="Override search radius in miles",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Enable test mode (limit to 20 items)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Number of items to scrape in test mode (default: 20)",
    )
    parser.add_argument(
        "--categories",
        type=str,
        help="Comma-separated list of categories to scrape",
    )
    parser.add_argument(
        "--db",
        type=str,
        help="Override database URL",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Set logging level",
    )

    return parser.parse_args()


def print_summary(
    run_id: int,
    stats: dict,
    start_time: datetime,
    end_time: datetime,
    config: Config,
) -> None:
    """Print scrape run summary."""
    duration = (end_time - start_time).total_seconds()

    print("\n" + "=" * 60)
    print("SCRAPE RUN SUMMARY")
    print("=" * 60)
    print(f"Run ID:          {run_id}")
    print(f"Status:          {stats.get('status', 'unknown')}")
    print(f"Duration:        {duration:.2f} seconds")
    print("-" * 60)
    print(f"Zip Code:        {config.zip_code}")
    print(f"Radius:          {config.radius_miles} miles")
    print(f"Test Mode:       {'Yes' if config.test_mode else 'No'}")
    print(f"Categories:      {', '.join(config.search_categories) or 'all'}")
    print("-" * 60)
    print(f"Items Found:     {stats.get('items_found', 0)}")
    print(f"Items Added:     {stats.get('items_added', 0)}")
    print(f"Items Updated:   {stats.get('items_updated', 0)}")
    print(f"Errors:          {stats.get('errors', 0)}")
    print("=" * 60)


def main() -> int:
    """Main entry point."""
    args = parse_args()

    # Load base configuration from environment
    try:
        config = Config.from_env()
    except ValueError as e:
        # If ZIP_CODE not in env and not provided via CLI
        if "--zip" not in sys.argv and args.zip is None:
            print(f"Error: {e}")
            print("Set ZIP_CODE in .env or provide --zip argument")
            return 1
        # Create minimal config that will be overridden
        config = Config(zip_code=args.zip or "00000")

    # Apply command line overrides
    if args.zip:
        config.zip_code = args.zip
    if args.radius:
        config.radius_miles = args.radius
    if args.test:
        config.test_mode = True
        config.test_limit = args.limit
    if args.categories:
        config.search_categories = [c.strip() for c in args.categories.split(",")]
    if args.db:
        config.database_url = args.db
    if args.log_level:
        config.log_level = args.log_level

    # Setup logging
    setup_logging(config.log_level)
    logger = logging.getLogger(__name__)

    logger.info("Starting HiBid scraper")
    logger.info(f"Configuration: zip={config.zip_code}, radius={config.radius_miles}, test_mode={config.test_mode}")

    # Initialize components
    db = Database(config)
    scraper = HiBidScraper(config)

    start_time = datetime.now(timezone.utc)
    run_id = None

    try:
        # Connect to database
        db.connect()

        # Start scrape run tracking
        run_id = db.start_scrape_run(
            config.zip_code, config.radius_miles, config.test_mode
        )
        logger.info(f"Started scrape run #{run_id}")

        # Scrape items
        items_added = 0
        items_updated = 0

        for item_id, raw_json, category in scraper.scrape_all():
            is_new, is_updated = db.upsert_item(
                item_id=item_id,
                raw_json=raw_json,
                zip_code=config.zip_code,
                radius_miles=config.radius_miles,
                category=category,
            )

            if is_new:
                items_added += 1
                logger.debug(f"Added new item: {item_id}")
            elif is_updated:
                items_updated += 1
                logger.debug(f"Updated item: {item_id}")

        # Update statistics
        scraper_stats = scraper.get_stats()

        # Complete scrape run
        db.complete_scrape_run(
            run_id=run_id,
            items_found=scraper_stats.items_found,
            items_added=items_added,
            items_updated=items_updated,
            errors=scraper_stats.errors,
            status="completed",
        )

        end_time = datetime.now(timezone.utc)
        run_stats = db.get_run_stats(run_id)

        print_summary(run_id, run_stats, start_time, end_time, config)

        logger.info("Scrape completed successfully")
        return 0

    except KeyboardInterrupt:
        logger.warning("Scrape interrupted by user")
        if run_id:
            scraper_stats = scraper.get_stats()
            db.complete_scrape_run(
                run_id=run_id,
                items_found=scraper_stats.items_found,
                items_added=0,
                items_updated=0,
                errors=scraper_stats.errors,
                status="interrupted",
            )
        return 130

    except Exception as e:
        logger.exception(f"Scrape failed: {e}")
        if run_id:
            scraper_stats = scraper.get_stats()
            db.complete_scrape_run(
                run_id=run_id,
                items_found=scraper_stats.items_found,
                items_added=0,
                items_updated=0,
                errors=scraper_stats.errors + 1,
                status="failed",
            )
        return 1

    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
