-- One row per auction, deduplicated across every lot and run that mentions it.
--
-- The auction is repeated inside each lot's payload, so this collapses to the
-- most recently scraped copy of each auction. Rebuilt in full on every run,
-- which keeps it correct after bronze partitions are dropped.

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

    nullif(auction ->> 'eventDateEnd', '')::timestamp as event_ends_at,
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
