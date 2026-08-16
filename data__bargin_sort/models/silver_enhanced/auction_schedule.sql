-- What the orchestrator schedules against: every discovered auction, placed in
-- space and time.
--
-- Two things happen here that silver cannot do on its own, both needing the
-- postal centroid seed:
--
--   * distance_km — HiBid publishes no distance at all (`distanceMiles` is
--     null on every lot and every auction node), and honours its own radius
--     filter loosely, so the 50 km question is only answerable here.
--
--   * close_at — HiBid states close times as naive local time with no offset.
--     Reading one as an instant means knowing where the auction is.
--
-- Timezone: everything inside the 50 km screen is in Ontario, so
-- America/Toronto is the correct reading. Widening the radius past ~90 km
-- reaches Buffalo and would need a per-state mapping before close_at could be
-- trusted; `is_within_radius` is the guard that keeps that honest today.

with auctions as (

    select * from {{ ref('stg_hibid_auctions') }}

),

located as (

    select
        a.*,
        o.latitude  as origin_latitude,
        o.longitude as origin_longitude,
        e.latitude  as event_latitude,
        e.longitude as event_longitude

    from auctions a

    -- Home, geocoded the same way as the auctions so both ends of the distance
    -- are comparable.
    left join {{ ref('postal_centroids') }} o
        on  o.postal_code  = {{ normalize_postal(var('home_postal', "'m8w3b7'")) }}
        and o.country_code = {{ postal_country(var('home_postal', "'m8w3b7'")) }}

    left join {{ ref('postal_centroids') }} e
        on  e.postal_code  = a.event_postal_key
        and e.country_code = a.event_country_code

),

measured as (

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
        end as distance_km,

        bid_close_local at time zone 'America/Toronto' as close_at,
        bid_open_local  at time zone 'America/Toronto' as opens_at

    from located

)

select
    *,
    -- A null distance is unknown, not near — same rule as lots_within_50km.
    -- An auction whose postal code did not geocode is deliberately left out of
    -- the schedule rather than scraped on the assumption it is local.
    coalesce(distance_km <= {{ var('radius_km', 50) }}, false) as is_within_radius

from measured
