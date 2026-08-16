{{
    config(
        materialized='incremental',
        unique_key='sys_run_name',
        incremental_strategy='delete+insert',
    )
}}

-- One row per listing per completed scrape of Police Auctions Canada.
--
-- Same incremental contract as stg_hibid_lots: scoped by target_run, catching
-- up when no var is given, replayable in full with --full-refresh.
--
-- The site is a single warehouse, so location is a constant rather than a
-- per-listing field. It is emitted anyway so fct_lots can treat both sources
-- identically and geocode them the same way.

{% set target_run = var('target_run', none) %}

with completed_runs as (

    select sys_run_name
    from {{ source('raw', 'scrape_runs') }}
    where source = 'police_auctions'
      and status = 'completed'
    {% if target_run %}
      and sys_run_name = {{ dbt.string_literal(target_run) }}
    {% endif %}

),

source as (

    select r.*
    from {{ source('raw', 'police_auctions') }} r
    inner join completed_runs c on c.sys_run_name = r.sys_run_name

    {% if is_incremental() and not target_run %}
    where r.sys_run_name not in (select sys_run_name from {{ this }})
    {% endif %}

),

renamed as (

    select
        'police_auctions'::varchar                       as source,
        item_id,
        sys_run_name,
        scraped_at,

        (raw_json ->> 'listing_id')::bigint              as lot_id,
        raw_json ->> 'title'                             as title,
        raw_json ->> 'seller'                            as seller,
        raw_json ->> 'url'                               as lot_url,
        raw_json ->> 'image_url'                         as image_url,

        -- The site's "current price" is the standing high bid, and its
        -- "minimum bid" is what the next bid must be — the same two figures
        -- HiBid exposes as highBid and minBid.
        {{ safe_numeric("raw_json ->> 'current_price'") }} as high_bid,
        {{ safe_numeric("raw_json ->> 'minimum_bid'") }}   as min_bid,

        (raw_json ->> 'has_ended')::boolean              as is_closed,
        case
            when (raw_json ->> 'has_ended')::boolean then 'CLOSED'
            else 'OPEN'
        end                                              as lot_status,

        -- Rendered as US-style MM/DD/YYYY by the site.
        to_timestamp(raw_json ->> 'ends_at', 'MM/DD/YYYY HH24:MI:SS')
                                                         as ends_at,

        raw_json -> 'location' ->> 'city'                as event_city,
        raw_json -> 'location' ->> 'province'            as event_state,
        raw_json -> 'location' ->> 'country_code'        as event_country_code,
        {{ normalize_postal("raw_json -> 'location' ->> 'postal_code'") }}
                                                         as event_postal_key,

        raw_json                                         as raw_json

    from source

)

select * from renamed
