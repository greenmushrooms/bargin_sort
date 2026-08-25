# bargin_sort — Task Tracker

## Dependency Chain
```
#1 Create PG database & grants ✓
├── #2 Set up Prefect Secret blocks ✓
│   └── #4 Wrap hibid_scraper in Prefect flow ✓
├── #3 Migrate hibid_scraper to PostgreSQL ✓
│   ├── #5 Build dbt layer (bronze → silver → silver_enhanced) ✓
│   │   ├── #9 Bronze retention, 6 month window ✓
│   │   ├── #10 Wishlist alerts to Telegram ✓
│   │   ├── #11 Triage site (web/) ✓
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

### 10. [x] Wishlist alerts to Telegram
- Blocked by: #5
- Flow `notify_wishlist` (`notify.py`), deployment `wishlist-notify-deployment`,
  daily 09:30 America/Toronto — behind all three morning scrapes, hours ahead of
  the afternoon closes
- Query held in step with `analyses/wishlist_check.sql`, plus two filters:
  within `max_bid_cad`, and not already announced
- `raw.wishlist_alerts` (`sql/011_wishlist_alerts.sql`) dedupes on
  (item_id, slug) so a match is announced once rather than every morning it
  stays open. Written only after the Bot API confirms the send, so a failed
  request costs a repeat rather than permanent silence
- Messages chunk at 3500 chars with the wishlist heading repeated, since
  Telegram hard-caps at 4096
- **Needs two secret blocks before deploying**: `bargin-sort--telegram-bot-token`,
  `bargin-sort--telegram-chat-id`. Its own bot, not job_searcher's — different
  urgency, and a shared chat means muting one mutes the other
- Flow declines quietly when either is unset, so every other deployment stays
  runnable without them
- `python notify.py --dry-run` formats and logs without sending or recording

### 7. [ ] Build Claude eval layer for auction items
- Blocked by: #5
- Lands in `silver_enhanced` alongside `fct_lots`
- Adapt agent_eval.py from job_searcher_2
- Evaluation: estimated value vs bid, condition, shipping, deal quality
- Structured output via Tools API
- Store in evaluated_items table
- Support generic eval + targeted eval

### 11. [x] Triage site — `web/`
- Blocked by: #5
- FastAPI + Jinja2 + htmx on :7780, own `pyproject.toml` and `.venv`. Same
  shape as `job_searcher_web`; Python rather than Go because the refresh path
  needs `PageFetcher`, `apollo` and `Database`, and reimplementing the
  Cloudflare handling in a second language is how those failure modes come back
- **`web` schema** — everything the browser writes, so `dbt run --full-refresh`
  never takes triage state with it:
  - `web.lot_review` — the selection state, keyed `(source, item_id)`. No row
    means unread, which is what makes the list a queue. `passed` and
    `false_positive` are separate verdicts: the second one is a report that the
    wishlist regex is wrong, which is how round eight of tuning gets collected
  - `web.lot_snapshot` — what the last manual refresh saw; a disposable cache
    over bronze that closes the gap until the next dbt build
  - `web.wishlist_match` + `web.match_build` — the matcher costs 4.5s
    (19 rows × 17,835 live lots), so it is materialised and rebuilt only when
    `max(fct_lots.scraped_at)` or a digest of the seed moves
- **Refresh button** fetches one lot's own page: current price plus the *rest*
  of the photographs, which the catalogue and browse pages do not carry. Writes
  a one-row `completed` scrape run to bronze through the scraper's `Database`,
  so the observation is kept, and to `web.lot_snapshot`, which is what the page
  reads. Both sources supported (HiBid via Apollo cache, PAC via og:title +
  the awe-rt price elements)
- **Ad-hoc search** over all live lots; a `/`-prefixed query runs as a POSIX
  regex, so a candidate wishlist rule can be tried against the corpus before it
  is committed to `seeds/wishlist.csv`
- `cd web && uv sync && uv run uvicorn app:app --port 7780`. See `web/README.md`

### Fixed in passing
- `raw.wishlist_alerts` was owned by `hub_user`, not `user__bargin_sort`. Since
  `sql/011_wishlist_alerts.sql` is now in `database.py::DDL_FILES`, its
  `COMMENT ON` / `CREATE INDEX` statements would have failed with
  `must be owner of table wishlist_alerts` on **every** scraper connect once the
  image was rebuilt. Fixed with `ALTER TABLE raw.wishlist_alerts OWNER TO
  user__bargin_sort`

### Checked and correct: Police Auctions close times
- Suspected during the #11 build that `stg_police_lots` had a 4-hour error —
  `to_timestamp(ends_at, 'MM/DD/YYYY HH24:MI:SS')` under a `TimeZone=Etc/UTC`
  session, against a Toronto site. **The suspicion was wrong**; the model is
  right and `data-action-time` is genuinely UTC. Recorded so it is not
  re-opened on the same hunch
- The evidence: `/Browse` lists only open lots, so every observation of an open
  lot must have its true close in the future. Reading `ends_at` as UTC, the
  hours-to-close distribution over 3,559 observations has a hard floor at zero
  — one observation at -0.07h, then 36 in the 0-1h band and 44 in 1-2h. Were
  the string Toronto local (true close = read + 4h), lots would remain listed
  through their final four hours and the -4h..0h buckets would hold roughly a
  sixth of all final observations
- What misled the eye: page 1 sorted by ending-soonest shows staggered closes
  at 20:20 / 20:23 / 20:25, which reads as an 8:20 PM local pattern. It is
  20:20 UTC — a 4:20 PM Toronto close, staggered three minutes apart
