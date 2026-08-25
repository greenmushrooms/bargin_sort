-- The match cache: which live lots satisfy which wishlist rows.
--
-- Why this is a table and not a query the page runs. The matcher costs ~4.5
-- seconds: eighteen wishlist rows crossed with ~18,000 live lots is 320,000
-- evaluations of regexes deliberately built to be picky, over title plus
-- description. That is a fine price to pay three times a day and an absurd one
-- to pay on every page load.
--
-- It is safe to cache because the inputs move on a schedule, not continuously.
-- `silver_enhanced.fct_lots` is a dbt table rebuilt by the scrape flow, and
-- `reference.wishlist` is a dbt seed rebuilt from CSV; between those runs the
-- answer cannot change. `match_build` records what the cache was built from so
-- staleness is a fact that can be checked in milliseconds rather than a guess
-- about elapsed time.
--
-- Note what this is NOT. analyses/wishlist_check.sql argues against promoting
-- the matcher to a dbt model, because a model implies the rules are settled
-- and seven rounds of false positives say they are not. That argument still
-- holds and this does not contradict it: the cache lives in `web`, is rebuilt
-- on demand, and can be thrown away without touching the pipeline. If
-- anything the site exists to keep the rules unsettled — the `false_positive`
-- verdict in web.lot_review is how the next round of tuning gets collected.

-- Denormalised on purpose. Everything the list needs is here, so rendering a
-- page is one scan of a few dozen rows instead of a join back into a 79,000
-- row fact table that carries no indexes (dbt drops them on every rebuild).
-- The prices are as current as fct_lots is, which is exactly what they would
-- be if joined live; a manual refresh overlays web.lot_snapshot on top.
CREATE TABLE IF NOT EXISTS web.wishlist_match (
    slug          TEXT        NOT NULL,
    source        TEXT        NOT NULL,
    item_id       TEXT        NOT NULL,

    -- Wishlist row context, copied so the list can group and show caps
    -- without joining the seed.
    label         TEXT,
    priority      INTEGER,
    max_bid_cad   NUMERIC,

    title         TEXT,
    lot_url       TEXT,
    high_bid      NUMERIC,
    min_bid       NUMERIC,
    bid_count     BIGINT,
    km            NUMERIC,
    close_at      TIMESTAMPTZ,
    event_name    TEXT,
    event_city    TEXT,
    thumbnail_url TEXT,

    -- min_bid is the cost to enter, so it is the figure the cap screens on. A
    -- lot already bid past the cap is not an opportunity — but it is still
    -- shown, greyed, rather than hidden: "this one got away at $340" is how a
    -- cap gets recalibrated, and the caps on half these rows are estimates
    -- with no observed clearing price behind them.
    within_budget BOOLEAN,

    PRIMARY KEY (slug, source, item_id)
);

COMMENT ON TABLE web.wishlist_match IS
    'Cached output of the wishlist matcher, denormalised for the list view. '
    'Rebuilt when fct_lots or the wishlist seed changes; disposable.';

CREATE INDEX IF NOT EXISTS wishlist_match_lot_idx
    ON web.wishlist_match (source, item_id);


-- One row, always. Records the inputs the cache was built from so "is this
-- stale" is answerable without re-running the matcher.
--
-- `source_scraped_at` is the newest observation in fct_lots at build time: the
-- pipeline only moves that forward when a dbt build lands, so comparing it to
-- the current maximum is a precise "has the data changed", not a timer.
-- `wishlist_digest` covers the seed the same way, which is what makes editing
-- the CSV and running `dbt seed` show up in the UI without a restart.
CREATE TABLE IF NOT EXISTS web.match_build (
    only_row          BOOLEAN     PRIMARY KEY DEFAULT TRUE CHECK (only_row),
    built_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_scraped_at TIMESTAMPTZ,
    wishlist_digest   TEXT,
    match_count       INTEGER,
    lots_considered   INTEGER,
    duration_ms       INTEGER
);

COMMENT ON TABLE web.match_build IS
    'Single row describing the last wishlist_match rebuild and the pipeline '
    'state it was built from.';
