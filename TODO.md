# bargin_sort — Task Tracker

## Dependency Chain
```
#1 Create PG database & grants ✓
├── #2 Set up Prefect Secret blocks ✓
│   └── #4 Wrap hibid_scraper in Prefect flow ✓
├── #3 Migrate hibid_scraper to PostgreSQL ✓
│   ├── #5 Build dbt layer (bronze → silver → silver_enhanced) ✓
│   │   ├── #9 Bronze retention, 6 month window ✓
│   │   └── #7 Build Claude eval layer
│   └── #6 Add specific item search capability
└── #8 Add VPN for scraper (PIA)
```
## Tasks

### 1. [x] Create PostgreSQL database and user grants
- Connect to PG server as privileged user
- CREATE DATABASE bargin_sort
- Create schemas: raw, public (for dbt models)
- GRANT privileges to the application user
- Verify connectivity from the app user
- **Blocker: needs manual DB admin access**

### 2. [x] Set up Prefect Secret blocks for bargin_sort
- Blocked by: #1
- Blocks created: `bargin-sort--database-host` (`hub_db`), `bargin-sort--database-port` (`5432`), `bargin-sort--database-user`, `bargin-sort--database-password`, `bargin-sort--database-name` (`bargin_sort`)

### 3. [x] Migrate hibid_scraper from SQLite to PostgreSQL
- Blocked by: #1
- Activate PostgreSQL implementation in database.py
- Use JSONB for raw_json column
- Keep schema: auction_items + scrape_runs
- Wire up connection string from env vars / Prefect blocks
- Add psycopg2-binary to dependencies

### 4. [x] Wrap hibid_scraper in Prefect flow
- Blocked by: #2, #3
- Flow: `scrape_auctions()` with params (`zip_code`, `radius_miles`, `categories`, `test_mode`, `test_limit`)
- Task: `scrape_hibid()`
- Deployment: `prefect.yaml` + `Dockerfile` (work pool: `dev-pool-docker`, network: `project-hub-network`)
- All DB env vars wired via secret blocks

### 5. [x] Build dbt layer for raw JSON to structured tables
- Blocked by: #3
- Medallion layers as schemas: `bronze` → `silver` → `silver_enhanced`
- dbt project `data__bargin_sort` (own `.venv`, in-project `profiles.yml`)
- **Bronze** (`hibid_scraper/sql/001_bronze.sql`): append-only, monthly range
  partitions on `scraped_at`, owned by the scraper
- **Silver**: `stg_lots` (incremental, `delete+insert` on `sys_run_name`),
  `stg_auctions` (rebuilt each run)
- **Silver enhanced**: `fct_lots` (+ `distance_km`), `lots_within_50km` view
- Seed `postal_centroids` — 43,141 GeoNames CA FSA + US ZIP centroids
- Reprocess: `dbt run --full-refresh` replays all of bronze; verified to give
  the same result as the incremental path
- Wired into `scrape_auctions` as the `build_silver` task — dbt runs in-process
  scoped to the finished run via `--vars '{target_run: <sys_run_name>}'`, so a
  transform costs one run's work regardless of how much history bronze holds.
  Disable per-run with `build_downstream=False`.
- Image now built from the repo root so it carries the dbt project:
  `docker build -f hibid_scraper/Dockerfile -t integration-bargin-sort .`

### 6. [ ] Add specific item search capability
- Blocked by: #3
- Add keyword/search term parameter to scraper
- Build HiBid search URL with the term
- Create separate Prefect task/flow for on-demand search
- Store results with search_term tag

### 8. [ ] Add VPN for scraper (PIA)
- Blocked by: #4
- Hide scraper IP from HiBid using PIA VPN
- Options: Gluetun container (HTTP proxy w/ kill switch) vs PIA SOCKS5 proxy (lighter, no kill switch)
- Decision pending — see memory/vpn-research.md for full analysis

### 9. [x] Retention — drop bronze data older than 6 months
- Blocked by: #5
- `bronze.drop_partitions_before()` / `bronze.delete_runs_before()` SQL functions
- Prefect flow `bronze_retention` (`maintenance.py`), `dry_run` param, monthly cron
- DROP TABLE per partition, so cost is independent of row count
- After a drop, run `dbt run --full-refresh` so silver stops describing
  lots whose bronze rows are gone

### 7. [ ] Build Claude eval layer for auction items
- Blocked by: #5
- Lands in `silver_enhanced` alongside `fct_lots`
- Adapt agent_eval.py from job_searcher_2
- Evaluation: estimated value vs bid, condition, shipping, deal quality
- Structured output via Tools API
- Store in evaluated_items table
- Support generic eval + targeted eval
