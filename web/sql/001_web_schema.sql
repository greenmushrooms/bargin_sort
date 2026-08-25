-- The `web` schema: everything the browser writes.
--
-- The boundary is the point of this file. `raw`, `silver` and
-- `silver_enhanced` are the pipeline's, and the site reads them and never
-- writes them; anything a person does in the UI lands here instead. That split
-- is what keeps `dbt run --full-refresh` a safe thing to type — replaying all
-- of bronze rebuilds silver from scratch, and it must not take a month of
-- triage decisions with it.
--
-- The one deliberate exception is the refresh, which writes a bronze row
-- through the scraper's own Database class. That is the scraper's table being
-- used the scraper's way, not the site reaching into it: an observation of a
-- lot is an observation of a lot regardless of what triggered the fetch, and
-- routing it anywhere else would make silver disagree with what the site is
-- showing.

CREATE SCHEMA IF NOT EXISTS web;


-- ---------------------------------------------------------------------------
-- lot_review — the selection state. One row per lot the user has touched.
-- ---------------------------------------------------------------------------
--
-- Absence is meaningful: no row means "not yet looked at", which is what the
-- UI shows as unread. That is why nothing here is backfilled and why there is
-- no default status — a lot with a row has been judged, a lot without one has
-- not, and collapsing those two would empty the only queue worth reading.
--
-- Keyed by (source, item_id) rather than item_id alone. HiBid lot ids and
-- Police Auctions listing ids are independent sequences from different systems
-- and nothing stops them colliding; they already share a namespace in
-- fct_lots only because `source` sits beside them there too.
--
-- No foreign key to fct_lots on purpose. Bronze partitions age out at six
-- months and silver is rebuilt from what survives, so a lot that has been
-- bid on and won will eventually stop existing upstream. The verdict should
-- outlive the listing — that history is the record of what this thing is
-- actually for.
CREATE TABLE IF NOT EXISTS web.lot_review (
    source      TEXT        NOT NULL,
    item_id     TEXT        NOT NULL,

    -- starred        — wanted; the shortlist
    -- bidding        — a bid is in
    -- won / lost     — how it ended
    -- passed         — matched correctly, not wanted
    -- false_positive — the wishlist regex is wrong, this is not the product
    --
    -- `passed` and `false_positive` are split because they mean opposite
    -- things about the wishlist. Seven rounds of regex tuning have come out of
    -- false positives — IPX7 read as a Sony PX7, "Unified Minds" as UniFi, the
    -- holy grail of skincare as a Canyon Grail — and each was found by hand.
    -- Recording which dismissals were the matcher's fault turns that into a
    -- list you can query instead of a thing you remember.
    status      TEXT        NOT NULL CHECK (status IN (
                    'starred', 'bidding', 'won', 'lost',
                    'passed', 'false_positive')),

    -- What the lot is worth to *this* user, which is not the wishlist row's
    -- cap. The cap is a screen for the whole category; this is a number for
    -- one lot, set after looking at the photos.
    max_bid     NUMERIC,

    notes       TEXT,

    -- Kept because the first question about a stale verdict is always "when
    -- did I decide that" — a `passed` from before the photos loaded is worth
    -- less than one from this morning.
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (source, item_id)
);

COMMENT ON TABLE web.lot_review IS
    'Per-lot triage state set from the UI. No row means unread. Survives '
    'bronze retention and dbt --full-refresh on purpose.';

-- The list view asks "everything I starred" and "everything still unjudged"
-- far more often than it asks about one lot.
CREATE INDEX IF NOT EXISTS lot_review_status_idx
    ON web.lot_review (status, updated_at DESC);


-- ---------------------------------------------------------------------------
-- lot_snapshot — what the last manual refresh saw.
-- ---------------------------------------------------------------------------
--
-- A cache with one job: close the gap between pressing Refresh and dbt
-- running. The refresh writes a bronze row, but `fct_lots` is a dbt table
-- built by the scrape flow, so until the next build the site would still be
-- quoting the price from this morning — on the one lot the user just asked
-- about, which is the worst possible place to be stale.
--
-- Truth still lives in bronze. Dropping this table loses nothing except the
-- freshness of a handful of rows between builds, which is why it carries no
-- constraints tying it to anything and is safe to TRUNCATE.
--
-- `refreshed_at` is the *attempt*; `observed_at` is the last attempt that
-- came back with numbers. They are separate columns because a failed refresh
-- must not overwrite good prices with nulls, and "I tried five minutes ago and
-- HiBid was down" is a different thing to show than "I have never tried".
CREATE TABLE IF NOT EXISTS web.lot_snapshot (
    source        TEXT        NOT NULL,
    item_id       TEXT        NOT NULL,

    refreshed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    refresh_error TEXT,

    observed_at   TIMESTAMPTZ,
    high_bid      NUMERIC,
    min_bid       NUMERIC,
    bid_count     BIGINT,
    lot_status    TEXT,
    time_left     TEXT,
    close_at      TIMESTAMPTZ,

    -- Full-size image URLs, best first. Stored rather than re-derived because
    -- the lot *page* carries the whole gallery while the catalogue and browse
    -- pages that fed bronze carry one thumbnail — refreshing is how the extra
    -- photos are obtained, so throwing them away would undo the fetch.
    image_urls    TEXT[],

    PRIMARY KEY (source, item_id)
);

COMMENT ON TABLE web.lot_snapshot IS
    'Freshest observation of a lot from the UI refresh button. A cache over '
    'bronze, safe to truncate; refreshed_at is the attempt, observed_at the '
    'last one that returned numbers.';


-- ---------------------------------------------------------------------------
-- saved_search — ad-hoc hunts that are not wishlist rows yet.
-- ---------------------------------------------------------------------------
--
-- The wishlist is a dbt seed, edited in CSV and applied by a `dbt seed` run.
-- That is the right home for a settled rule and the wrong one for "is there a
-- band saw on right now", so the search box keeps its own list here. A query
-- that keeps earning its keep is a candidate for promotion into the seed; one
-- that does not gets deleted without a commit.
CREATE TABLE IF NOT EXISTS web.saved_search (
    name       TEXT        PRIMARY KEY,
    query      TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE web.saved_search IS
    'Named ad-hoc searches. Deliberately not the wishlist seed — these are '
    'throwaway hunts, promoted into reference.wishlist only once they settle.';
