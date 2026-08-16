-- One row per auction, from the auction-level discovery feed.
--
-- Distinct from `stg_auctions`, which reconstructs auctions from the copy
-- embedded in each lot payload. This one comes from /auctions directly, so it
-- knows about auctions before any of their lots have been scraped — which is
-- the whole point, since that is what the orchestrator schedules against.
--
-- Rebuilt in full each run rather than incrementally: the feed is ~100 rows per
-- discovery pass, so replaying it is cheaper than reasoning about watermarks,
-- and it stays correct after raw partitions are dropped.
--
-- Every run is admitted, not just completed ones. A discovery pass that died
-- half way still found real auctions, and taking the latest row per auction
-- means a partial run can only ever leave some auctions stale — never missing.

with observations as (

    select
        (item_id)::bigint as auction_id,
        raw_json          as auction,
        sys_run_name,
        scraped_at
    from {{ source('raw', 'hibid_auctions') }}

),

ranked as (

    select
        *,
        row_number() over (
            partition by auction_id
            order by scraped_at desc
        ) as recency_rank
    from observations

),

latest as (

    select * from ranked where recency_rank = 1

)

select
    auction_id,
    auction ->> 'eventName'                             as event_name,
    auction ->> 'description'                           as description,
    auction ->> 'eventAddress'                          as event_address,
    auction ->> 'eventCity'                             as event_city,
    upper(auction ->> 'eventState')                     as event_state,
    auction ->> 'eventZip'                              as event_postal_raw,

    {{ normalize_postal("auction ->> 'eventZip'") }}     as event_postal_key,
    {{ postal_country("auction ->> 'eventZip'") }}       as event_country_code,

    -- Naive local time, no offset. Resolving it to an instant needs to know
    -- where the auction is, so that happens in silver_enhanced after the
    -- geocode, not here.
    nullif(auction ->> 'bidOpenDateTime',  '')::timestamp as bid_open_local,
    nullif(auction ->> 'bidCloseDateTime', '')::timestamp as bid_close_local,

    {{ safe_int("auction ->> 'lotCount'") }}             as lot_count,
    auction ->> 'bidType'                               as bid_type,

    auction -> 'auctionState'  ->> 'auctionStatus'      as auction_status,
    {{ safe_int("auction -> 'auctionState' ->> 'openLotCount'") }}
                                                        as open_lot_count,

    -- Pickup-only is the structural reason these auctions clear cheap: freight
    -- cost is what keeps out-of-town bidders away.
    auction -> 'auctionOptions' ->> 'shippingType'      as shipping_type,

    -- Auctioneer arrives as an unresolved Apollo ref, e.g.
    -- {"__ref": "Auctioneer:148685"}. Keep the id so a future auctioneer
    -- dimension has something to join on.
    nullif(
        replace(auction -> 'auctioneer' ->> '__ref', 'Auctioneer:', ''), ''
    )::bigint                                           as auctioneer_id,

    sys_run_name                                        as last_seen_run,
    scraped_at                                          as last_seen_at

from latest
