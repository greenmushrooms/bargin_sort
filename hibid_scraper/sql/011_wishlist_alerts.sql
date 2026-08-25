-- Which wishlist matches have already been announced.
--
-- Without this the notifier is unusable on a schedule. A wishlist match stays
-- matched for as long as its lot is open — typically four days — so a daily
-- run would re-send the same 54 lots every morning and the alert would be
-- ignored inside a week. One row per (lot, wishlist row) turns the daily
-- question into "what is new since yesterday", which is the only question
-- worth a notification.
--
-- Keyed by slug as well as item_id on purpose: one lot can satisfy two
-- wishlist rows (a mini PC that is also storage), and suppressing the second
-- because the first was sent would hide a match under a heading the reader is
-- watching for.
--
-- Like raw.catalog_progress this is operational state, not a source of truth
-- about lots — it records what was *said*, never what exists. Dropping it
-- costs one duplicate morning, nothing more, which is why it carries no
-- foreign key to a partitioned landing table whose rows age out from under it.
--
-- Written only after Telegram confirms the send. A row here means the message
-- left the building; recording on attempt instead would let one 500 from the
-- Bot API silence a lot permanently.

CREATE TABLE IF NOT EXISTS raw.wishlist_alerts (
    item_id      VARCHAR(255) NOT NULL,
    slug         TEXT         NOT NULL,
    notified_at  TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- What the lot stood at when it was announced, so a later "it went for
    -- how much?" can be answered against the number the alert actually quoted.
    high_bid     NUMERIC,
    PRIMARY KEY (item_id, slug)
);

COMMENT ON TABLE raw.wishlist_alerts IS
    'One row per wishlist match already sent to Telegram. Suppresses repeats '
    'on the daily run; written only after the Bot API confirms the send.';

COMMENT ON COLUMN raw.wishlist_alerts.high_bid IS
    'Bid at announcement time. Context for the alert, not lot state — '
    'silver_enhanced.fct_lots remains the answer to what a lot is worth now.';

-- The notifier asks "which of today's matches are not in here" once per run,
-- and retention asks for old rows. Both are served by the primary key and a
-- date scan respectively; the index below is for the retention sweep only.
CREATE INDEX IF NOT EXISTS wishlist_alerts_notified_at_idx
    ON raw.wishlist_alerts (notified_at);
