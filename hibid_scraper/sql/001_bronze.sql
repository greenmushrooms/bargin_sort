-- Bronze layer: immutable, append-only landing zone for raw HiBid payloads.
--
-- Every scrape writes here and nothing ever updates a row, so silver and
-- silver_enhanced can always be rebuilt from scratch by replaying this table.
--
-- raw_auction_items is range-partitioned by month on scraped_at. Retention is
-- the reason: dropping a month is a DROP TABLE on one partition rather than a
-- DELETE walking millions of rows and leaving them for VACUUM.

CREATE SCHEMA IF NOT EXISTS bronze;

CREATE TABLE IF NOT EXISTS bronze.raw_auction_items (
    id            BIGSERIAL,
    item_id       VARCHAR(255) NOT NULL,
    raw_json      JSONB        NOT NULL,
    scraped_at    TIMESTAMPTZ  NOT NULL,
    zip_code      VARCHAR(10)  NOT NULL,
    radius_miles  INTEGER      NOT NULL,
    category      VARCHAR(255),
    sys_run_name  VARCHAR(255) NOT NULL,
    created_at    TIMESTAMPTZ  DEFAULT CURRENT_TIMESTAMP,
    -- The partition key has to be in the primary key, hence the composite.
    PRIMARY KEY (id, scraped_at)
) PARTITION BY RANGE (scraped_at);

-- Declared on the parent, so every partition inherits them automatically.
CREATE INDEX IF NOT EXISTS idx_raw_auction_items_item_id
    ON bronze.raw_auction_items (item_id);
CREATE INDEX IF NOT EXISTS idx_raw_auction_items_scraped_at
    ON bronze.raw_auction_items (scraped_at);
CREATE INDEX IF NOT EXISTS idx_raw_auction_items_sys_run_name
    ON bronze.raw_auction_items (sys_run_name);
CREATE INDEX IF NOT EXISTS idx_raw_auction_items_category
    ON bronze.raw_auction_items (category);
CREATE INDEX IF NOT EXISTS idx_raw_auction_items_gin
    ON bronze.raw_auction_items USING GIN (raw_json);

-- Run-level bookkeeping. Small and queried by dbt to find the newest complete
-- run, so it is deliberately not partitioned.
CREATE TABLE IF NOT EXISTS bronze.scrape_runs (
    id             SERIAL PRIMARY KEY,
    started_at     TIMESTAMPTZ NOT NULL,
    completed_at   TIMESTAMPTZ,
    zip_code       VARCHAR(10) NOT NULL,
    radius_miles   INTEGER     NOT NULL,
    test_mode      BOOLEAN     NOT NULL,
    items_found    INTEGER     DEFAULT 0,
    items_inserted INTEGER     DEFAULT 0,
    errors         INTEGER     DEFAULT 0,
    status         VARCHAR(50) DEFAULT 'running',
    sys_run_name   VARCHAR(255) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scrape_runs_sys_run_name
    ON bronze.scrape_runs (sys_run_name);


-- Create the monthly partition covering p_month, if it is not already there.
-- Called before every insert batch, so a run that crosses a month boundary
-- never fails on a missing partition.
CREATE OR REPLACE FUNCTION bronze.ensure_month_partition(p_month DATE)
RETURNS TEXT AS $$
DECLARE
    v_start DATE := date_trunc('month', p_month)::DATE;
    v_end   DATE := (date_trunc('month', p_month) + INTERVAL '1 month')::DATE;
    v_name  TEXT := 'raw_auction_items_' || to_char(v_start, 'YYYY_MM');
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'bronze' AND c.relname = v_name
    ) THEN
        EXECUTE format(
            'CREATE TABLE bronze.%I PARTITION OF bronze.raw_auction_items '
            'FOR VALUES FROM (%L) TO (%L)',
            v_name, v_start, v_end
        );
    END IF;
    RETURN v_name;
END;
$$ LANGUAGE plpgsql;


-- Retention. Drops every whole partition older than the cutoff month and
-- returns what it dropped, so the caller can log it.
--
-- Only partitions entirely older than the cutoff go, so the current month is
-- never touched part-way through.
CREATE OR REPLACE FUNCTION bronze.drop_partitions_before(p_cutoff DATE)
RETURNS TABLE (dropped_partition TEXT) AS $$
DECLARE
    r        RECORD;
    v_cutoff DATE := date_trunc('month', p_cutoff)::DATE;
BEGIN
    FOR r IN
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_inherits i ON i.inhrelid = c.oid
        JOIN pg_class p    ON p.oid = i.inhparent
        WHERE n.nspname = 'bronze'
          AND p.relname = 'raw_auction_items'
          AND c.relname ~ '^raw_auction_items_[0-9]{4}_[0-9]{2}$'
        ORDER BY c.relname
    LOOP
        IF to_date(right(r.relname, 7), 'YYYY_MM') < v_cutoff THEN
            EXECUTE format('DROP TABLE bronze.%I', r.relname);
            dropped_partition := r.relname;
            RETURN NEXT;
        END IF;
    END LOOP;
END;
$$ LANGUAGE plpgsql;


-- Companion cleanup for the un-partitioned run table.
CREATE OR REPLACE FUNCTION bronze.delete_runs_before(p_cutoff DATE)
RETURNS BIGINT AS $$
DECLARE
    v_deleted BIGINT;
BEGIN
    DELETE FROM bronze.scrape_runs WHERE started_at < p_cutoff;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN v_deleted;
END;
$$ LANGUAGE plpgsql;
