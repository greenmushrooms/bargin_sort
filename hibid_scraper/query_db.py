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
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv
from config import Config

load_dotenv()


def get_db_path() -> str:
    """Get database path from config."""
    try:
        config = Config.from_env()
        return config.get_sqlite_path()
    except ValueError:
        # Default if no config
        return "hibid_auctions.db"


def connect_db(db_path: str) -> sqlite3.Connection:
    """Connect to database."""
    if not Path(db_path).exists():
        print(f"Error: Database not found at {db_path}")
        print("Run the scraper first: python main.py --test")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def cmd_stats(conn: sqlite3.Connection) -> None:
    """Show database statistics."""
    cursor = conn.cursor()

    # Item count
    cursor.execute("SELECT COUNT(*) as count FROM auction_items")
    item_count = cursor.fetchone()["count"]

    # Run count
    cursor.execute("SELECT COUNT(*) as count FROM scrape_runs")
    run_count = cursor.fetchone()["count"]

    # Date range
    cursor.execute("""
        SELECT MIN(scraped_at) as oldest, MAX(scraped_at) as newest
        FROM auction_items
    """)
    dates = cursor.fetchone()

    # Categories
    cursor.execute("""
        SELECT category, COUNT(*) as count
        FROM auction_items
        GROUP BY category
        ORDER BY count DESC
    """)
    categories = cursor.fetchall()

    # Zip codes
    cursor.execute("""
        SELECT zip_code, COUNT(*) as count
        FROM auction_items
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


def cmd_recent(conn: sqlite3.Connection, limit: int = 10) -> None:
    """Show recent items."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT item_id, scraped_at, category, raw_json
        FROM auction_items
        ORDER BY scraped_at DESC
        LIMIT ?
    """, (limit,))

    items = cursor.fetchall()

    print(f"\n{len(items)} Most Recent Items:")
    print("-" * 80)

    for item in items:
        raw = json.loads(item["raw_json"])
        title = raw.get("lead", "No title")[:50]
        # Auction data is nested under auction_data from resolved references
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


def cmd_runs(conn: sqlite3.Connection) -> None:
    """Show scrape run history."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM scrape_runs
        ORDER BY started_at DESC
        LIMIT 20
    """)

    runs = cursor.fetchall()

    print("\nScrape Run History:")
    print("-" * 100)
    print(f"{'ID':<5} {'Status':<12} {'Zip':<8} {'Radius':<8} {'Found':<8} {'Added':<8} {'Errors':<8} {'Started'}")
    print("-" * 100)

    for run in runs:
        print(
            f"{run['id']:<5} "
            f"{run['status']:<12} "
            f"{run['zip_code']:<8} "
            f"{run['radius_miles']:<8} "
            f"{run['items_found']:<8} "
            f"{run['items_added']:<8} "
            f"{run['errors']:<8} "
            f"{run['started_at'][:19]}"
        )


def cmd_item(conn: sqlite3.Connection, item_id: str) -> None:
    """Show full JSON for an item."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM auction_items WHERE item_id = ?", (item_id,)
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
    print(json.dumps(json.loads(item["raw_json"]), indent=2))


def cmd_search(conn: sqlite3.Connection, term: str) -> None:
    """Search items by text in JSON."""
    cursor = conn.cursor()
    # Search in raw_json text
    cursor.execute("""
        SELECT item_id, scraped_at, raw_json
        FROM auction_items
        WHERE raw_json LIKE ?
        LIMIT 20
    """, (f"%{term}%",))

    items = cursor.fetchall()

    print(f"\nSearch results for '{term}': {len(items)} items")
    print("-" * 80)

    for item in items:
        raw = json.loads(item["raw_json"])
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
    parser.add_argument(
        "--db",
        help="Database path (default: from .env or hibid_auctions.db)",
    )

    args = parser.parse_args()

    db_path = args.db or get_db_path()
    conn = connect_db(db_path)

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
