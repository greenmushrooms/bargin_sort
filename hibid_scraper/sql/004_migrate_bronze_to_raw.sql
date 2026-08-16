-- Move the single-source bronze layer into the per-source raw layer.
--
-- zip_code and radius_miles are dropped from the item rows: they are constants
-- for a whole run and are already on scrape_runs, where a second source can
-- leave them null without the column being meaningless.
--
-- Safe to re-run: rows already present are skipped, and it no-ops once bronze
-- has been dropped.

DO $$
DECLARE
    v_month DATE;
    v_moved BIGINT;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'bronze' AND table_name = 'raw_auction_items'
    ) THEN
        RAISE NOTICE 'bronze.raw_auction_items is gone, nothing to migrate';
        RETURN;
    END IF;

    -- Runs first: raw.hibid rows are only admitted downstream if their run is
    -- present, and this is where the per-run parameters land.
    INSERT INTO raw.scrape_runs
        (source, sys_run_name, started_at, completed_at, status,
         zip_code, radius_miles, test_mode, items_found, items_inserted, errors)
    SELECT 'hibid', b.sys_run_name, b.started_at, b.completed_at, b.status,
           b.zip_code, b.radius_miles, b.test_mode,
           b.items_found, b.items_inserted, b.errors
    FROM bronze.scrape_runs b
    WHERE NOT EXISTS (
        SELECT 1 FROM raw.scrape_runs r
        WHERE r.source = 'hibid' AND r.sys_run_name = b.sys_run_name
    );
    GET DIAGNOSTICS v_moved = ROW_COUNT;
    RAISE NOTICE 'migrated % scrape_runs rows', v_moved;

    FOR v_month IN
        SELECT DISTINCT date_trunc('month', scraped_at)::DATE
        FROM bronze.raw_auction_items
    LOOP
        PERFORM raw.ensure_month_partition('hibid', v_month);
    END LOOP;

    INSERT INTO raw.hibid
        (sys_run_name, item_id, category, raw_json, scraped_at, created_at)
    SELECT b.sys_run_name, b.item_id, b.category, b.raw_json,
           b.scraped_at, b.created_at
    FROM bronze.raw_auction_items b
    WHERE NOT EXISTS (
        SELECT 1 FROM raw.hibid h
        WHERE h.sys_run_name = b.sys_run_name
          AND h.item_id      = b.item_id
          AND h.scraped_at   = b.scraped_at
    );
    GET DIAGNOSTICS v_moved = ROW_COUNT;
    RAISE NOTICE 'migrated % hibid rows', v_moved;
END $$;
