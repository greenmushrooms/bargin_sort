{{ config(materialized='view') }}

-- The scrape manifest: every discovered auction and how much of it we hold.
--
-- This is the orchestrator's ledger, and it is deliberately the only place the
-- rule "an auction is scraped once" is written down. The selection query reads
-- `scrape_state = 'pending'` rather than restating the condition, so what the
-- scheduler will do and what you can see are the same thing.
--
-- Capture is measured in *lots held*, not in runs completed. A large catalogue
-- cannot be taken in one pass — HiBid answers erratically on deep pages, so a
-- single run always stops somewhere short, and asking "did one run get 90%?"
-- makes the biggest auctions the least able to pass. A 4,296-lot catalogue
-- needs ~43 consecutive good pages; at 95% per-page reliability that is an 11%
-- chance of ever clearing the bar, so it retried forever at 28% coverage.
--
-- Counting distinct lots instead works because raw.hibid is append-only and
-- fct_lots dedupes on item_id: three runs banking a different 1,000 lots of the
-- same auction genuinely converge. Paired with raw.catalog_progress, which
-- resumes pagination where the last run stopped, successive partial passes add
-- up instead of re-reading the front of the catalogue.
--
-- Retention caveat: coverage is computed from raw, whose partitions are dropped
-- past the window, so a long-closed auction can fall back out of 'captured'.
-- Harmless — the scheduler only ever selects auctions that have not closed yet,
-- and those are far younger than the retention window.
--
-- A view, not a table. State changes while the orchestrator is running — every
-- auction it captures flips a row — so a table would be stale from the moment
-- it was built, and the run would re-select auctions it had just taken.

{% set completeness = var('catalog_completeness', 0.9) %}

with coverage as (

    -- How many distinct lots of each auction raw actually holds, across every
    -- run that ever touched it.
    select
        (raw_json -> 'auction_data' ->> 'id')::bigint as auction_id,
        count(distinct item_id)                       as lots_held,
        count(distinct sys_run_name)                  as passes,
        max(scraped_at)                               as last_captured_at
    from {{ source('raw', 'hibid') }}
    where raw_json -> 'auction_data' ->> 'id' is not null
    group by 1

),

attempts as (

    -- Runs are still tracked, but only to tell "never tried" from "tried and
    -- got nothing", which is the difference between pending and retry.
    select
        auction_id,
        count(*)                                                   as scrape_attempts,
        count(*) filter (where status = 'completed' and not test_mode) as completed_runs
    from {{ source('raw', 'scrape_runs') }}
    where source = 'hibid'
      and auction_id is not null
      and not test_mode
    group by auction_id

)

select
    s.auction_id,
    s.event_name,
    s.event_city,
    s.distance_km,
    s.lot_count,
    s.close_at,
    s.opens_at,
    s.auction_status,
    s.is_within_radius,

    coalesce(c.lots_held, 0)        as lots_held,
    coalesce(c.passes, 0)           as passes,
    c.last_captured_at,
    coalesce(a.scrape_attempts, 0)  as scrape_attempts,

    -- Null lot_count means discovery could not say how big the auction is, so
    -- coverage is unknowable rather than zero.
    round(
        100.0 * coalesce(c.lots_held, 0) / nullif(s.lot_count, 0)
    )                               as coverage_pct,

    p.last_page                     as resume_page,

    round(
        extract(epoch from (s.close_at - now())) / 3600.0
    )::int                          as hours_to_close,

    case
        -- Out of scope before anything else: an auction we will never take is
        -- not "pending" and must never read as a gap.
        when not s.is_within_radius then 'out_of_radius'

        -- Enough of it is held, however many passes that took.
        when s.lot_count > 0
             and coalesce(c.lots_held, 0) >= s.lot_count * {{ completeness }}
            then 'captured'

        -- Closed while still short. Nothing can be done about these now, which
        -- is exactly why they are worth being able to count.
        when s.close_at <= now() then 'missed'

        -- Tried before and still short, so there is a resume point to continue
        -- from. Rides the same selection window as a fresh auction.
        when coalesce(a.scrape_attempts, 0) > 0 then 'retry'

        else 'pending'
    end                             as scrape_state

from {{ ref('auction_schedule') }} s
left join coverage c on c.auction_id = s.auction_id
left join attempts a on a.auction_id = s.auction_id
left join {{ source('raw', 'catalog_progress') }} p on p.auction_id = s.auction_id
