-- Where each auction's catalogue pagination got to.
--
-- A big catalogue cannot be taken in one pass. HiBid answers erratically on
-- deep pages — measured on a 4,296-lot auction, pages 10 and 11 returned
-- cleanly while 1, 5, 12, 15 and 20 came back with no Apollo state at all — so
-- any single run stops somewhere short. Restarting from page 1 next time just
-- re-collects the same early pages, which is why that auction sat at 28%
-- coverage after three runs.
--
-- Recording the last page that yielded lots lets the next run continue from
-- there. Combined with coverage-based capture accounting (auction_manifest),
-- successive partial runs converge on the whole catalogue instead of
-- re-treading the front of it.
--
-- Not partitioned and not append-only: this is scheduler state, one row per
-- auction, deliberately mutable. It is a cache of progress, never a source of
-- truth about lots — raw.hibid remains that.

CREATE TABLE IF NOT EXISTS raw.catalog_progress (
    auction_id   BIGINT      PRIMARY KEY,
    last_page    INTEGER     NOT NULL DEFAULT 0,
    lots_seen    INTEGER     NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

COMMENT ON COLUMN raw.catalog_progress.last_page IS
    'Highest page that yielded lots. 0 means start from the beginning — also '
    'what a run resets it to after paging past the end, so the next pass '
    'sweeps the front again and fills whatever the deep pages missed.';
