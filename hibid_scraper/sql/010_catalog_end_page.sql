-- The page at which an auction's catalogue actually ends.
--
-- expected_pages comes from discovery's lot_count, which is every lot the
-- auction ever had. The catalogue only serves lots that are still open, and it
-- shrinks as the auction closes — one 4,296-lot auction served 43 pages
-- overnight and 11 the following afternoon. Asking for the missing 32 pages
-- burns four attempts each and marks them failed, so a late auction yields
-- nothing rather than yielding what is still there.
--
-- end_page is the lowest page confirmed to be past the end: HiBid answers those
-- with a 183-byte stub. That stub is ambiguous on its own — it is also what
-- throttling looks like — so it is only believed when a lower page in the same
-- pass returned lots. A stub with no good page behind it stays a failure.
ALTER TABLE raw.catalog_pages
    ADD COLUMN IF NOT EXISTS end_page INTEGER;

COMMENT ON COLUMN raw.catalog_pages.end_page IS
    'Lowest page confirmed past the end of the catalogue, or NULL if unknown. '
    'Caps the work queue so a shrunken catalogue is not chased to lot_count.';
