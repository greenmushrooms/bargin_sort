-- The queryable grain: current state of every lot across every source, with a
-- real distance.
--
-- HiBid's radius filter only offers whole miles (25 ≈ 40km, 50 ≈ 80km), so an
-- exact "within 50 km" is not expressible at scrape time. Raw therefore
-- collects the wider net and the precise cutoff is applied here, where it can
-- be changed without re-scraping anything.
--
-- Distance is centroid-to-centroid: Canadian postal data is only published at
-- FSA level, so both ends are accurate to roughly 5-15 km. That is fine for a
-- 50 km screen and deliberately not exposed as a precise figure.
--
-- The two sources are conformed before anything else happens, so distance, the
-- 50 km view and any search work across both without knowing the origin site.

with hibid_lots as (

    select
        l.source,
        l.item_id,
        l.lot_id,
        l.title,
        -- HiBid exposes no canonical lot URL in the payload, but /lot/<id>
        -- resolves to the lot page (verified against live lots). Police
        -- Auctions publishes the link directly, so both arrive comparable.
        'https://hibid.com/lot/' || l.lot_id            as lot_url,
        l.high_bid,
        l.min_bid,
        l.bid_count,
        l.lot_status,
        l.is_closed,
        l.time_left,
        l.sys_run_name,
        l.scraped_at,
        l.search_zip_code,
        l.search_category,

        a.auction_id,
        a.event_name,
        a.event_city,
        a.event_state,
        a.event_country_code,
        a.event_postal_key,
        a.event_ends_at

    from {{ ref('stg_hibid_lots') }} l
    left join {{ ref('stg_auctions') }} a on a.auction_id = l.auction_id

),

police_lots as (

    -- One warehouse, so the origin of a distance is the warehouse itself and
    -- every lot sits at zero from it. The search origin is still the user's
    -- postal code, which is what makes the two sources comparable.
    select
        source,
        item_id,
        lot_id,
        title,
        lot_url,
        high_bid,
        min_bid,
        null::bigint     as bid_count,      -- not published per listing
        lot_status,
        is_closed,
        null::varchar    as time_left,      -- ends_at is absolute, not a countdown
        sys_run_name,
        scraped_at,
        {{ var('home_postal', "'m8w3b7'") }} as search_zip_code,
        null::varchar    as search_category,

        null::bigint     as auction_id,     -- no auction grouping; one seller
        seller           as event_name,
        event_city,
        event_state,
        event_country_code,
        event_postal_key,
        ends_at          as event_ends_at

    from {{ ref('stg_police_lots') }}

),

unioned as (

    select * from hibid_lots
    union all
    select * from police_lots

),

latest_observation as (

    select
        *,
        row_number() over (
            partition by source, item_id
            order by scraped_at desc
        ) as recency_rank
    from unioned

),

current_lots as (

    select * from latest_observation where recency_rank = 1

),

located as (

    select
        l.*,
        o.latitude  as origin_latitude,
        o.longitude as origin_longitude,
        e.latitude  as event_latitude,
        e.longitude as event_longitude

    from current_lots l

    -- Where the search was run from, geocoded the same way as the lots so the
    -- two ends of the distance are directly comparable.
    left join {{ ref('postal_centroids') }} o
        on  o.postal_code  = {{ normalize_postal('l.search_zip_code') }}
        and o.country_code = {{ postal_country('l.search_zip_code') }}

    left join {{ ref('postal_centroids') }} e
        on  e.postal_code  = l.event_postal_key
        and e.country_code = l.event_country_code

)

select
    *,
    case
        when origin_latitude is not null and event_latitude is not null
        then round(
            (
                earth_distance(
                    ll_to_earth(origin_latitude, origin_longitude),
                    ll_to_earth(event_latitude, event_longitude)
                ) / 1000.0
            )::numeric,
            1
        )
    end as distance_km

from located
