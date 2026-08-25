"""
Reads over the pipeline, and the wishlist match cache.

Everything in here treats `raw`, `silver` and `silver_enhanced` as read-only.
The only writes are to `web.*`, and only to the two tables this module owns:
`wishlist_match` and `match_build`.
"""

import logging
import time
from typing import Any, Optional

import db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The live-lot definition, shared by the matcher and the ad-hoc search.
# ---------------------------------------------------------------------------
#
# Ported from analyses/wishlist_check.sql, which is the query a human runs to
# ask "is there one right now". Keeping the two in step is deliberate: an alert
# or a page that disagrees with the hand-run check is worse than not having one.
#
# Three conditions make a lot live, and all three are load-bearing.
# `recency_rank = 1` takes the newest observation of each lot rather than every
# scrape of it; `lot_status = 'OPEN'` is the lot's own state; and the close time
# is checked because a lot can still read OPEN in the last capture of an auction
# that has since finished — the scrape saw it open and nothing has looked since.
#
# Descriptions come from `silver.stg_hibid_lots`, joined on the same
# (item_id, sys_run_name) the fact row was built from, rather than from a
# correlated subquery into partitioned bronze. Same text, one hash join instead
# of ~18,000 index lookups into a table whose partitions age out.
_LIVE_LOTS = """
live AS (
    SELECT
        f.source,
        f.item_id,
        f.sys_run_name,
        f.title,
        f.lot_url,
        f.high_bid,
        f.min_bid,
        f.bid_count,
        f.event_name,
        f.event_city,
        f.scraped_at,
        round(f.distance_km)                        AS km,
        -- A refreshed close time outranks both stored ones. It was read off
        -- the lot's own page seconds ago; the others are whatever the last
        -- sweep happened to see.
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
      -- page, which no stored close time can outvote, so the lot leaves the
      -- live set no matter how recent its scheduled close looks.
      AND (snap.lot_status IS NULL OR snap.lot_status <> 'CLOSED')

      -- Otherwise a one-hour grace window rather than a hard cutoff at now().
      -- HiBid extends auctions and every stored close is a snapshot taken
      -- when something last scraped, so a lot extended after that scrape
      -- looks closed and gets dropped: on 2026-08-24 that hid a WIWU iPad Pro
      -- at zero bids with seventy minutes left, and a Jetson e-bike still
      -- taking money. Both still read OPEN; only the stored close had lapsed.
      --
      -- An hour, not six: six kept genuinely finished auctions on the page
      -- long after they ended, which is its own kind of wrong. Lots inside
      -- the window carry stale_close so the UI flags them as a question
      -- rather than showing them as confidently live.
      AND coalesce(snap.close_at, m.close_at, f.event_ends_at)
            > now() - interval '1 hour'
),
texts AS (
    SELECT
        l.*,
        coalesce(h.description, '')                 AS descr,
        -- Police Auctions publishes one thumbnail per listing; HiBid ships a
        -- gallery, of which the featured picture is the one the site itself
        -- leads with. Either way this is the small image — the full-size set
        -- is only pulled for the detail view.
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
    -- mid-word — "New / Open Box - UL Listed... Sz 5.5x2.5mm" is a laptop
    -- charger, and the word "charger" survives only in the buried line — while
    -- matching exclusions against the full description drops 19 of 54 real
    -- matches, because listings mention cases, mounts and cables constantly.
    --
    -- The raw string on the pattern matters: `[^\\n]` has to reach Postgres as
    -- a backslash and an n.
    SELECT t.*,
           t.title || ' ' ||
             coalesce(substring(t.descr from 'Title: ([^\\n]*)'), '') AS full_title
    FROM texts t
)
"""


# ---------------------------------------------------------------------------
# Match cache
# ---------------------------------------------------------------------------

_REBUILD_SQL = f"""
WITH {_LIVE_LOTS}
INSERT INTO web.wishlist_match (
    slug, source, item_id, label, priority, max_bid_cad,
    title, lot_url, high_bid, min_bid, bid_count, km, close_at,
    event_name, event_city, thumbnail_url, within_budget,
    brand, brand_tier, condition_grade)
SELECT DISTINCT ON (w.slug, l.source, l.item_id)
    w.slug, l.source, l.item_id, w.label, w.priority, w.max_bid_cad,
    l.title, l.lot_url, l.high_bid, l.min_bid, l.bid_count, l.km, l.close_at,
    l.event_name, l.event_city, l.thumbnail_url,
    coalesce(l.min_bid, l.high_bid, 0) <= w.max_bid_cad,
    -- Brand is read from the title first and only falls back to the
    -- description, because descriptions name component makers: the used N5100
    -- laptop in this corpus says "Brand: Intel", which is true of its CPU and
    -- says nothing about who built the machine.
    bt.brand, bt.tier,
    web.condition_grade(l.title, l.descr)
FROM named l
LEFT JOIN LATERAL web.brand_for(l.title, l.descr) bt ON true
JOIN reference.wishlist w
  ON  CASE w.match_scope
          WHEN 'title' THEN l.title
          ELSE l.title || ' ' || l.descr
      END ~* w.match_regex
 AND l.full_title !~* w.exclude_regex
ORDER BY w.slug, l.source, l.item_id, l.close_at
"""

# Every input the match set depends on, in one round trip. The digest covers
# the seed's matching columns only — editing the `notes` column of the wishlist
# CSV changes nothing about which lots match, and forcing a 4.5 second rebuild
# for a comment would train the user to stop writing them.
_PIPELINE_STATE_SQL = """
SELECT
    (SELECT max(scraped_at) FROM silver_enhanced.fct_lots) AS source_scraped_at,
    (SELECT md5(string_agg(
                slug || '\x1f' || match_scope || '\x1f' || match_regex ||
                '\x1f' || exclude_regex || '\x1f' || max_bid_cad::text ||
                '\x1f' || priority::text || '\x1f' || label,
                '\x1e' ORDER BY slug))
       FROM reference.wishlist)                            AS wishlist_digest
"""


def pipeline_state() -> dict:
    """What the match cache would be built from right now."""
    return db.query_one(_PIPELINE_STATE_SQL) or {}


def build_state() -> Optional[dict]:
    """What the match cache was last built from, or None if never."""
    return db.query_one("SELECT * FROM web.match_build WHERE only_row")


def matches_stale() -> bool:
    """
    Whether the cache no longer reflects the pipeline.

    A comparison of inputs, not a timer. Never built is stale; a dbt build
    landing or the wishlist seed changing is stale; everything else is not,
    which is what keeps the common page load free.
    """
    built = build_state()
    if built is None:
        return True
    current = pipeline_state()
    return (
        built["source_scraped_at"] != current.get("source_scraped_at")
        or built["wishlist_digest"] != current.get("wishlist_digest")
    )


def rebuild_matches() -> dict:
    """
    Re-run the matcher and replace the cache. Returns the new build row.

    Delete-then-insert inside one transaction, so a reader either sees the
    whole previous match set or the whole new one. Rebuilding in place would
    briefly show an empty wishlist, which reads as "nothing matched today"
    rather than "ask again in four seconds".
    """
    started = time.monotonic()
    current = pipeline_state()

    with db.cursor(commit=True) as cur:
        # The matcher sorts 87k description rows to join them. The default 4MB
        # sends that to disk as a 32MB external merge; giving it room turns the
        # sort in-memory. Scoped to this transaction, so nothing else on the
        # connection inherits it.
        cur.execute("SET LOCAL work_mem = '256MB'")

        cur.execute(_LOTS_CONSIDERED_SQL)
        lots_considered = cur.fetchone()["n"]

        cur.execute("DELETE FROM web.wishlist_match")
        cur.execute(_REBUILD_SQL)
        match_count = cur.rowcount

        cur.execute(
            """
            INSERT INTO web.match_build (
                only_row, built_at, source_scraped_at, wishlist_digest,
                match_count, lots_considered, duration_ms)
            VALUES (TRUE, now(), %s, %s, %s, %s, %s)
            ON CONFLICT (only_row) DO UPDATE SET
                built_at          = EXCLUDED.built_at,
                source_scraped_at = EXCLUDED.source_scraped_at,
                wishlist_digest   = EXCLUDED.wishlist_digest,
                match_count       = EXCLUDED.match_count,
                lots_considered   = EXCLUDED.lots_considered,
                duration_ms       = EXCLUDED.duration_ms
            """,
            (
                current.get("source_scraped_at"),
                current.get("wishlist_digest"),
                match_count,
                lots_considered,
                int((time.monotonic() - started) * 1000),
            ),
        )

    built = build_state()
    logger.info(
        "wishlist match rebuild: %s matches over %s live lots in %sms",
        built["match_count"],
        built["lots_considered"],
        built["duration_ms"],
    )
    return built


# How many live lots the matcher screened, for the freshness line. Worth
# showing beside the match count: "54 matches" means something different
# against 18,000 open lots than against 200.
_LOTS_CONSIDERED_SQL = """
SELECT count(*) AS n
FROM silver_enhanced.fct_lots f
LEFT JOIN silver_enhanced.auction_manifest m ON m.auction_id = f.auction_id
WHERE f.recency_rank = 1
  AND f.lot_status = 'OPEN'
  AND coalesce(m.close_at, f.event_ends_at) > now()
"""


def ensure_matches() -> dict:
    """Rebuild the cache if the pipeline has moved under it."""
    if matches_stale():
        return rebuild_matches()
    return build_state()


# ---------------------------------------------------------------------------
# Reading lots for the UI
# ---------------------------------------------------------------------------

# The overlay. `web.lot_snapshot` holds what the refresh button last saw, and it
# wins over the cached figures whenever it is newer than the observation the
# pipeline built from. Between a refresh and the next dbt build that is the only
# place the current price exists, and it is precisely the lot the user just
# asked about.
_OVERLAY = """
    coalesce(s.high_bid,  b.high_bid)   AS high_bid,
    coalesce(s.min_bid,   b.min_bid)    AS min_bid,
    coalesce(s.bid_count, b.bid_count)  AS bid_count,
    coalesce(s.close_at,  b.close_at)   AS close_at,
    s.lot_status                        AS refreshed_status,
    s.time_left                         AS time_left,
    s.observed_at                       AS observed_at,
    s.refreshed_at                      AS refreshed_at,
    s.refresh_error                     AS refresh_error,
    r.status                            AS review_status,
    r.max_bid                           AS review_max_bid,
    r.notes                             AS review_notes,
    r.updated_at                        AS reviewed_at
"""

_LIST_SQL = f"""
SELECT
    b.slug, b.source, b.item_id, b.label, b.priority, b.max_bid_cad,
    b.title, b.lot_url, b.km, b.event_name, b.event_city,
    b.thumbnail_url, b.within_budget,
    b.brand, b.brand_tier, b.condition_grade,
    {_OVERLAY}
FROM web.wishlist_match b
LEFT JOIN web.lot_snapshot s ON s.source = b.source AND s.item_id = b.item_id
LEFT JOIN web.lot_review   r ON r.source = b.source AND r.item_id = b.item_id
WHERE (%(slug)s   IS NULL OR b.slug = %(slug)s)
  AND (%(status)s IS NULL OR coalesce(r.status, 'unread') = %(status)s)
  AND (NOT %(budget_only)s OR b.within_budget)
  AND (NOT %(hide_judged)s OR r.status IS NULL OR r.status IN ('starred', 'bidding'))
  -- "closed boxes only": sealed or new-in-open-box. Deliberately excludes
  -- 'tested', which is a working-order claim about a loose unit, not a box.
  AND (NOT %(boxed_only)s OR b.condition_grade IN ('sealed', 'new'))
ORDER BY {{order}}
"""

# Sort orders offered in the UI. Whitelisted rather than interpolated from the
# request, because this lands in an ORDER BY.
SORT_ORDERS = {
    # The default. Most-wanted wishlist rows first, then cheapest to enter —
    # which is the order the Telegram alert already uses, so the two feeds
    # cannot disagree about what is at the top.
    "priority": "b.priority, b.label, coalesce(b.min_bid, b.high_bid, 0), b.close_at",
    "closing": "b.close_at, b.priority",
    "cheapest": "coalesce(b.min_bid, b.high_bid, 0), b.priority",
    "distance": "b.km NULLS LAST, b.priority",
    # Best box first, then most-wanted. The sort someone buying sealed stock
    # actually reads the list in.
    "condition": "web.condition_rank(b.condition_grade), b.priority, coalesce(b.min_bid, b.high_bid, 0)",
    "bids": "b.bid_count DESC NULLS LAST, b.priority",
}


def list_matches(
    slug: Optional[str] = None,
    status: Optional[str] = None,
    budget_only: bool = False,
    hide_judged: bool = False,
    boxed_only: bool = False,
    sort: str = "priority",
) -> list[dict]:
    """Cached wishlist matches, with review state and any refresh overlaid."""
    order = SORT_ORDERS.get(sort, SORT_ORDERS["priority"])
    return db.query(
        _LIST_SQL.format(order=order),
        {
            "slug": slug,
            "status": status,
            "budget_only": budget_only,
            "hide_judged": hide_judged,
            "boxed_only": boxed_only,
        },
    )


def wishlist_groups() -> list[dict]:
    """
    Every wishlist row with how many live lots it currently matches.

    A LEFT JOIN from the seed rather than a GROUP BY over the matches, so rows
    matching nothing still appear. A silent zero is information — the projector
    row has never matched anything in the whole corpus, and seeing that stated
    is what tells you the regex is a hypothesis rather than a filter.
    """
    return db.query(
        """
        SELECT w.slug, w.label, w.priority, w.max_bid_cad, w.intent, w.notes,
               count(b.item_id)                                  AS n,
               count(b.item_id) FILTER (WHERE b.within_budget)   AS n_in_budget,
               count(b.item_id) FILTER (WHERE r.status IS NULL)  AS n_unread,
               count(b.item_id) FILTER (WHERE r.status = 'starred') AS n_starred
        FROM reference.wishlist w
        LEFT JOIN web.wishlist_match b ON b.slug = w.slug
        LEFT JOIN web.lot_review r ON r.source = b.source AND r.item_id = b.item_id
        GROUP BY w.slug, w.label, w.priority, w.max_bid_cad, w.intent, w.notes
        ORDER BY w.priority, w.label
        """
    )


_SEARCH_SQL = f"""
WITH {_LIVE_LOTS}
SELECT
    NULL::text AS slug, NULL::text AS label, NULL::integer AS priority,
    NULL::numeric AS max_bid_cad, TRUE AS within_budget,
    l.source, l.item_id, l.title, l.lot_url, l.km, l.event_name, l.event_city,
    l.thumbnail_url,
    bt.brand, bt.tier AS brand_tier,
    web.condition_grade(l.title, l.descr) AS condition_grade,
    coalesce(s.high_bid,  l.high_bid)  AS high_bid,
    coalesce(s.min_bid,   l.min_bid)   AS min_bid,
    coalesce(s.bid_count, l.bid_count) AS bid_count,
    coalesce(s.close_at,  l.close_at)  AS close_at,
    s.lot_status AS refreshed_status, s.time_left, s.observed_at,
    s.refreshed_at, s.refresh_error,
    r.status AS review_status, r.max_bid AS review_max_bid,
    r.notes AS review_notes, r.updated_at AS reviewed_at
FROM named l
LEFT JOIN LATERAL web.brand_for(l.title, l.descr) bt ON true
LEFT JOIN web.lot_snapshot s ON s.source = l.source AND s.item_id = l.item_id
LEFT JOIN web.lot_review   r ON r.source = l.source AND r.item_id = l.item_id
WHERE {{predicate}}
ORDER BY coalesce(l.min_bid, l.high_bid, 0), l.close_at
LIMIT %(limit)s
"""


def search_lots(query: str, limit: int = 200) -> tuple[list[dict], str]:
    """
    Free search over every live lot, in either of two modes.

    Plain text is a case-insensitive substring of the title or description.
    A query starting with `/` is treated as a POSIX regex and run the way the
    wishlist matcher runs one — which makes this the place to try a rule out
    against the live corpus before committing it to the seed. Seven rounds of
    false positives have come from rules that looked right in the abstract, and
    each was found by running exactly this query by hand.

    Returns the rows and the mode used, so the UI can say which one it did.
    """
    query = (query or "").strip()
    if not query:
        return [], "empty"

    if query.startswith("/"):
        pattern = query[1:].strip()
        if not pattern:
            return [], "empty"
        predicate = "(l.title || ' ' || l.descr) ~* %(q)s"
        params: dict[str, Any] = {"q": pattern, "limit": limit}
        mode = "regex"
    else:
        predicate = "(l.title || ' ' || l.descr) ILIKE %(q)s"
        params = {"q": f"%{query}%", "limit": limit}
        mode = "text"

    try:
        rows = db.query(_SEARCH_SQL.format(predicate=predicate), params)
    except Exception as e:
        # An invalid regex is a typo, not a server fault — the whole point of
        # regex mode is iterating on one, so it has to fail readably.
        if mode == "regex":
            return [], f"error: {str(e).strip().splitlines()[0]}"
        raise
    return rows, mode


_DETAIL_SQL = f"""
WITH {_LIVE_LOTS}
SELECT
    l.source, l.item_id, l.sys_run_name, l.title, l.lot_url, l.descr,
    l.km, l.event_name, l.event_city, l.scraped_at, l.thumbnail_url,
    l.high_bid AS pipeline_high_bid, l.min_bid AS pipeline_min_bid,
    l.bid_count AS pipeline_bid_count, l.close_at AS pipeline_close_at,
    coalesce(s.high_bid,  l.high_bid)  AS high_bid,
    coalesce(s.min_bid,   l.min_bid)   AS min_bid,
    coalesce(s.bid_count, l.bid_count) AS bid_count,
    coalesce(s.close_at,  l.close_at)  AS close_at,
    s.lot_status AS refreshed_status, s.time_left, s.observed_at,
    s.refreshed_at, s.refresh_error, s.image_urls AS refreshed_images,
    r.status AS review_status, r.max_bid AS review_max_bid,
    r.notes AS review_notes, r.updated_at AS reviewed_at,
    CASE l.source WHEN 'hibid' THEN h.raw_json ELSE p.raw_json END AS raw_json,
    bt.brand, bt.tier AS brand_tier, bt.note AS brand_note,
    web.condition_grade(l.title, l.descr) AS condition_grade
FROM named l
LEFT JOIN LATERAL web.brand_for(l.title, l.descr) bt ON true
LEFT JOIN silver.stg_hibid_lots h
       ON l.source = 'hibid' AND h.item_id = l.item_id
      AND h.sys_run_name = l.sys_run_name
LEFT JOIN silver.stg_police_lots p
       ON l.source = 'police_auctions' AND p.item_id = l.item_id
      AND p.sys_run_name = l.sys_run_name
LEFT JOIN web.lot_snapshot s ON s.source = l.source AND s.item_id = l.item_id
LEFT JOIN web.lot_review   r ON r.source = l.source AND r.item_id = l.item_id
WHERE l.source = %(source)s AND l.item_id = %(item_id)s
"""


def lot_detail(source: str, item_id: str) -> Optional[dict]:
    """
    One lot, with everything the detail pane shows.

    Returns None when the lot is not live — closed since the last scrape, or
    never in the corpus. The caller distinguishes those; both are a dead pane.
    """
    row = db.query_one(_DETAIL_SQL, {"source": source, "item_id": item_id})
    if row is None:
        return None

    row["images"] = image_urls(row)
    row["wishlist_rows"] = db.query(
        """
        SELECT b.slug, b.label, b.priority, b.max_bid_cad, b.within_budget
        FROM web.wishlist_match b
        WHERE b.source = %s AND b.item_id = %s
        ORDER BY b.priority, b.label
        """,
        (source, item_id),
    )
    return row


def image_urls(row: dict) -> list[str]:
    """
    Every picture known for a lot, best first, de-duplicated.

    A refresh is preferred over the pipeline's copy because it fetched the lot
    *page*: HiBid's catalogue pages carry a partial gallery and Police Auctions'
    browse cards carry exactly one thumbnail, while both detail pages carry the
    lot. Getting the rest of the photos is half of what the refresh button is
    for, so they lead.
    """
    urls: list[str] = list(row.get("refreshed_images") or [])

    payload = row.get("raw_json") or {}
    if row.get("source") == "hibid":
        featured = payload.get("featuredPicture") or {}
        for picture in [featured, *(payload.get("pictures") or [])]:
            if not isinstance(picture, dict):
                continue
            url = picture.get("fullSizeLocation") or picture.get("thumbnailLocation")
            if url:
                urls.append(url)
    else:
        if payload.get("image_url"):
            # The browse card links the thumbnail-fit crop. The same asset is
            # published full size under a predictable name, so ask for that and
            # keep the crop as the fallback — a wrong guess here shows a broken
            # image, which is worse than a small one.
            thumb = payload["image_url"]
            urls.append(thumb.replace("_thumbfit.jpg", "_fullsize.jpg"))
            urls.append(thumb)

    if row.get("thumbnail_url"):
        urls.append(row["thumbnail_url"])

    seen: set[str] = set()
    unique = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


# ---------------------------------------------------------------------------
# Selection state
# ---------------------------------------------------------------------------

REVIEW_STATUSES = ("starred", "bidding", "won", "lost", "passed", "false_positive")

# Shown on the buttons. The verb is what the click means, not what the row
# becomes, because the row is already in front of you.
REVIEW_LABELS = {
    "starred": "Star",
    "bidding": "Bidding",
    "won": "Won",
    "lost": "Lost",
    "passed": "Pass",
    "false_positive": "Not this",
}


def set_review(
    source: str,
    item_id: str,
    status: str,
    max_bid: Optional[float] = None,
    notes: Optional[str] = None,
) -> dict:
    """Record a verdict on a lot. Re-setting the same status just re-stamps it."""
    if status not in REVIEW_STATUSES:
        raise ValueError(f"unknown review status {status!r}")

    # max_bid and notes are only overwritten when supplied, so clicking a
    # status button does not silently wipe a note typed a minute earlier.
    return db.mutate_one(
        """
        INSERT INTO web.lot_review (source, item_id, status, max_bid, notes)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (source, item_id) DO UPDATE SET
            status     = EXCLUDED.status,
            max_bid    = coalesce(EXCLUDED.max_bid, web.lot_review.max_bid),
            notes      = coalesce(EXCLUDED.notes,   web.lot_review.notes),
            updated_at = now()
        RETURNING *
        """,
        (source, item_id, status, max_bid, notes),
    )


def clear_review(source: str, item_id: str) -> None:
    """Put a lot back in the unread queue by deleting the row entirely."""
    db.execute(
        "DELETE FROM web.lot_review WHERE source = %s AND item_id = %s",
        (source, item_id),
    )


def review_counts() -> dict:
    """How many matched lots sit in each verdict, for the filter chips."""
    rows = db.query(
        """
        SELECT coalesce(r.status, 'unread') AS status, count(DISTINCT (b.source, b.item_id)) AS n
        FROM web.wishlist_match b
        LEFT JOIN web.lot_review r ON r.source = b.source AND r.item_id = b.item_id
        GROUP BY 1
        """
    )
    return {row["status"]: row["n"] for row in rows}
