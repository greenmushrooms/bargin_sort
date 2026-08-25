-- lot_event — the append-only history behind web.lot_review.
--
-- lot_review holds one row per lot and answers "what do I think of this now",
-- which is what the list needs to render and what makes absence meaningful.
-- What it cannot answer is anything about time: changing a verdict overwrites
-- the old one, so "when did I star this", "what did I pass on and later
-- regret" and "how did my ceiling move as the bidding went" are all
-- unanswerable the moment they matter.
--
-- This table is the log; lot_review is the fold of it. They are written in one
-- transaction and are expected to agree, but the log is the record: if they
-- ever disagree, lot_review is the one that is wrong, because it is derived.
--
-- Why this is app data and lives in web.*, not raw.*:
-- an observation of a lot is pipeline data and belongs in bronze -- which is
-- exactly where the refresh button writes it, through the scraper's own
-- writer. A verdict is not an observation of the world; it is a person's
-- opinion, it has no run, no partition and no replay, and dbt must never own
-- it. Keeping it here is what lets `dbt run --full-refresh` stay a safe thing
-- to type.
--
-- Deliberately NOT partitioned. Bronze drops partitions at six months
-- (hibid_scraper/maintenance.py drives that off an explicit SOURCE_TABLES
-- list) and lot_review's own DDL says the verdict should outlive the listing.
-- A heavy day of triage is ~80 rows, so partitioning would buy nothing and
-- cost exactly the history this table exists to keep.

CREATE TABLE IF NOT EXISTS web.lot_event (
    event_id    BIGSERIAL   PRIMARY KEY,

    source      TEXT        NOT NULL,
    item_id     TEXT        NOT NULL,

    -- The verdict as of this event. NULL means cleared: the UI toggles, so
    -- clicking the verdict a lot already has removes it, and "I un-starred
    -- this" is a real event worth keeping rather than a gap in the record.
    -- The allowed values match lot_review's CHECK exactly; they are repeated
    -- rather than shared so that dropping lot_review cannot silently widen
    -- what the log accepts.
    verdict     TEXT,

    max_bid     NUMERIC,
    notes       TEXT,

    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT lot_event_verdict_check CHECK (
        verdict IS NULL OR verdict IN
            ('starred', 'bidding', 'won', 'lost', 'passed', 'false_positive')
    )
);

-- The two questions asked of this table: one lot's history, newest first, and
-- everything that happened in a window.
CREATE INDEX IF NOT EXISTS lot_event_lot_idx
    ON web.lot_event (source, item_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS lot_event_time_idx
    ON web.lot_event (occurred_at DESC);


-- Current state as the log sees it, for checking the fold against the record.
-- DISTINCT ON is the same latest-wins shape fct_lots uses over observations.
CREATE OR REPLACE VIEW web.lot_event_current AS
SELECT DISTINCT ON (source, item_id)
       source, item_id, verdict, max_bid, notes, occurred_at
FROM web.lot_event
ORDER BY source, item_id, occurred_at DESC, event_id DESC;


-- One-shot seed, guarded so the file stays re-runnable.
--
-- Without this the log begins empty and every verdict already recorded looks
-- like it appeared from nowhere. Two sources, in order:
--
--   * lot_review_bak, if present -- the pre-clear snapshot taken on
--     2026-08-24, which is the only remaining record of the verdicts cleared
--     that day. Rows in the snapshot with no surviving review row get a
--     trailing NULL (cleared) event, because that is what happened to them.
--   * lot_review itself, for anything the snapshot does not cover.
--
-- created_at is used as occurred_at: it is when the verdict was actually made,
-- and backdating the log to the truth is better than stamping it all `now()`.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM web.lot_event) THEN
        RETURN;
    END IF;

    IF to_regclass('web.lot_review_bak') IS NOT NULL THEN
        EXECUTE $seed$
            INSERT INTO web.lot_event (source, item_id, verdict, max_bid, notes, occurred_at)
            SELECT source, item_id, status, max_bid, notes, created_at
            FROM web.lot_review_bak
        $seed$;

        EXECUTE $cleared$
            INSERT INTO web.lot_event (source, item_id, verdict, notes, occurred_at)
            SELECT b.source, b.item_id, NULL,
                   'cleared in the 2026-08-24 sweep of closed lots',
                   TIMESTAMPTZ '2026-08-24 19:01:00+00'
            FROM web.lot_review_bak b
            WHERE NOT EXISTS (
                SELECT 1 FROM web.lot_review r
                WHERE r.source = b.source AND r.item_id = b.item_id
            )
        $cleared$;
    END IF;

    INSERT INTO web.lot_event (source, item_id, verdict, max_bid, notes, occurred_at)
    SELECT r.source, r.item_id, r.status, r.max_bid, r.notes, r.created_at
    FROM web.lot_review r
    WHERE NOT EXISTS (
        SELECT 1 FROM web.lot_event e
        WHERE e.source = r.source AND e.item_id = r.item_id
    );
END $$;
