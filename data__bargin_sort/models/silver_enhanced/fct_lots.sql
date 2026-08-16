-- The queryable grain: current state of every lot, with a real distance.
--
-- HiBid's own radius filter only offers whole miles (25 ≈ 40km, 50 ≈ 80km), so
-- an exact "within 50 km" is not expressible at scrape time. Bronze therefore
-- collects the wider 50-mile net and the precise cutoff is applied here, where
-- it can be changed without re-scraping anything.
--
-- Distance is centroid-to-centroid: Canadian postal data is only published at
-- FSA level, so both ends are accurate to roughly 5-15 km. That is fine for a
-- 50 km screen and deliberately not exposed as a precise figure.

with latest_observation as (

    select
        *,
        row_number() over (
            partition by item_id
            order by scraped_at desc
        ) as recency_rank
    from {{ ref('stg_hibid_lots') }}

),

current_lots as (

    select * from latest_observation where recency_rank = 1

),

origin as (

    -- Where the search was run from, geocoded the same way as the lots so the
    -- two ends of the distance are directly comparable.
    select
        l.item_id,
        c.latitude  as origin_latitude,
        c.longitude as origin_longitude
    from current_lots l
    left join {{ ref('postal_centroids') }} c
        on  c.postal_code  = {{ normalize_postal('l.search_zip_code') }}
        and c.country_code = {{ postal_country('l.search_zip_code') }}

),

joined as (

    select
        l.source,
        l.item_id,
        l.lot_id,
        l.lot_number,
        l.title,
        l.description,
        l.high_bid,
        l.min_bid,
        l.buy_now,
        l.bid_count,
        l.lot_status,
        l.is_closed,
        l.time_left,
        l.time_left_seconds,
        l.bid_quantity,
        l.quantity,
        l.estimate_text,
        l.shipping_offered,
        l.picture_count,
        l.scraped_at            as last_seen_at,
        l.sys_run_name          as last_seen_run,
        l.search_category,

        a.auction_id,
        a.event_name,
        a.event_city,
        a.event_state,
        a.event_country_code,
        a.event_postal_key,
        a.event_ends_at,
        a.bid_type,
        a.buyer_premium_text,
        a.alt_bidding_url,
        a.auctioneer_id,

        o.origin_latitude,
        o.origin_longitude,
        e.latitude              as event_latitude,
        e.longitude             as event_longitude

    from current_lots l
    left join {{ ref('stg_auctions') }} a on a.auction_id = l.auction_id
    left join origin o                    on o.item_id    = l.item_id
    left join {{ ref('postal_centroids') }} e
        on  e.postal_code  = a.event_postal_key
        and e.country_code = a.event_country_code

)

select
    *,

    -- HiBid exposes no canonical lot URL in the payload, but /lot/<id>
    -- resolves to the lot page (verified against live lots).
    'https://hibid.com/lot/' || lot_id                as lot_url,

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

from joined
