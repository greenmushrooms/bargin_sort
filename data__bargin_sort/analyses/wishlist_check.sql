-- Wishlist check across BOTH sources, live lots only.
--
-- DELIBERATELY AD HOC. This lives in analyses/ rather than models/ because it
-- is run by hand when someone asks "is there one right now", not on a schedule.
-- Promoting it to a model would imply the match rules are settled, and they are
-- not: every widening of the corpus has turned up another false positive —
-- "th{e ink}", IPX7 waterproof ratings, "Unifi{ed Minds}" Pokemon cards, X870
-- motherboards advertising WiFi 7, a Philips Hue UV sanitiser box, and the holy
-- {grail} of skincare. Seven rounds so far, none of them predictable from the
-- previous corpus.
--
-- The durable version of this is the extraction step, where a model reads the
-- listing and decides which noun is the product. Until then, hand-run.
--
--   psql "$BARGIN_DSN" -f analyses/wishlist_check.sql
--
-- Police Auctions was excluded before, which mattered: it is a single warehouse
-- with complete inventory and it is where every bike lives. A HiBid-only check
-- would have reported zero bikes while 41 sat on PAC.
-- Exclusions read the title plus the "Title:" line HiBid buries in the
-- description, not the title alone. The title column is truncated mid-word --
-- "New / Open Box - UL Listed... Sz 5.5x2.5mm" is a 120W laptop charger, and
-- "charger", already in that row's exclude_regex, survives only in the buried
-- line. Deliberately NOT the whole description: that haystack drops 19 of the
-- 54 current matches, because listings mention cases, mounts and cables
-- constantly -- it takes the gravel bike and nine of the headphones with it.

\pset format aligned

WITH live AS (
  SELECT f.source, f.title, f.high_bid, f.min_bid, f.bid_count, f.lot_url,
         round(f.distance_km) AS km,
         coalesce(m.close_at, f.event_ends_at) AS close_at,
         coalesce((SELECT h.raw_json ->> 'description' FROM raw.hibid h
                    WHERE h.item_id = f.item_id ORDER BY h.scraped_at DESC LIMIT 1), '') AS descr
  FROM silver_enhanced.fct_lots f
  LEFT JOIN silver_enhanced.auction_manifest m ON m.auction_id = f.auction_id
  WHERE f.recency_rank = 1
    AND coalesce(m.close_at, f.event_ends_at) > now()
    AND f.lot_status = 'OPEN'
), named AS (
  SELECT l.*,
         l.title || ' ' || coalesce(substring(l.descr from 'Title: ([^\n]*)'), '') AS full_title
  FROM live l
)
SELECT w.priority AS p, w.label, l.source,
       left(l.title, 44) AS title,
       l.high_bid AS bid, l.min_bid AS entry, l.bid_count AS bids, l.km,
       to_char(l.close_at AT TIME ZONE 'America/Toronto', 'Dy HH24:MI') AS closes,
       l.lot_url
FROM named l
JOIN reference.wishlist w
  ON  CASE w.match_scope WHEN 'title' THEN l.title ELSE l.title || ' ' || l.descr END ~* w.match_regex
  AND l.full_title !~* w.exclude_regex
ORDER BY w.priority, l.min_bid
LIMIT 40;
