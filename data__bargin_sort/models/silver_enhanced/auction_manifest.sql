{{ config(materialized='view') }}

-- The scrape manifest: every discovered auction and whether it has been taken.
--
-- This is the orchestrator's ledger, and it is deliberately the only place the
-- rule "an auction is scraped once" is written down. The selection query reads
-- `scrape_state = 'pending'` rather than restating the condition, so what the
-- scheduler will do and what you can see are the same thing.
--
-- A view, not a table. State changes while the orchestrator is running — every
-- auction it captures flips a row — so a table would be stale from the moment
-- it was built, and the run would re-select auctions it had just taken.
--
-- raw.scrape_runs is the log this reads. It is written in the same transaction
-- as the run bookkeeping, so it cannot drift from what actually landed, and it
-- survives the container that produced it.

with captures as (

    -- Completed, non-test runs only. A test run stops after test_limit lots,
    -- so counting it would retire an auction having seen a handful of it.
    select
        auction_id,
        count(*)                          as attempts,
        count(*) filter (
            where status = 'completed' and not test_mode
        )                                 as captures,
        max(completed_at) filter (
            where status = 'completed' and not test_mode
        )                                 as captured_at,
        max(items_inserted) filter (
            where status = 'completed' and not test_mode
        )                                 as captured_lots,
        max(sys_run_name) filter (
            where status = 'completed' and not test_mode
        )                                 as captured_by_run
    from {{ source('raw', 'scrape_runs') }}
    where source = 'hibid'
      and auction_id is not null
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

    coalesce(c.attempts, 0)  as scrape_attempts,
    c.captured_at,
    c.captured_lots,
    c.captured_by_run,

    round(
        extract(epoch from (s.close_at - now())) / 3600.0
    )::int                   as hours_to_close,

    case
        -- Out of scope before anything else: an auction we will never take is
        -- not "pending" and must never read as a gap.
        when not s.is_within_radius            then 'out_of_radius'
        when coalesce(c.captures, 0) > 0       then 'captured'
        -- Closed without a capture. Nothing can be done about these now, which
        -- is exactly why they are worth being able to count.
        when s.close_at <= now()               then 'missed'
        -- Attempted and not captured: the run failed, so it is still due. The
        -- scraper fails a catalogue rather than retiring it on a partial read,
        -- and this is where that shows up.
        when coalesce(c.attempts, 0) > 0       then 'retry'
        else 'pending'
    end                      as scrape_state

from {{ ref('auction_schedule') }} s
left join captures c on c.auction_id = s.auction_id
