-- Raw layer: one landing table per source.
--
-- Sources have nothing in common but "a job produced this payload", so forcing
-- them into a shared table would mean a lowest-common-denominator schema that
-- fits none of them. A table each keeps every source honest about its own
-- shape, and adding a source touches nothing that already exists.
--
-- Each landing table holds only what is genuinely per-row: the job that
-- produced it, the item, and the payload. Scrape parameters like zip code and
-- radius are constants for a whole run and live on raw.scrape_runs instead of
-- being repeated on every row.

CREATE SCHEMA IF NOT EXISTS raw;


-- Run bookkeeping, shared across sources.
CREATE TABLE IF NOT EXISTS raw.scrape_runs (
    id             SERIAL PRIMARY KEY,
    source         VARCHAR(50)  NOT NULL,
    sys_run_name   VARCHAR(255) NOT NULL,
    started_at     TIMESTAMPTZ  NOT NULL,
    completed_at   TIMESTAMPTZ,
    status         VARCHAR(50)  DEFAULT 'running',
    -- Nullable: meaningful for a radius search, not for a single-warehouse
    -- seller where every lot is in the same place.
    zip_code       VARCHAR(20),
    radius_miles   INTEGER,
    test_mode      BOOLEAN      NOT NULL DEFAULT false,
    items_found    INTEGER      DEFAULT 0,
    items_inserted INTEGER      DEFAULT 0,
    errors         INTEGER      DEFAULT 0,
    CONSTRAINT scrape_runs_source_run_key UNIQUE (source, sys_run_name)
);

CREATE INDEX IF NOT EXISTS idx_scrape_runs_source_status
    ON raw.scrape_runs (source, status);


-- Create the monthly partition covering p_month for any source table.
CREATE OR REPLACE FUNCTION raw.ensure_month_partition(p_table TEXT, p_month DATE)
RETURNS TEXT AS $$
DECLARE
    v_start DATE := date_trunc('month', p_month)::DATE;
    v_end   DATE := (date_trunc('month', p_month) + INTERVAL '1 month')::DATE;
    v_name  TEXT := p_table || '_' || to_char(v_start, 'YYYY_MM');
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'raw' AND c.relname = v_name
    ) THEN
        EXECUTE format(
            'CREATE TABLE raw.%I PARTITION OF raw.%I FOR VALUES FROM (%L) TO (%L)',
            v_name, p_table, v_start, v_end
        );
    END IF;
    RETURN v_name;
END;
$$ LANGUAGE plpgsql;


-- Retention for any source table: drops whole partitions older than the cutoff
-- month. A DROP TABLE per partition, so cost does not scale with row count.
CREATE OR REPLACE FUNCTION raw.drop_partitions_before(p_table TEXT, p_cutoff DATE)
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
        WHERE n.nspname = 'raw'
          AND p.relname = p_table
          AND c.relname ~ ('^' || p_table || '_[0-9]{4}_[0-9]{2}$')
        ORDER BY c.relname
    LOOP
        IF to_date(right(r.relname, 7), 'YYYY_MM') < v_cutoff THEN
            EXECUTE format('DROP TABLE raw.%I', r.relname);
            dropped_partition := r.relname;
            RETURN NEXT;
        END IF;
    END LOOP;
END;
$$ LANGUAGE plpgsql;


CREATE OR REPLACE FUNCTION raw.delete_runs_before(p_cutoff DATE)
RETURNS BIGINT AS $$
DECLARE
    v_deleted BIGINT;
BEGIN
    DELETE FROM raw.scrape_runs WHERE started_at < p_cutoff;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN v_deleted;
END;
$$ LANGUAGE plpgsql;


-- ---------------------------------------------------------------------------
-- Source: hibid
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.hibid (
    id           BIGSERIAL,
    sys_run_name VARCHAR(255) NOT NULL,
    item_id      VARCHAR(255) NOT NULL,
    -- Genuinely per-row: one run can sweep several categories, and this
    -- records which one yielded the item. NULL means "all open lots".
    category     VARCHAR(255),
    raw_json     JSONB        NOT NULL,
    scraped_at   TIMESTAMPTZ  NOT NULL,
    created_at   TIMESTAMPTZ  DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id, scraped_at)
) PARTITION BY RANGE (scraped_at);

CREATE INDEX IF NOT EXISTS idx_hibid_item_id      ON raw.hibid (item_id);
CREATE INDEX IF NOT EXISTS idx_hibid_scraped_at   ON raw.hibid (scraped_at);
CREATE INDEX IF NOT EXISTS idx_hibid_sys_run_name ON raw.hibid (sys_run_name);
CREATE INDEX IF NOT EXISTS idx_hibid_gin          ON raw.hibid USING GIN (raw_json);
