"""
Re-fetch one lot on demand: the refresh button.

The pipeline sees a lot when a sweep happens to pass over it — three times a
morning, and only for auctions the orchestrator selected. That is the right
cadence for 18,000 lots and the wrong one for the four you are actually
thinking about bidding on, where the question is "what is it at *now*" and the
answer has to be seconds old.

Two things come back that a sweep does not have:

  * the current price, from the lot's own page rather than a catalogue row;
  * the rest of the photographs. HiBid's catalogue pages carry a partial
    gallery and Police Auctions' browse cards carry exactly one thumbnail,
    while both detail pages carry everything. For deciding whether a $2.50
    ProDesk is worth driving to, the photos are the whole decision.

Where the result goes, and why in two places:

  * `raw.hibid` / `raw.police_auctions` — because it is a genuine observation
    of a lot and bronze is append-only. Anything else would make the price
    history lie about what was known when.
  * `web.lot_snapshot` — because bronze is not what the site reads. `fct_lots`
    is a dbt table rebuilt by the scrape flow, so between a refresh and the
    next build the new price exists nowhere the UI can see it.

One nuance about the bronze write. The scrape flow builds silver scoped with
`--vars '{target_run: ...}'`, so it only ever transforms its own run; a refresh
run is picked up by an unscoped `dbt run` or a `--full-refresh`, not by the
next scrape. That is deliberate and harmless — the snapshot is what the UI
reads, and bronze is where the observation is kept until silver is next
replayed in full.
"""

import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from bs4 import BeautifulSoup

import db
from scraper_bridge import Database, PageFetcher, apollo
from settings import scraper_config

logger = logging.getLogger(__name__)

HIBID_LOT_URL = "https://hibid.com/lot/{item_id}"
PAC_BASE_URL = "https://policeauctionscanada.com"
PAC_LISTING_URL = PAC_BASE_URL + "/Listing/Details/{item_id}"

# A real lot photo: a UUID asset with a size suffix. Site chrome from the
# same bucket has no suffix, which is the only thing separating them.
PAC_ASSET_RE = re.compile(r"_(fullsize|thumbfit)\.jpg", re.I)

# Two at a time. Each refresh drives a real Chrome through FlareSolverr, and
# forty-two orphaned browsers is the reason the scraper closes its sessions so
# carefully — a page with a dozen refresh buttons and an impatient user is
# exactly how that happens again.
_refresh_slots = threading.Semaphore(2)


@dataclass
class RefreshResult:
    """What one refresh attempt learned. `error` set means nothing else is."""

    source: str
    item_id: str
    ok: bool = False
    error: Optional[str] = None
    observed: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# HiBid
# ---------------------------------------------------------------------------


class HiBidLotFetcher(PageFetcher):
    """
    One HiBid lot page.

    A lot page ships the same Apollo cache every HiBid page does, so the
    payload built here is the same shape the catalogue scrape produces — which
    is what lets the bronze row it writes flow through silver unchanged.

    The two hooks differ from the catalogue scraper's because the question
    differs. A catalogue asks "is there another page"; a lot page has no
    pagination, so the only end-of-results signal that means anything is the
    stub HiBid serves for a lot that is no longer there.
    """

    source = "hibid"

    def __init__(self, config, item_id: str):
        super().__init__(config)
        self.item_id = str(item_id)

    def is_incomplete(self, html: str) -> bool:
        # The precise test, not `has_rendered_query`: the ROOT_QUERY key here
        # is `lot({"countAsView":true,"input":"318648415"})`, and matching on
        # "lot(" alone would also accept a page carrying somebody else's lot.
        return f'"Lot:{self.item_id}"' not in html

    def is_end_of_results(self, html: str) -> bool:
        # ~183 bytes with no state script. For a search that means the last
        # page; for a lot it means the lot has been pulled.
        return apollo.is_end_of_results(html)


def _fetch_hibid(config, item_id: str) -> tuple[Optional[dict], Optional[str]]:
    """Fetch and rebuild one HiBid lot payload. Returns (payload, error)."""
    fetcher = HiBidLotFetcher(config, item_id)
    try:
        html = fetcher.fetch_page(HIBID_LOT_URL.format(item_id=item_id))
    finally:
        # Always, on every path. A dropped FlareSolverr session is a Chrome
        # process that survives until the container restarts.
        fetcher.close()

    if html == "":
        return None, "HiBid no longer serves this lot — it looks withdrawn."
    if html is None:
        return None, "HiBid did not return a rendered lot page after 5 attempts."

    state = apollo.extract_state(html)
    if not state:
        return None, "The lot page carried no Apollo state."

    lot = state.get(f"Lot:{item_id}")
    if not isinstance(lot, dict):
        return None, f"No Lot:{item_id} in the page's cache."

    payload = dict(lot)

    # Resolve the two references silver depends on. `auction_data.id` is not
    # cosmetic: stg_hibid_lots drops any row without it, on the grounds that a
    # lot with no auction has no location and can never be placed in a radius.
    auction_ref = payload.get("auction")
    if isinstance(auction_ref, dict) and "__ref" in auction_ref:
        resolved = state.get(auction_ref["__ref"])
        if isinstance(resolved, dict):
            payload["auction_data"] = resolved
        payload["auction_ref"] = payload.pop("auction")["__ref"]

    # lotState arrives inline on a lot page, but the catalogue path resolves it
    # from a ref — handle both so this cannot quietly start writing rows whose
    # prices silver reads as null.
    lot_state = payload.get("lotState")
    if isinstance(lot_state, dict) and "__ref" in lot_state:
        resolved = state.get(lot_state["__ref"])
        payload["lot_state_ref"] = payload.pop("lotState")["__ref"]
        if isinstance(resolved, dict):
            payload["lotState"] = resolved

    if not isinstance(payload.get("lotState"), dict):
        return None, "The lot page carried no lotState — no price to read."

    return payload, None


def _observe_hibid(payload: dict) -> dict:
    """Pull the display figures out of a HiBid payload."""
    state = payload.get("lotState") or {}

    # `bidAmount` on the Lot object is a fixed 123.45 placeholder on every
    # endpoint HiBid serves. The live figures are only ever on lotState.
    close_at = None
    seconds = state.get("timeLeftSeconds")
    if isinstance(seconds, (int, float)) and seconds > 0:
        # Derived from the countdown rather than parsed out of `timeLeftTitle`
        # ("Internet Bidding closes at: 8/25/2026 10:35:20 AM EST"), which is a
        # display string with a named zone and no year discipline.
        close_at = datetime.now(timezone.utc) + timedelta(seconds=float(seconds))

    pictures = [payload.get("featuredPicture"), *(payload.get("pictures") or [])]
    images = [
        p.get("fullSizeLocation") or p.get("thumbnailLocation")
        for p in pictures
        if isinstance(p, dict) and (p.get("fullSizeLocation") or p.get("thumbnailLocation"))
    ]

    return {
        "high_bid": state.get("highBid"),
        "min_bid": state.get("minBid"),
        "bid_count": state.get("bidCount"),
        "lot_status": state.get("status"),
        "time_left": (state.get("timeLeft") or "").strip() or None,
        "close_at": close_at,
        "close_at_text": None,
        "image_urls": _dedupe(images),
    }


# ---------------------------------------------------------------------------
# Police Auctions Canada
# ---------------------------------------------------------------------------


class PoliceLotFetcher(PageFetcher):
    """
    One Police Auctions listing page.

    Direct, like the browse scraper: the site answers plain requests fine and
    FlareSolverr's solver has been seen to crash on it. The browser stays
    available as the fallback for a 403.
    """

    source = "police_auctions"
    prefer_direct = True

    def is_incomplete(self, html: str) -> bool:
        # Prices render into `.awe-rt-CurrentPrice` by the same live-bidding
        # widget the browse cards use. No price element means the page has not
        # finished, which is a retry rather than a missing lot.
        return "awe-rt-CurrentPrice" not in html

    def is_end_of_results(self, html: str) -> bool:
        return False


def _money(node) -> Optional[float]:
    """
    A price out of one of the live-bidding elements.

    Same shape as the browse cards — `$<span class="NumberPart">2.74</span>` —
    except the detail page also renders a "Quick Bid $2.74" variant, so the
    digits are taken from the NumberPart child when there is one and scraped
    out of the text when there is not.
    """
    if node is None:
        return None
    part = node.find(class_="NumberPart")
    text = (part or node).get_text(strip=True)
    match = re.search(r"[\d,]+\.?\d*", text.replace("$", ""))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _fetch_police(config, item_id: str) -> tuple[Optional[dict], Optional[str]]:
    """Fetch and rebuild one Police Auctions payload. Returns (payload, error)."""
    fetcher = PoliceLotFetcher(config)
    try:
        html = fetcher.fetch_page(PAC_LISTING_URL.format(item_id=item_id))
    finally:
        fetcher.close()

    if not html:
        return None, "Police Auctions did not return a usable listing page."

    soup = BeautifulSoup(html, "html.parser")

    # The detail page has no h1 and no .title — the only clean copy of the
    # product name is the OpenGraph tag, which matches the browse card's title
    # exactly. Taken rather than carried over from the database because this
    # payload becomes a bronze row, and a row with a null title would surface
    # as a nameless lot the moment silver is replayed.
    og_title = soup.find("meta", property="og:title")
    title = (og_title or {}).get("content")
    if not title:
        return None, "No og:title on the listing page — refusing to write a nameless lot."

    time_el = soup.find(attrs={"data-action-time": True})
    ends_at = time_el["data-action-time"] if time_el else None

    # Lot photos only. Every pacimages asset is a UUID with a `_fullsize` or
    # `_thumbfit` suffix; the site's own chrome (logo, payment icons) is served
    # from the same bucket as bare `.png`, and without this filter two of those
    # land in every gallery — which is how a bike listing came back with 11
    # photos, two of them the company logo.
    #
    # Thumbnails are rewritten to their full-size twin rather than kept beside
    # it: the carousel links `_thumbfit` for all but the lead image, and the
    # full-size asset is the same UUID. That matters here more than anywhere
    # else in the project — Police Auctions photographs every lot against a
    # measuring wall, so the photo IS the specification, and a 150px crop of a
    # ruler is worth nothing.
    images = _dedupe(
        img["src"].replace("_thumbfit.jpg", "_fullsize.jpg")
        for img in soup.select("img[src]")
        if "pacimages" in img["src"] and PAC_ASSET_RE.search(img["src"])
    )
    og_image = soup.find("meta", property="og:image")
    if og_image and og_image.get("content"):
        images = _dedupe([og_image["content"], *images])

    payload = {
        # Exactly the keys police_scraper._parse_listing emits, so
        # stg_police_lots reads this row and a browse-card row identically.
        "listing_id": str(item_id),
        "title": title.strip(),
        "seller": "Police Auctions Canada",
        "url": PAC_LISTING_URL.format(item_id=item_id),
        "image_url": images[0] if images else None,
        "current_price": _money(soup.select_one(".awe-rt-CurrentPrice")),
        "minimum_bid": _money(soup.select_one(".awe-rt-MinimumBid")),
        "ends_at": ends_at,
        # The browse card decides this from a status element that the detail
        # page does not render, so it is derived from the clock instead. The
        # site is a rolling catalogue with no re-listing, so "past its end time"
        # and "ended" are the same thing here.
        "has_ended": _pac_has_ended(ends_at),
        "location": {
            "address": "Toronto, ON M8Z 2X3",
            "city": "Toronto",
            "province": "ON",
            "postal_code": "M8Z 2X3",
            "country_code": "CA",
        },
        # Extra key, ignored by silver. The browse card only ever carried one
        # image; keeping the gallery is most of why a refresh is worth pressing.
        "images": images,
    }
    return payload, None


def _pac_has_ended(ends_at: Optional[str]) -> bool:
    if not ends_at:
        return False
    try:
        parsed = datetime.strptime(ends_at, "%m/%d/%Y %H:%M:%S")
    except ValueError:
        return False
    # Naive-to-naive against UTC, which is both correct and what
    # stg_police_lots does with the same string. The site publishes
    # `data-action-time` in UTC despite being a Toronto seller — verified
    # against bronze, see the note in web/README.md, because it does not look
    # like it (its 20:20 closes are 4:20 PM local, not 8:20 PM).
    return parsed <= datetime.now(timezone.utc).replace(tzinfo=None)


def _observe_police(payload: dict) -> dict:
    return {
        "high_bid": payload.get("current_price"),
        "min_bid": payload.get("minimum_bid"),
        "bid_count": None,  # never published per listing
        "lot_status": "CLOSED" if payload.get("has_ended") else "OPEN",
        "time_left": None,  # ends_at is absolute, not a countdown
        "close_at": None,
        # Converted by Postgres with the same expression stg_police_lots uses.
        # The string is UTC — confirmed, not assumed; see web/README.md.
        "close_at_text": payload.get("ends_at"),
        "image_urls": payload.get("images") or [],
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

FETCHERS = {
    "hibid": (_fetch_hibid, _observe_hibid),
    "police_auctions": (_fetch_police, _observe_police),
}


def refresh_lot(source: str, item_id: str) -> RefreshResult:
    """
    Re-fetch one lot, bank the observation, and record what was seen.

    Never raises for an ordinary failure. A blocked fetch, a withdrawn lot and
    a page that will not render are all things the user needs told about in the
    pane they are looking at, not things that should return a 500.
    """
    if source not in FETCHERS:
        return RefreshResult(source, item_id, error=f"Unknown source {source!r}.")

    fetch, observe = FETCHERS[source]
    config = scraper_config()

    with _refresh_slots:
        try:
            payload, error = fetch(config, item_id)
        except Exception as e:  # a transport fault, not a bad lot
            logger.exception("refresh %s/%s failed", source, item_id)
            error, payload = f"{type(e).__name__}: {e}", None

    if payload is None:
        _record_failure(source, item_id, error or "Unknown failure.")
        return RefreshResult(source, item_id, error=error)

    observed = observe(payload)

    try:
        _write_bronze(config, source, item_id, payload)
    except Exception:
        # A failed bronze write must not lose the observation the user just
        # paid ten seconds for — the snapshot below is what the page reads.
        # Loud in the log, invisible in the UI, which is the right split: the
        # user asked for a price, not for a report on the landing table.
        logger.exception("could not bank refresh of %s/%s to bronze", source, item_id)

    _record_success(source, item_id, observed)
    return RefreshResult(source, item_id, ok=True, observed=observed)


def _write_bronze(config, source: str, item_id: str, payload: dict) -> None:
    """
    Append the observation to the landing table, through the scraper's own writer.

    A one-row scrape run rather than a bare INSERT: silver admits rows only
    from runs marked `completed`, and bronze is monthly-partitioned. Both of
    those are the Database class's job and neither is worth reimplementing.
    """
    sys_run_name = f"web-refresh-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    writer = Database(config, source=source)
    try:
        writer.connect()
        run_id = writer.start_scrape_run(
            zip_code=config.zip_code or None,
            radius_miles=config.radius_miles,
            test_mode=False,
            sys_run_name=sys_run_name,
        )
        writer.insert_item(item_id, payload, category=None, sys_run_name=sys_run_name)
        writer.flush()
        writer.complete_scrape_run(run_id, items_found=1, items_inserted=1, errors=0)
    finally:
        writer.close()


_SNAPSHOT_UPSERT = """
INSERT INTO web.lot_snapshot (
    source, item_id, refreshed_at, refresh_error, observed_at,
    high_bid, min_bid, bid_count, lot_status, time_left, close_at, image_urls)
VALUES (
    %(source)s, %(item_id)s, now(), NULL, now(),
    %(high_bid)s, %(min_bid)s, %(bid_count)s, %(lot_status)s, %(time_left)s,
    coalesce(%(close_at)s::timestamptz,
             to_timestamp(%(close_at_text)s, 'MM/DD/YYYY HH24:MI:SS')),
    %(image_urls)s)
ON CONFLICT (source, item_id) DO UPDATE SET
    refreshed_at  = EXCLUDED.refreshed_at,
    refresh_error = NULL,
    observed_at   = EXCLUDED.observed_at,
    -- Bids only ever go up, so a figure that drops is not a new observation
    -- of a cheaper lot -- it is HiBid zeroing lotState the moment an auction
    -- closes. A Jetson e-bike that took 19 bids to $160 reported highBid 0 and
    -- bidCount 0 thirty minutes later, and writing that through would erase
    -- the sale price the caps are supposed to be learned from.
    --
    -- GREATEST rather than a CLOSED check because it needs no trust in the
    -- status field and stays correct for the honest case: a lot that closes
    -- with no bids was already 0, so 0 wins and nothing is invented.
    high_bid      = GREATEST(EXCLUDED.high_bid, web.lot_snapshot.high_bid),
    bid_count     = GREATEST(EXCLUDED.bid_count, web.lot_snapshot.bid_count),
    -- min_bid is the next accepted bid rather than a running total, so it is
    -- not monotonic and cannot use GREATEST. It is zeroed on close the same
    -- way, so keep the last real figure instead.
    min_bid       = CASE
                        WHEN coalesce(EXCLUDED.min_bid, 0) > 0
                        THEN EXCLUDED.min_bid
                        ELSE web.lot_snapshot.min_bid
                    END,
    lot_status    = EXCLUDED.lot_status,
    time_left     = EXCLUDED.time_left,
    close_at      = EXCLUDED.close_at,
    -- Never replace a gallery with an empty one. A lot page that renders its
    -- prices but not its carousel is a real thing, and blanking the photos
    -- would make the refresh button destructive.
    image_urls    = CASE
                        WHEN coalesce(array_length(EXCLUDED.image_urls, 1), 0) > 0
                        THEN EXCLUDED.image_urls
                        ELSE web.lot_snapshot.image_urls
                    END
"""


def _record_success(source: str, item_id: str, observed: dict) -> None:
    db.execute(
        _SNAPSHOT_UPSERT,
        {
            "source": source,
            "item_id": item_id,
            "high_bid": observed.get("high_bid"),
            "min_bid": observed.get("min_bid"),
            "bid_count": observed.get("bid_count"),
            "lot_status": observed.get("lot_status"),
            "time_left": observed.get("time_left"),
            "close_at": observed.get("close_at"),
            "close_at_text": observed.get("close_at_text"),
            "image_urls": observed.get("image_urls") or [],
        },
    )


def _record_failure(source: str, item_id: str, error: str) -> None:
    """
    Record that an attempt happened and failed, leaving the last good figures.

    Separate columns for the attempt and the observation is the whole point:
    overwriting prices with nulls because HiBid was briefly unhappy would turn
    a transient block into a lot that looks like it has no bids.
    """
    db.execute(
        """
        INSERT INTO web.lot_snapshot (source, item_id, refreshed_at, refresh_error)
        VALUES (%s, %s, now(), %s)
        ON CONFLICT (source, item_id) DO UPDATE SET
            refreshed_at  = now(),
            refresh_error = EXCLUDED.refresh_error
        """,
        (source, item_id, error[:500]),
    )


def _dedupe(urls) -> list[str]:
    seen: set[str] = set()
    out = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out
