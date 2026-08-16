{{
    config(
        materialized='incremental',
        unique_key='sys_run_name',
        incremental_strategy='delete+insert',
    )
}}

-- One row per lot per scrape run: bronze JSONB pulled apart into typed columns.
--
-- Incremental on sys_run_name, in three modes:
--   * `--vars '{target_run: <name>}'` processes exactly that run. The scrape
--     flow passes the job id it just finished, so a transform costs one run's
--     worth of work no matter how much history bronze holds.
--   * no var: picks up every run not already here (catch-up after a failure).
--   * `--full-refresh`: replays all of bronze from scratch.
--
-- delete+insert on sys_run_name makes all three idempotent — re-running a job
-- id replaces its slice rather than duplicating it.
--
-- Only completed runs are admitted. A run that died mid-flight left a partial
-- slice of a catalogue behind, and letting that into silver would look like
-- lots genuinely disappearing.

{% set target_run = var('target_run', none) %}

with completed_runs as (

    select sys_run_name
    from {{ source('bronze', 'scrape_runs') }}
    where status = 'completed'
    {% if target_run %}
      and sys_run_name = {{ dbt.string_literal(target_run) }}
    {% endif %}

),

source as (

    select r.*
    from {{ source('bronze', 'raw_auction_items') }} r
    inner join completed_runs c on c.sys_run_name = r.sys_run_name

    {% if is_incremental() and not target_run %}
    where r.sys_run_name not in (select sys_run_name from {{ this }})
    {% endif %}

),

renamed as (

    select
        item_id,
        sys_run_name,
        scraped_at,
        zip_code                                        as search_zip_code,
        radius_miles                                    as search_radius_miles,
        category                                        as search_category,

        (raw_json ->> 'id')::bigint                     as lot_id,
        raw_json ->> 'itemId'                           as hibid_item_id,
        raw_json ->> 'lotNumber'                        as lot_number,
        raw_json ->> 'lead'                             as title,
        raw_json ->> 'description'                      as description,

        nullif(raw_json ->> 'bidAmount', '')::numeric   as current_bid,
        nullif(raw_json ->> 'bidQuantity', '')::int     as bid_quantity,
        nullif(raw_json ->> 'quantity', '')::int        as quantity,
        raw_json ->> 'estimate'                         as estimate_text,

        (raw_json ->> 'shippingOffered')::boolean       as shipping_offered,
        nullif(raw_json ->> 'pictureCount', '')::int    as picture_count,

        -- Present in the schema but never populated by the search endpoint;
        -- real distance is derived in silver_enhanced from postal centroids.
        nullif(raw_json ->> 'distanceMiles', '')::numeric as reported_distance_miles,

        (raw_json -> 'auction_data' ->> 'id')::bigint   as auction_id,

        raw_json                                        as raw_json

    from source

)

select * from renamed
