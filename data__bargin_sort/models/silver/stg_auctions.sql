-- One row per auction, deduplicated across every lot and run that mentions it.
--
-- The auction is repeated inside each lot's payload, so this collapses to the
-- most recently scraped copy of each auction. Rebuilt in full on every run,
-- which keeps it correct after raw partitions are dropped.

with lots as (

    select
        (raw_json -> 'auction_data' ->> 'id')::bigint as auction_id,
        raw_json -> 'auction_data'                    as auction,
        scraped_at
    from {{ ref('stg_hibid_lots') }}
    where raw_json -> 'auction_data' ->> 'id' is not null

),

ranked as (

    select
        *,
        row_number() over (
            partition by auction_id
            order by scraped_at desc
        ) as recency_rank
    from lots

),

latest as (

    select * from ranked where recency_rank = 1

)

select
    auction_id,
    auction ->> 'eventName'                          as event_name,
    auction ->> 'description'                        as description,
    auction ->> 'eventAddress'                       as event_address,
    auction ->> 'eventCity'                          as event_city,
    upper(auction ->> 'eventState')                  as event_state,
    auction ->> 'eventZip'                           as event_postal_raw,

    {{ normalize_postal("auction ->> 'eventZip'") }}  as event_postal_key,
    {{ postal_country("auction ->> 'eventZip'") }}    as event_country_code,

    -- eventDateEnd is a naive local string, exactly like the bidCloseDateTime
    -- that stg_hibid_auctions is careful to name `bid_close_local`. Read as
    -- UTC it lands up to 46 hours early, because it is the event's end DATE at
    -- midnight rather than an instant: a lot closing 18:00 Toronto on the 25th
    -- was reported as having ended 00:00 UTC on the 24th, and every "has this
    -- closed" test downstream got the wrong answer. auction_schedule already
    -- applies this conversion to the sibling column; this is the same rule on
    -- the one fct_lots actually exposes.
    (nullif(auction ->> 'eventDateEnd', '')::timestamp
        at time zone 'America/Toronto')              as event_ends_at,

    -- When bidding actually closes, which is the question every consumer of
    -- this model is really asking. eventDateEnd above is the event's end DATE
    -- at midnight and runs ~19 hours early against it, so using it as a
    -- "has this closed" test hid 1,068 live lots from the triage site. Same
    -- naive-local parse and the same Toronto conversion as
    -- stg_hibid_auctions.bid_close_local, from the identical payload key.
    (nullif(auction ->> 'bidCloseDateTime', '')::timestamp
        at time zone 'America/Toronto')              as bid_close_at,
    nullif(auction ->> 'lotCount', '')::int           as lot_count,
    auction ->> 'bidType'                            as bid_type,
    auction ->> 'buyerPremium'                       as buyer_premium_text,
    auction ->> 'altBiddingUrl'                      as alt_bidding_url,

    -- Auctioneer arrives as an unresolved Apollo ref, e.g.
    -- {"__ref": "Auctioneer:148685"}. Keep the id so a future auctioneer
    -- dimension has something to join on.
    nullif(
        replace(auction -> 'auctioneer' ->> '__ref', 'Auctioneer:', ''), ''
    )::bigint                                        as auctioneer_id,

    scraped_at                                       as last_seen_at

from latest
