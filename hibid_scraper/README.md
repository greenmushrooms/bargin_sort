# HiBid Auction Scraper

Scrapes auction items from HiBid based on zip code and radius, storing raw JSON payloads in SQLite (PostgreSQL-compatible).

Adapted from [texas_auctions_scraper](https://github.com/jkoelmel/texas_auctions_scraper).

## Features

- Location-based search by zip code and radius
- Stores complete raw JSON payloads (no data filtering)
- SQLite storage with easy PostgreSQL migration path
- Test mode for development
- Respectful rate limiting (2-5 second delays)
- Retry logic for failed requests
- Run tracking and statistics

## Installation

```bash
# Clone or navigate to the project directory
cd hibid_scraper

# Install dependencies with uv
uv sync

# Or create virtual environment and install
uv venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows
uv sync
```

## Configuration

Copy the example environment file and configure:

```bash
cp .env.example .env
```

Edit `.env` with your settings:

```env
ZIP_CODE=78414          # Your zip code (required)
RADIUS_MILES=50         # Search radius: 10, 25, 50, 100, 250, 500
TEST_MODE=false         # Limit to 20 items for testing
DATABASE_URL=sqlite:///hibid_auctions.db
```

## Usage

### Basic Usage

```bash
# Run with .env configuration
uv run python main.py

# Or if venv is activated
python main.py
```

### Test Mode (20 items)

```bash
uv run python main.py --test
```

### Command Line Options

```bash
# Override zip code
uv run python main.py --zip 90210

# Override search radius
uv run python main.py --radius 100

# Scrape specific categories
uv run python main.py --categories "cars,trucks,coins---currency"

# Combine options
uv run python main.py --test --zip 12345 --radius 25

# Custom test limit
uv run python main.py --test --limit 50

# Verbose logging
uv run python main.py --log-level DEBUG
```

### Example Output

```
2024-01-15 10:30:00 - __main__ - INFO - Starting HiBid scraper
2024-01-15 10:30:00 - __main__ - INFO - Configuration: zip=78414, radius=50, test_mode=True
2024-01-15 10:30:01 - scraper - INFO - Scraping category: all (zip: 78414, radius: 50 miles)
2024-01-15 10:30:03 - scraper - INFO - Page 1: found 100 items
2024-01-15 10:30:05 - scraper - INFO - Test mode limit reached (20 items)

============================================================
SCRAPE RUN SUMMARY
============================================================
Run ID:          1
Status:          completed
Duration:        5.23 seconds
------------------------------------------------------------
Zip Code:        78414
Radius:          50 miles
Test Mode:       Yes
Categories:      all
------------------------------------------------------------
Items Found:     20
Items Added:     20
Items Updated:   0
Errors:          0
============================================================
```

## Database Schema

### auction_items

| Column       | Type    | Description                    |
|--------------|---------|--------------------------------|
| id           | INTEGER | Auto-increment primary key     |
| item_id      | TEXT    | HiBid unique item identifier   |
| raw_json     | TEXT    | Complete raw JSON payload      |
| scraped_at   | TEXT    | Timestamp of scrape            |
| zip_code     | TEXT    | Zip code used for search       |
| radius_miles | INTEGER | Radius used for search         |
| category     | TEXT    | Category searched (nullable)   |
| created_at   | TEXT    | Record creation timestamp      |

### scrape_runs

Tracks each scrape execution for auditing.

| Column        | Type    | Description                 |
|---------------|---------|----------------------------|
| id            | INTEGER | Run identifier             |
| started_at    | TEXT    | Start timestamp            |
| completed_at  | TEXT    | End timestamp              |
| zip_code      | TEXT    | Search zip code            |
| radius_miles  | INTEGER | Search radius              |
| test_mode     | INTEGER | Whether test mode was used |
| items_found   | INTEGER | Total items found          |
| items_added   | INTEGER | New items added to DB      |
| items_updated | INTEGER | Existing items updated     |
| errors        | INTEGER | Error count                |
| status        | TEXT    | completed/failed/interrupted|

## Querying Raw JSON Data

### Using the Query Utility

```bash
# Show database statistics
uv run python query_db.py stats

# Show recent items
uv run python query_db.py recent 10

# Show scrape run history
uv run python query_db.py runs

# View full JSON for a specific item
uv run python query_db.py item 282503697

# Search items by text
uv run python query_db.py search "vintage"
```

### SQLite Examples

```sql
-- View all items
SELECT * FROM auction_items;

-- Get item count
SELECT COUNT(*) FROM auction_items;

-- View recent items
SELECT item_id, scraped_at, category
FROM auction_items
ORDER BY scraped_at DESC
LIMIT 10;

-- Extract specific fields from JSON (SQLite 3.38+)
-- Note: auction data is nested under auction_data from resolved references
SELECT
    item_id,
    json_extract(raw_json, '$.lead') as title,
    json_extract(raw_json, '$.bidAmount') as current_bid,
    json_extract(raw_json, '$.auction_data.eventCity') as city,
    json_extract(raw_json, '$.auction_data.eventState') as state,
    json_extract(raw_json, '$.shippingOffered') as shipping
FROM auction_items;

-- Find items by auction event
SELECT item_id, json_extract(raw_json, '$.auction_data.eventName') as auction
FROM auction_items
WHERE json_extract(raw_json, '$.auction_data.eventName') LIKE '%Estate%';

-- Find items with shipping available
SELECT COUNT(*) FROM auction_items
WHERE json_extract(raw_json, '$.shippingOffered') = 1;

-- View scrape run history
SELECT * FROM scrape_runs ORDER BY started_at DESC;

-- Items from a specific zip code search
SELECT COUNT(*) FROM auction_items WHERE zip_code = '90210';
```

### PostgreSQL Examples (After Migration)

```sql
-- Extract fields using JSONB operators
SELECT
    item_id,
    raw_json->>'lead' as title,
    raw_json->>'bidAmount' as current_bid,
    raw_json->'auction_data'->>'eventCity' as city,
    raw_json->'auction_data'->>'eventState' as state
FROM auction_items;

-- Find items in Texas
SELECT * FROM auction_items
WHERE raw_json->'auction_data'->>'eventState' = 'TX';

-- Full-text search in JSON
SELECT * FROM auction_items
WHERE raw_json::text ILIKE '%vintage%';

-- Find items with shipping (using JSONB containment)
SELECT * FROM auction_items
WHERE raw_json @> '{"shippingOffered": true}';
```

## Migrating to PostgreSQL

1. Install PostgreSQL driver:
   ```bash
   uv add psycopg2-binary
   # Or: uv sync --extra postgres
   ```

2. Update `.env`:
   ```env
   DATABASE_URL=postgresql://user:password@localhost:5432/hibid_auctions
   ```

3. In `database.py`, uncomment the `PostgresDatabase` class and update the import in `main.py`.

4. The schema automatically uses JSONB for better JSON querying performance.

## Finding Categories

HiBid categories can be found by browsing the site. The category slug is in the URL:

- `https://hibid.com/lots/cars/` → category: `cars`
- `https://hibid.com/lots/coins---currency/` → category: `coins---currency`
- `https://hibid.com/lots/antiques/` → category: `antiques`

Leave `SEARCH_CATEGORIES` empty to scrape all open lots.

## Rate Limiting

The scraper includes respectful rate limiting:
- 2-5 second random delays between requests
- Exponential backoff on failures
- Maximum 3 retries per request

## Logs

Logs are written to both stdout and `scraper.log` in the project directory.

## License

MIT License - See original repository for details.
