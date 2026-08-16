-- Source: hibid_auctions — the auction-level discovery feed.
--
-- The lot search answers "which lots are open right now". That is the wrong
-- question for scheduling, and it costs twice: an auction stays open for a
-- median of four days, so a daily sweep re-reads it four times to learn
-- nothing, and a sweep that lands mid-close returns the auction half-empty
-- because HiBid drops closed lots from an open-status search.
--
-- /auctions answers "which auctions are near here and when do they close" in
-- one request. That is what lets the orchestrator visit each auction exactly
-- once, shortly before it closes, and read every one of its lots while they
-- are all still open.
--
-- Same shape as the other landing tables: job, item, payload. `item_id` is the
-- HiBid auction id. Append-only like the rest of raw, so the row history also
-- records how an auction's lot count and close time moved before it closed.

CREATE TABLE IF NOT EXISTS raw.hibid_auctions (
    id           BIGSERIAL,
    sys_run_name VARCHAR(255) NOT NULL,
    item_id      VARCHAR(255) NOT NULL,
    category     VARCHAR(255),
    raw_json     JSONB        NOT NULL,
    scraped_at   TIMESTAMPTZ  NOT NULL,
    created_at   TIMESTAMPTZ  DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id, scraped_at)
) PARTITION BY RANGE (scraped_at);

CREATE INDEX IF NOT EXISTS idx_hibid_auctions_item_id      ON raw.hibid_auctions (item_id);
CREATE INDEX IF NOT EXISTS idx_hibid_auctions_scraped_at   ON raw.hibid_auctions (scraped_at);
CREATE INDEX IF NOT EXISTS idx_hibid_auctions_sys_run_name ON raw.hibid_auctions (sys_run_name);
CREATE INDEX IF NOT EXISTS idx_hibid_auctions_gin          ON raw.hibid_auctions USING GIN (raw_json);


-- Which auction a run captured.
--
-- This is what makes "scrape each auction once" enforceable: the orchestrator
-- skips any auction that already has a completed run against it. NULL means
-- the run was not scoped to one auction — a radius sweep, a discovery pass, or
-- a source with no auction grouping at all — so the column never has to lie.
ALTER TABLE raw.scrape_runs ADD COLUMN IF NOT EXISTS auction_id BIGINT;

-- Partial: only auction-scoped runs are ever looked up this way, and they are
-- the minority of rows.
CREATE INDEX IF NOT EXISTS idx_scrape_runs_auction
    ON raw.scrape_runs (auction_id)
    WHERE auction_id IS NOT NULL;
