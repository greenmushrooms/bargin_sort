package lots

// liveLotsCTE is the definition of "live", ported from lots.py, which was in
// turn ported from analyses/wishlist_check.sql -- the query a human runs to ask
// "is there one right now". Keeping all three in step is deliberate: a page
// that disagrees with the hand-run check is worse than not having one.
//
// Three conditions make a lot live and all three are load-bearing.
// recency_rank = 1 takes the newest observation of each lot rather than every
// scrape of it; lot_status = 'OPEN' is the lot's own state; and the close time
// is checked separately because a lot can still read OPEN in the last capture
// of an auction that has since finished -- the scrape saw it open and nothing
// has looked since.
//
// A raw string literal on purpose: the `[^\n]` below has to reach Postgres as a
// backslash and an n, not as a newline.
const liveLotsCTE = `
live AS (
    SELECT
        f.source, f.item_id, f.sys_run_name, f.title, f.lot_url,
        f.high_bid, f.min_bid, f.bid_count, f.event_name, f.event_city,
        round(f.distance_km)::int                   AS km,
        -- A refreshed close time outranks both stored ones: it was read off
        -- the lot's own page, the others are whatever the last sweep saw.
        coalesce(snap.close_at, m.close_at, f.event_ends_at) AS close_at,
        (snap.close_at IS NULL
         AND coalesce(m.close_at, f.event_ends_at) <= now()) AS stale_close
    FROM silver_enhanced.fct_lots f
    LEFT JOIN silver_enhanced.auction_manifest m ON m.auction_id = f.auction_id
    LEFT JOIN web.lot_snapshot snap
           ON snap.source = f.source AND snap.item_id = f.item_id
    WHERE f.recency_rank = 1
      AND f.lot_status = 'OPEN'

      -- A refresh that saw CLOSED ends the argument. It read the lot's own
      -- page, which no stored close time can outvote.
      AND (snap.lot_status IS NULL OR snap.lot_status <> 'CLOSED')

      -- Otherwise a one-hour grace window rather than a hard cutoff at now().
      -- HiBid extends auctions and every stored close is a snapshot from the
      -- last scrape, so a lot extended after it looks closed and gets dropped:
      -- on 2026-08-24 that hid a WIWU iPad Pro at zero bids with seventy
      -- minutes left and a Jetson e-bike still taking money, both still
      -- reading OPEN with only the stored close lapsed.
      --
      -- An hour, not six: six kept genuinely finished auctions on the page
      -- long after they ended. Lots inside the window carry stale_close so the
      -- UI flags them as a question rather than as confidently live.
      AND coalesce(snap.close_at, m.close_at, f.event_ends_at)
            > now() - interval '1 hour'
),
texts AS (
    SELECT
        l.*,
        coalesce(h.description, '')                 AS descr,
        -- Police Auctions publishes one thumbnail per listing; HiBid ships a
        -- gallery, of which the featured picture is the one the site leads
        -- with. Either way this is the small image -- the full-size set is
        -- only pulled for the detail view.
        CASE l.source
            WHEN 'hibid' THEN coalesce(
                h.raw_json -> 'featuredPicture' ->> 'thumbnailLocation',
                h.raw_json -> 'pictures' -> 0 ->> 'thumbnailLocation',
                h.raw_json -> 'pictures' -> 0 ->> 'fullSizeLocation')
            ELSE p.raw_json ->> 'image_url'
        END                                         AS thumbnail_url
    FROM live l
    LEFT JOIN silver.stg_hibid_lots h
           ON l.source = 'hibid'
          AND h.item_id = l.item_id
          AND h.sys_run_name = l.sys_run_name
    LEFT JOIN silver.stg_police_lots p
           ON l.source = 'police_auctions'
          AND p.item_id = l.item_id
          AND p.sys_run_name = l.sys_run_name
),
named AS (
    -- Exclusions read the title plus the "Title:" line HiBid buries in the
    -- description, not the whole description. The title column truncates
    -- mid-word, while matching exclusions against the full description drops
    -- 19 of 54 real matches because listings mention cases and cables
    -- constantly.
    SELECT t.*,
           t.title || ' ' ||
             coalesce(substring(t.descr from 'Title: ([^\n]*)'), '') AS full_title
    FROM texts t
)`
