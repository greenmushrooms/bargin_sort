-- Which pages of each auction's catalogue have actually been banked.
--
-- Replaces the single `last_page` high-water mark, which could not express the
-- state that actually occurs: "I have pages 1-5 and 8-12, missing 6-7". A scan
-- resuming from one number either re-reads what it has or skips what it does
-- not, and it did both — 26% of all fetching was re-reading lots already held.
--
-- The deeper problem it fixes is that pagination used to abort on a short page.
-- A short page is a throttling signal, not an end signal, so one bad page at
-- position 5 abandoned pages 6..N for that pass — and the next pass came a full
-- day later, by which time auctions had closed. Nine of them closed at 46%
-- average coverage, losing 7,467 lots that cannot be re-read: HiBid zeroes bid
-- data at close.
--
-- With a page set, the missing pages *are* the work queue. A failed page stays
-- missing and is retried; it costs one page, not the remainder of the
-- catalogue. And `expected_pages` comes from discovery's lot_count, so "is this
-- the end?" stops being a guess — which is what most of the pagination
-- heuristics were unsuccessfully trying to infer.
--
-- Scheduler state: one mutable row per auction, deliberately not append-only
-- and never a source of truth about lots. raw.hibid remains that.

CREATE TABLE IF NOT EXISTS raw.catalog_pages (
    auction_id     BIGINT      PRIMARY KEY,
    -- Pages successfully fetched, ascending. Small by nature: even a
    -- 4,296-lot catalogue is 43 pages at 100 per page.
    pages_done     INTEGER[]   NOT NULL DEFAULT '{}',
    -- ceil(lot_count / ITEMS_PER_PAGE) as discovery last reported it. Sellers
    -- add and pull lots, so this is a target rather than a contract.
    expected_pages INTEGER     NOT NULL DEFAULT 0,
    -- Pages attempted and not banked, so repeated failures are visible rather
    -- than looking like work that was never scheduled.
    pages_failed   INTEGER[]   NOT NULL DEFAULT '{}',
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

COMMENT ON TABLE raw.catalog_pages IS
    'Page-level progress per auction catalogue. The set of pages not in '
    'pages_done is the work queue for the next pass.';

-- The old high-water table is superseded. Kept, not dropped: it records what
-- earlier passes reached, and dropping it would strand auctions mid-catalogue
-- with no way to tell how far they had got.
COMMENT ON TABLE raw.catalog_progress IS
    'Superseded by raw.catalog_pages. Retained for the auctions that were '
    'part-way through when page-level tracking replaced the high-water mark.';
