-- How many times each catalogue page has been attempted.
--
-- Without this, a page that cannot be read blocks the rest of its catalogue.
-- Failed pages sort lowest, so they were retried first, and three consecutive
-- failures trip the circuit breaker before the pass ever reaches pages it has
-- never tried. An auction could sit at 15 of 43 pages indefinitely, spending
-- every pass re-failing the same three.
--
-- Attempts are counted per page so that two things become possible: work the
-- never-tried pages first, and retire a page that has failed enough times to
-- look genuinely unreadable rather than merely throttled.
ALTER TABLE raw.catalog_pages
    ADD COLUMN IF NOT EXISTS page_attempts JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN raw.catalog_pages.page_attempts IS
    'Page number (as text) to attempt count. A page at or past the retry limit '
    'is skipped so it cannot starve pages that have never been tried.';
