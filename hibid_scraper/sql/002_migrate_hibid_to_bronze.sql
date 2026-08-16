-- One-off migration of the original flat `hibid` schema into partitioned bronze.
--
-- Safe to re-run: it skips rows whose sys_run_name is already in bronze, and
-- does nothing at all once the old schema has been dropped.

DO $$
DECLARE
    v_month  DATE;
    v_moved  BIGINT;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'hibid' AND table_name = 'raw_auction_items'
    ) THEN
        RAISE NOTICE 'hibid.raw_auction_items is gone, nothing to migrate';
        RETURN;
    END IF;

    -- Every month present in the legacy data needs a partition to land in.
    FOR v_month IN
        SELECT DISTINCT date_trunc('month', scraped_at)::DATE
        FROM hibid.raw_auction_items
    LOOP
        PERFORM bronze.ensure_month_partition(v_month);
    END LOOP;

    INSERT INTO bronze.raw_auction_items
        (item_id, raw_json, scraped_at, zip_code, radius_miles, category,
         sys_run_name, created_at)
    SELECT h.item_id, h.raw_json, h.scraped_at, h.zip_code, h.radius_miles,
           h.category, h.sys_run_name, h.created_at
    FROM hibid.raw_auction_items h
    WHERE NOT EXISTS (
        SELECT 1 FROM bronze.raw_auction_items b
        WHERE b.sys_run_name = h.sys_run_name
          AND b.item_id      = h.item_id
          AND b.scraped_at   = h.scraped_at
    );
    GET DIAGNOSTICS v_moved = ROW_COUNT;
    RAISE NOTICE 'migrated % raw_auction_items rows', v_moved;

    INSERT INTO bronze.scrape_runs
        (started_at, completed_at, zip_code, radius_miles, test_mode,
         items_found, items_inserted, errors, status, sys_run_name)
    SELECT r.started_at, r.completed_at, r.zip_code, r.radius_miles, r.test_mode,
           r.items_found, r.items_inserted, r.errors, r.status, r.sys_run_name
    FROM hibid.scrape_runs r
    WHERE NOT EXISTS (
        SELECT 1 FROM bronze.scrape_runs b WHERE b.sys_run_name = r.sys_run_name
    );
    GET DIAGNOSTICS v_moved = ROW_COUNT;
    RAISE NOTICE 'migrated % scrape_runs rows', v_moved;
END $$;
