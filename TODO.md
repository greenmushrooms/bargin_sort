# bargin_sort — Task Tracker

## Dependency Chain
```
#1 Create PG database & grants
├── #2 Set up Prefect Secret blocks
│   └── #4 Wrap hibid_scraper in Prefect flow (also needs #3)
├── #3 Migrate hibid_scraper to PostgreSQL
│   ├── #5 Build dbt layer (raw JSON → structured tables)
│   │   └── #7 Build Claude eval layer
│   └── #6 Add specific item search capability
```

## Tasks

### 1. [ ] Create PostgreSQL database and user grants
- Connect to PG server as privileged user
- CREATE DATABASE bargin_sort
- Create schemas: raw, public (for dbt models)
- GRANT privileges to the application user
- Verify connectivity from the app user
- **Blocker: needs manual DB admin access**

### 2. [ ] Set up Prefect Secret blocks for bargin_sort
- Blocked by: #1
- Create setup_secrets.py (modeled after job_searcher_2)
- Blocks: bargin-sort--database-host, database-port, database-user, database-password, database-name, anthropic-api-key, telegram-bot-token, telegram-chat-id
- Create .env.example template

### 3. [ ] Migrate hibid_scraper from SQLite to PostgreSQL
- Blocked by: #1
- Activate PostgreSQL implementation in database.py
- Use JSONB for raw_json column
- Keep schema: auction_items + scrape_runs
- Wire up connection string from env vars / Prefect blocks
- Add psycopg2-binary to dependencies

### 4. [ ] Wrap hibid_scraper in Prefect flow
- Blocked by: #2, #3
- Main flow: scrape_auctions() with params (zip_code, radius, categories, search_term)
- Tasks: scrape_hibid(), run_dbt(), evaluate_items(), notify_results()
- Add prefect.yaml deployment config + Dockerfile
- Wire up secret blocks as env vars
- Support local execution initially

### 5. [ ] Build dbt layer for raw JSON to structured tables
- Blocked by: #3
- Create dbt project (data__bargin_sort)
- Source: raw auction_items table (JSONB)
- Models: stg_auction_items, stg_auctions, fct_items
- Incremental strategy using sys_run_name

### 6. [ ] Add specific item search capability
- Blocked by: #3
- Add keyword/search term parameter to scraper
- Build HiBid search URL with the term
- Create separate Prefect task/flow for on-demand search
- Store results with search_term tag

### 7. [ ] Build Claude eval layer for auction items
- Blocked by: #5
- Adapt agent_eval.py from job_searcher_2
- Evaluation: estimated value vs bid, condition, shipping, deal quality
- Structured output via Tools API
- Store in evaluated_items table
- Support generic eval + targeted eval
