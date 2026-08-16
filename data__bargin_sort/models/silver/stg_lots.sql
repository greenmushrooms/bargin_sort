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

search_results as (

    -- Rows scraped before the extractor was restricted to lotSearch results
    -- include featured and related lots from other auctions. They carry a stub
    -- auction with no id and no location, so they can never be placed in a
    -- radius. They are inventory from somewhere else entirely, not near-misses.
    -- Bronze keeps them; silver is the layer that conforms to "what we searched".
    select *
    from source
    where raw_json -> 'auction_data' ->> 'id' is not null

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

        -- `bidAmount` on the Lot object is a fixed 123.45 placeholder on both
        -- the search endpoint and the lot page. The live figures are on the
        -- inline lotState, which is what anything price-related must use.
        {{ safe_numeric("raw_json -> 'lotState' ->> 'highBid'") }}  as high_bid,
        {{ safe_numeric("raw_json -> 'lotState' ->> 'minBid'") }}   as min_bid,
        {{ safe_numeric("raw_json -> 'lotState' ->> 'buyNow'") }}   as buy_now,
        {{ safe_int("raw_json -> 'lotState' ->> 'bidCount'") }}     as bid_count,
        raw_json -> 'lotState' ->> 'status'                        as lot_status,
        (raw_json -> 'lotState' ->> 'isClosed')::boolean           as is_closed,
        raw_json -> 'lotState' ->> 'timeLeft'                      as time_left,
        {{ safe_numeric("raw_json -> 'lotState' ->> 'timeLeftSeconds'") }}
                                                                   as time_left_seconds,

        -- Free text in practice (" x 2"), so keep both readings.
        raw_json ->> 'bidQuantity'                      as bid_quantity_raw,
        {{ safe_int("raw_json ->> 'bidQuantity'") }}    as bid_quantity,
        {{ safe_int("raw_json ->> 'quantity'") }}       as quantity,
        raw_json ->> 'estimate'                         as estimate_text,

        (raw_json ->> 'shippingOffered')::boolean       as shipping_offered,
        {{ safe_int("raw_json ->> 'pictureCount'") }}   as picture_count,

        -- Present in the schema but never populated by the search endpoint;
        -- real distance is derived in silver_enhanced from postal centroids.
        nullif(raw_json ->> 'distanceMiles', '')::numeric as reported_distance_miles,

        (raw_json -> 'auction_data' ->> 'id')::bigint   as auction_id,

        raw_json                                        as raw_json

    from search_results

)

select * from renamed
