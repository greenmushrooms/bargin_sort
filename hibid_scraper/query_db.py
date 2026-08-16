#!/usr/bin/env python3
"""
Database Query Utility for HiBid Scraper.

Simple CLI tool to view and query scraped auction data.

Usage:
    python query_db.py stats           # Show database statistics
    python query_db.py recent [N]      # Show N most recent items (default: 10)
    python query_db.py runs            # Show scrape run history
    python query_db.py item <item_id>  # Show full JSON for an item
    python query_db.py search <term>   # Search items by text
"""

import argparse
import json
import sys

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from config import Config

load_dotenv()


def connect_db(config: Config):
    """Connect to PostgreSQL database."""
    return psycopg2.connect(
        host=config.db_host,
        port=config.db_port,
        user=config.db_user,
        password=config.db_password,
        dbname=config.db_name,
    )


def cmd_stats(conn) -> None:
    """Show database statistics."""
    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute("SELECT COUNT(*) as count FROM bronze.raw_auction_items")
        item_count = cursor.fetchone()["count"]

        cursor.execute("SELECT COUNT(*) as count FROM bronze.scrape_runs")
        run_count = cursor.fetchone()["count"]

        cursor.execute("""
            SELECT MIN(scraped_at) as oldest, MAX(scraped_at) as newest
            FROM bronze.raw_auction_items
        """)
        dates = cursor.fetchone()

        cursor.execute("""
            SELECT category, COUNT(*) as count
            FROM bronze.raw_auction_items
            GROUP BY category
            ORDER BY count DESC
        """)
        categories = cursor.fetchall()

        cursor.execute("""
            SELECT zip_code, COUNT(*) as count
            FROM bronze.raw_auction_items
            GROUP BY zip_code
            ORDER BY count DESC
        """)
        zip_codes = cursor.fetchall()

    print("\n" + "=" * 50)
    print("DATABASE STATISTICS")
    print("=" * 50)
    print(f"Total Items:     {item_count}")
    print(f"Total Runs:      {run_count}")
    print(f"Oldest Item:     {dates['oldest'] or 'N/A'}")
    print(f"Newest Item:     {dates['newest'] or 'N/A'}")
    print("-" * 50)
    print("Items by Category:")
    for cat in categories:
        print(f"  {cat['category'] or 'all'}: {cat['count']}")
    print("-" * 50)
    print("Items by Zip Code:")
    for zc in zip_codes:
        print(f"  {zc['zip_code']}: {zc['count']}")
    print("=" * 50)


def cmd_recent(conn, limit: int = 10) -> None:
    """Show recent items."""
    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute("""
            SELECT item_id, scraped_at, category, raw_json
            FROM bronze.raw_auction_items
            ORDER BY scraped_at DESC
            LIMIT %s
        """, (limit,))
        items = cursor.fetchall()

    print(f"\n{len(items)} Most Recent Items:")
    print("-" * 80)

    for item in items:
        raw = item["raw_json"]
        title = raw.get("lead", "No title")[:50]
        auction = raw.get("auction_data", {})
        event_name = auction.get("eventName", "Unknown")[:30]
        city = auction.get("eventCity", "")
        state = auction.get("eventState", "")

        print(f"ID: {item['item_id']}")
        print(f"  Title:    {title}")
        print(f"  Auction:  {event_name}")
        print(f"  Location: {city}, {state}")
        print(f"  Scraped:  {item['scraped_at']}")
        print("-" * 80)


def cmd_runs(conn) -> None:
    """Show scrape run history."""
    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute("""
            SELECT * FROM bronze.scrape_runs
            ORDER BY started_at DESC
            LIMIT 20
        """)
        runs = cursor.fetchall()

    print("\nScrape Run History:")
    print("-" * 100)
    print(f"{'ID':<5} {'Status':<12} {'Zip':<8} {'Radius':<8} {'Found':<8} {'Added':<8} {'Errors':<8} {'Started'}")
    print("-" * 100)

    for run in runs:
        started = str(run['started_at'])[:19]
        print(
            f"{run['id']:<5} "
            f"{run['status']:<12} "
            f"{run['zip_code']:<8} "
            f"{run['radius_miles']:<8} "
            f"{run['items_found']:<8} "
            f"{run['items_added']:<8} "
            f"{run['errors']:<8} "
            f"{started}"
        )


def cmd_item(conn, item_id: str) -> None:
    """Show full JSON for an item."""
    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            "SELECT * FROM bronze.raw_auction_items WHERE item_id = %s", (item_id,)
        )
        item = cursor.fetchone()

    if not item:
        print(f"Item not found: {item_id}")
        return

    print(f"\nItem: {item_id}")
    print(f"Scraped: {item['scraped_at']}")
    print(f"Zip: {item['zip_code']}, Radius: {item['radius_miles']}")
    print(f"Category: {item['category'] or 'all'}")
    print("-" * 50)
    print("Raw JSON:")
    print(json.dumps(item["raw_json"], indent=2))


def cmd_search(conn, term: str) -> None:
    """Search items by text in JSON."""
    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute("""
            SELECT item_id, scraped_at, raw_json
            FROM bronze.raw_auction_items
            WHERE raw_json::text ILIKE %s
            LIMIT 20
        """, (f"%{term}%",))
        items = cursor.fetchall()

    print(f"\nSearch results for '{term}': {len(items)} items")
    print("-" * 80)

    for item in items:
        raw = item["raw_json"]
        title = raw.get("lead", "No title")[:60]
        print(f"{item['item_id']}: {title}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Query HiBid auction database")
    parser.add_argument(
        "command",
        choices=["stats", "recent", "runs", "item", "search"],
        help="Command to run",
    )
    parser.add_argument(
        "arg",
        nargs="?",
        help="Command argument (item_id for 'item', search term for 'search', count for 'recent')",
    )

    args = parser.parse_args()

    try:
        config = Config.from_env()
    except ValueError:
        config = Config(zip_code="00000")

    conn = connect_db(config)

    try:
        if args.command == "stats":
            cmd_stats(conn)
        elif args.command == "recent":
            limit = int(args.arg) if args.arg else 10
            cmd_recent(conn, limit)
        elif args.command == "runs":
            cmd_runs(conn)
        elif args.command == "item":
            if not args.arg:
                print("Error: item_id required")
                return 1
            cmd_item(conn, args.arg)
        elif args.command == "search":
            if not args.arg:
                print("Error: search term required")
                return 1
            cmd_search(conn, args.arg)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
