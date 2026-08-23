"""
HiBid Auction Scraper.

Scrapes auction items from HiBid based on zip code and radius.
Adapted from: https://github.com/jkoelmel/texas_auctions_scraper

HiBid uses Angular with Apollo GraphQL. The SSR response contains the
Apollo state cache in a <script id="hibid-state"> tag, which we parse
to extract lot data.
"""

import logging
import time
from typing import Iterator, Optional

import apollo
from config import Config
from fetcher import PageFetcher, ScrapeStats

logger = logging.getLogger(__name__)

# HiBid base URL (works for nationwide search)
HIBID_BASE_URL = "https://hibid.com"

# Items per page (HiBid's default is 100, but we use smaller batches for stability)
ITEMS_PER_PAGE = 100

# How many times the end-of-results stub must repeat before it is believed.
END_OF_RESULTS_CONFIRMATIONS = 3

# How many times a catalogue page that came back short is re-fetched while the
# auction's lot count says more are still due, and the base pause before each
# attempt. Multiplied by the attempt number, so 8s, 16s, 24s: a short page is a
# throttling signal, and re-asking immediately just earns another one.
SHORT_PAGE_RETRIES = 3
SHORT_PAGE_BACKOFF_SECONDS = 8

# Attempts per catalogue page before it is recorded as failed and left for the
# next pass. Low on purpose: a page that will not render now usually will later,
# and moving on costs nothing now that a failure no longer aborts the scan.
CATALOG_PAGE_ATTEMPTS = 3

# How far past the advertised last page to probe. Sellers add lots after
# discovery saw the auction, and an empty page stops it immediately.
CATALOG_PAGE_OVERRUN = 2

# Consecutive failures before a pass gives up on this auction for now.
#
# HiBid does not fail pages independently: once it starts answering with the
# 183-byte stub it keeps doing so for a while. Measured on a 43-page catalogue,
# pages 8-11 read cleanly and then 12-22 all stubbed — and each of those cost
# three attempts with backoff, about eight minutes for nothing. Stopping banks
# the good pages and leaves the rest as outstanding work, which is cheaper and
# no less complete.
CATALOG_CONSECUTIVE_FAILURES = 3

# Attempts a single page gets across all passes before it is retired.
#
# Failed pages sort lowest, so they were retried before pages never tried, and
# three failures in a row trips the breaker — meaning an auction could spend
# every pass re-failing the same pages and never reach the rest of its
# catalogue. Retiring a page that has failed this many times is the difference
# between "this catalogue is 15 of 43 forever" and "this catalogue is as
# complete as HiBid will allow".
CATALOG_MAX_PAGE_ATTEMPTS = 4


class HiBidScraper(PageFetcher):
    """Scraper for HiBid auction listings."""

    source = "hibid"

    def _build_url(self, category: Optional[str], page: int) -> str:
        """
        Build the URL for one page of lots.

        Two modes, because they answer different questions:

          * catalogue — every lot in one auction, open or closed. This is what
            the orchestrator uses. It is the only way to see an auction whole:
            a radius search drops lots the moment they close, so an auction
            part-way through its staggered close reads as half its size.

          * radius search — lots open right now within `miles` of a postal
            code. Still the way to find lots without knowing the auction, but
            it re-lists the same auctions every day and cannot be made complete.
        """
        if self.config.auction_id:
            params = {
                "apage": str(page),
                "ipp": str(ITEMS_PER_PAGE),
            }
            url = f"{HIBID_BASE_URL}/catalog/{self.config.auction_id}"
        else:
            url = f"{HIBID_BASE_URL}/lots/{category}/" if category else f"{HIBID_BASE_URL}/lots/"
            params = {
                "status": "open",
                "zip": self.config.zip_code,
                "miles": str(self.config.radius_miles),
                "apage": str(page),
                "ipp": str(ITEMS_PER_PAGE),
            }

        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{url}?{query_string}"

    def is_end_of_results(self, html: str) -> bool:
        """
        Whether there is genuinely nothing left to page through.

        The radius search answers a page past its last with a ~183 byte stub.
        A catalogue does not: it answers with a full site-chrome page carrying
        an Apollo state but no `lotSearch` node, which is byte-for-byte the same
        shape as a transient half-render.

        Telling those apart in one request is not possible, so this does not
        try — END_OF_RESULTS_CONFIRMATIONS repeats with a fresh browser each
        time is what separates them, and a false end on page one still lands
        zero lots, which scrape_source refuses to call a capture.
        """
        if apollo.is_end_of_results(html):
            return True
        if self.config.auction_id:
            return 'id="hibid-state"' in html and "lotSearch(" not in html
        return False

    def is_incomplete(self, html: str) -> bool:
        """
        A page carrying an Apollo state but no search results is not finished.

        Both modes render their lots through `lotSearch`, so that check covers
        either.

        A catalogue page needs a second one, because its lots carry no auction
        of their own — the auction is the page, not a field — and lots that land
        without one are dropped by silver as unplaceable. Usually a missing
        auction node means a half-rendered page and retrying fixes it. Some
        catalogues never render one at all, though, so the check only applies
        when there is no discovered payload to fall back on; otherwise those
        auctions would retry until they closed and be lost.
        """
        if not apollo.has_rendered_query(html, "lotSearch"):
            return True
        if self.config.auction_id and not self.config.auction_payload:
            return f'"Auction:{self.config.auction_id}"' not in html
        return False

    def _extract_apollo_state(self, html: str) -> Optional[dict]:
        """Extract the Apollo cache, counting a failure against the run."""
        state = apollo.extract_state(html)
        if state is None:
            self.stats.errors += 1
        return state

    def _search_result_refs(self, apollo_state: dict) -> Optional[list[str]]:
        """
        The lot refs HiBid actually returned for this search, in its order.

        The Apollo cache also holds lots the page merely referenced — featured
        and related items from other auctions, carrying a stub auction with no
        id or location. Those are not search results and must not be mistaken
        for inventory in the requested radius.

        In catalogue mode there is a second way to get the wrong lots: a page
        past the auction's last serves a perfectly well-formed lotSearch scoped
        to a *different* auction. Its results look exactly like ours, and
        because catalogue lots carry no auction of their own they would be
        stamped with the auction we asked for — filing a Missouri race car
        under a Toronto contractor sale. The query's own arguments are the only
        thing that says otherwise, so they are checked here.
        """
        if self.config.auction_id:
            args = apollo.query_arguments(apollo_state, "lotSearch") or {}
            found = (args.get("input") or {}).get("auctionId")
            # Normally null — a catalogue page asks by eventItemIds. Only a
            # node naming a different auction is disqualifying.
            if found is not None and str(found) != str(self.config.auction_id):
                logger.warning(
                    f"Page serves lotSearch for auction {found}, not "
                    f"{self.config.auction_id}; discarding its results"
                )
                return []

        return apollo.paged_result_refs(apollo_state, "lotSearch")

    def _extract_lots_from_apollo(self, apollo_state: dict) -> list[dict]:
        """
        Extract lot objects from Apollo state.

        Lots are stored with keys like "Lot:12345" and have __typename="Lot".
        We also resolve references to auctions to get complete data.
        """
        lots = []
        auctions = {}

        # First pass: collect auctions
        for key, value in apollo_state.items():
            if isinstance(value, dict):
                if value.get("__typename") == "Auction" or key.startswith("Auction:"):
                    auction_id = key.replace("Auction:", "") if key.startswith("Auction:") else value.get("id")
                    auctions[key] = value
                    auctions[f"Auction:{auction_id}"] = value

        # Second pass: collect the search results, in HiBid's own order. Fall
        # back to every Lot in the cache only if the search node is absent,
        # which upstream already treats as an unrendered page.
        result_refs = self._search_result_refs(apollo_state)
        if result_refs is None:
            if self.config.auction_id:
                # Never in catalogue mode. Sweeping the cache picks up featured
                # and related lots from other auctions, and those carry no
                # auction of their own — so the fallback below would stamp them
                # with this auction and silver would have no way to tell.
                logger.warning("No lotSearch results node on a catalogue page")
                return []
            logger.warning("No lotSearch results node; falling back to all lots")
            lot_keys = [k for k in apollo_state if k.startswith("Lot:")]
        else:
            lot_keys = result_refs

        for key in lot_keys:
            value = apollo_state.get(key)
            if isinstance(value, dict) and (
                value.get("__typename") == "Lot" or key.startswith("Lot:")
            ):
                lot = dict(value)  # Make a copy

                # Resolve auction reference
                auction_ref = lot.get("auction", {})
                if isinstance(auction_ref, dict) and "__ref" in auction_ref:
                    ref_key = auction_ref["__ref"]

                    # A catalogue page's own results can still include lots
                    # belonging to other auctions — ones carrying their own
                    # auction ref, so the payload fallback never touches them
                    # and the query-level guard sees nothing wrong because the
                    # query itself was ours. Observed live: an Australian
                    # "27th Aug General collectors Auction" lot arriving in a
                    # Toronto catalogue, postcode 3140, which geocodes to
                    # nothing and fails fct_lots' not-null distance test.
                    # We asked for one auction; anything else is not ours.
                    if self.config.auction_id:
                        ref_id = ref_key.replace("Auction:", "")
                        if ref_id != str(self.config.auction_id):
                            logger.warning(
                                f"Lot {key} belongs to auction {ref_id}, not "
                                f"{self.config.auction_id}; skipping"
                            )
                            continue

                    if ref_key in auctions:
                        lot["_resolved_auction"] = auctions[ref_key]
                elif self.config.auction_id:
                    # A catalogue page states the auction once, at the top, and
                    # leaves it off every lot — so attach the one the page is
                    # for. Without this the lots land with no auction and silver
                    # drops them for being unplaceable.
                    #
                    # The page's own copy is preferred where it exists: it is
                    # fuller than anything else available, carrying buyerPremium,
                    # bidIncrements and paymentInfo. Some catalogues render no
                    # auction node at all, and for those the copy discovery
                    # already fetched is what keeps the lots usable.
                    resolved = (
                        auctions.get(f"Auction:{self.config.auction_id}")
                        or self.config.auction_payload
                    )
                    if resolved:
                        lot["_resolved_auction"] = resolved

                # Also resolve lotState reference if present
                lot_state_ref = lot.get("lotState", {})
                if isinstance(lot_state_ref, dict) and "__ref" in lot_state_ref:
                    ref_key = lot_state_ref["__ref"]
                    if ref_key in apollo_state:
                        lot["_resolved_lotState"] = apollo_state[ref_key]

                lots.append(lot)

        return lots

    def _get_item_id(self, item: dict) -> Optional[str]:
        """Extract unique item ID from lot data."""
        # Try various ID fields
        for field in ["id", "itemId", "eventItemId"]:
            if field in item and item[field]:
                return str(item[field])

        # Fallback: use Apollo cache key pattern
        typename = item.get("__typename", "")
        if typename == "Lot" and "id" in item:
            return f"lot-{item['id']}"

        return None

    def _enrich_lot_data(self, lot: dict) -> dict:
        """
        Enrich lot data with resolved references for complete raw payload.

        This ensures we store all available data including auction details.
        """
        enriched = dict(lot)

        # Add resolved auction data inline if present
        if "_resolved_auction" in enriched:
            auction = enriched.pop("_resolved_auction")
            enriched["auction_data"] = auction

        # Add resolved lot state inline if present
        if "_resolved_lotState" in enriched:
            lot_state = enriched.pop("_resolved_lotState")
            enriched["lot_state_data"] = lot_state

        # Clean up internal Apollo fields for storage
        # Keep __typename as it's useful for understanding the data
        if "auction" in enriched and isinstance(enriched["auction"], dict):
            if "__ref" in enriched["auction"]:
                enriched["auction_ref"] = enriched.pop("auction")["__ref"]

        if "lotState" in enriched and isinstance(enriched["lotState"], dict):
            if "__ref" in enriched["lotState"]:
                enriched["lot_state_ref"] = enriched.pop("lotState")["__ref"]

        return enriched

    def scrape_catalog(self) -> Iterator[tuple[str, dict]]:
        """
        Fetch the pages of one auction's catalogue that are not yet banked.

        Page-at-a-time rather than a scan that halts. The old loop stopped on a
        short page, but a short page is a throttling signal, not an end signal —
        so one bad page at position 5 abandoned pages 6..N, and the next attempt
        came a day later. Nine auctions closed at 46% average coverage that way,
        losing 7,467 lots that cannot be re-read once HiBid zeroes them.

        Here a failed page costs exactly one page: it stays out of `pages_done`,
        lands in `pages_failed`, and is simply outstanding work next time.

        `expected_pages` comes from discovery's lot_count, so the end of the
        catalogue is known rather than inferred. That removes the guessing the
        short-page and end-of-results heuristics were doing badly.
        """
        done = set(self.config.catalog_pages_done or [])
        attempts = self.config.catalog_page_attempts or {}
        expected = self.config.catalog_expected_pages or 0
        budget = self.config.catalog_page_budget

        # Sellers add lots after discovery saw the auction, so allow a little
        # past the advertised end; the probe stops as soon as a page is empty.
        ceiling = expected + CATALOG_PAGE_OVERRUN if expected else budget
        # A confirmed end beats lot_count: the catalogue cannot grow back.
        if self.config.catalog_end_page:
            ceiling = min(ceiling, self.config.catalog_end_page - 1)

        outstanding = [p for p in range(1, ceiling + 1) if p not in done]
        retired = [p for p in outstanding
                   if attempts.get(p, 0) >= CATALOG_MAX_PAGE_ATTEMPTS]

        # Never-tried pages first, then retries in order of least-tried. A page
        # that keeps failing must never starve pages that have never been read —
        # that is what pinned one 43-page catalogue at 15 pages.
        fresh = [p for p in outstanding if p not in attempts]
        stale = sorted(
            (p for p in outstanding
             if p in attempts and attempts[p] < CATALOG_MAX_PAGE_ATTEMPTS),
            key=lambda p: (attempts[p], p),
        )
        todo = (fresh + stale)[:budget]

        if retired:
            logger.warning(
                f"Auction {self.config.auction_id}: skipping {len(retired)} page(s) "
                f"retired after {CATALOG_MAX_PAGE_ATTEMPTS} attempts: {retired[:10]}"
            )

        if not todo:
            logger.info(
                f"Auction {self.config.auction_id}: nothing left to fetch "
                f"({len(done)} of {expected} pages banked, {len(retired)} retired)"
            )
            return

        logger.info(
            f"Scraping catalogue for auction {self.config.auction_id}: "
            f"{len(fresh)} fresh + {len(stale)} retry page(s) outstanding of "
            f"{ceiling}, have {len(done)}, taking {len(todo)}"
        )

        test_limit = self.config.test_limit if self.config.test_mode else float("inf")
        total_items = 0
        consecutive_failures = 0

        for page in todo:
            if total_items >= test_limit:
                logger.info(f"Test mode limit reached ({test_limit} items)")
                return

            lots = self._fetch_catalog_page(page)

            if lots is None:
                # One page lost. Record it and keep going — the whole point.
                self.stats.pages_failed.add(page)
                consecutive_failures += 1
                if consecutive_failures >= CATALOG_CONSECUTIVE_FAILURES:
                    logger.warning(
                        f"{consecutive_failures} pages failed in a row; HiBid is "
                        f"throttling this session. Banking "
                        f"{len(self.stats.pages_done)} page(s) and leaving "
                        f"{len(todo) - todo.index(page) - 1} for the next pass"
                    )
                    return
                logger.warning(f"Page {page} failed; continuing to the next")
                continue

            if not lots:
                # Empty. Believed as the end only if a lower page produced lots
                # this pass — otherwise the same response is just throttling.
                #
                # A catalogue shrinks as its auction closes, because closed lots
                # drop out of the listing, so the end routinely arrives long
                # before lot_count says it should. Chasing the difference is
                # what made late auctions return nothing at all instead of
                # returning what was still there.
                # A good page *below* this one — from this pass or any earlier
                # one — is what makes the stub an ending rather than a throttle.
                # Requiring it from this pass alone fails exactly when it
                # matters: a resumed pass starts deep and never fetches a lower
                # page, so the end could never be confirmed.
                banked_below = [
                    q for q in (self.stats.pages_done | set(done)) if q < page
                ]
                if banked_below:
                    logger.info(
                        f"Page {page} is past the end of the catalogue "
                        f"(lot_count implied {expected} pages, it actually ends "
                        f"here) — highest good page {max(banked_below)}"
                    )
                    self.stats.end_page = page
                    return
                logger.warning(
                    f"Page {page} came back empty with nothing banked yet; "
                    f"treating as unreadable rather than the end"
                )
                self.stats.pages_failed.add(page)
                consecutive_failures += 1
                if consecutive_failures >= CATALOG_CONSECUTIVE_FAILURES:
                    return
                continue

            # Only a banked page clears the run of failures.
            consecutive_failures = 0
            self.stats.pages_done.add(page)
            self.stats.pages_scraped += 1
            logger.info(f"Page {page}: {len(lots)} lots")

            for lot in lots:
                if total_items >= test_limit:
                    return
                item_id = self._get_item_id(lot)
                if not item_id:
                    logger.warning("Lot without ID, skipping")
                    self.stats.errors += 1
                    continue
                total_items += 1
                self.stats.items_found += 1
                yield (item_id, self._enrich_lot_data(lot))

            self._delay()

    def _fetch_catalog_page(self, page: int) -> Optional[list[dict]]:
        """
        One catalogue page, retried on its own.

        Returns the lots, [] when HiBid says there is nothing at this page, or
        None when the page could not be read — one page lost, not an ending.

        [] is deliberately ambiguous here and resolved by the caller: the
        183-byte stub means both "past the end" and "throttled", and only the
        caller knows whether a lower page succeeded this pass.
        """
        url = self._build_url(None, page)

        for attempt in range(1, CATALOG_PAGE_ATTEMPTS + 1):
            # The retry budget has to cover the confirmation budget. fetch_page
            # only returns its end-of-results stub after END_OF_RESULTS_
            # CONFIRMATIONS repeats, so asking for fewer retries than that means
            # the confirmations can never be spent: the stub is recognised every
            # time, never believed, and the page comes back None. That is what
            # retries=2 against 3 confirmations did — on 2026-08-22 every
            # catalogue whose page count had shrunk failed outright, because an
            # ending could only ever arrive dressed as a lost page.
            html = self.fetch_page(url, retries=END_OF_RESULTS_CONFIRMATIONS)

            if html == "":
                # Confirmed: three reads, a fresh browser each time, all of them
                # the stub. Report it empty rather than lost. [] is what lets the
                # caller apply the one test that separates a shrunken catalogue
                # from a throttled one — whether a lower page ever banked.
                logger.info(
                    f"Page {page}: end-of-results stub confirmed "
                    f"{END_OF_RESULTS_CONFIRMATIONS}x — reporting empty, not lost"
                )
                return []

            if not html:
                # HiBid throttles by degrading rather than erroring, so backing
                # off matters more than retrying quickly.
                if attempt < CATALOG_PAGE_ATTEMPTS:
                    backoff = SHORT_PAGE_BACKOFF_SECONDS * attempt
                    logger.warning(
                        f"Page {page} unreadable; backing off {backoff}s "
                        f"({attempt}/{CATALOG_PAGE_ATTEMPTS})"
                    )
                    time.sleep(backoff)
                    self._rotate_flaresolverr_session()
                continue

            state = self._extract_apollo_state(html)
            if not state:
                continue

            lots = self._extract_lots_from_apollo(state)
            if lots:
                return lots

            # A page with a rendered state and no lots is either past the end
            # or a throttled render. Only believe it after a retry.
            if attempt >= CATALOG_PAGE_ATTEMPTS:
                return []
            time.sleep(SHORT_PAGE_BACKOFF_SECONDS * attempt)
            self._rotate_flaresolverr_session()

        return None

    def scrape_category(self, category: Optional[str] = None) -> Iterator[tuple[str, dict]]:
        """
        Scrape all items from a radius search.

        Catalogue mode has its own loop — see scrape_catalog. This one keeps the
        heuristics it needs, because a radius search genuinely cannot know how
        many pages it has.
        """
        page = 1
        total_items = 0
        test_limit = self.config.test_limit if self.config.test_mode else float("inf")
        seen_ids = set()

        logger.info(
            f"Scraping category: {category or 'all'} "
            f"(zip: {self.config.zip_code}, radius: {self.config.radius_miles} miles)"
        )

        while total_items < test_limit:
            url = self._build_url(category, page)
            html = self.fetch_page(url)

            if html == "":
                logger.info(f"Reached the end of the results at page {page}")
                break

            if html is None:
                logger.error(
                    f"Giving up on page {page} after exhausting retries; "
                    f"results are incomplete beyond {total_items} items"
                )
                break

            apollo_state = self._extract_apollo_state(html)
            if not apollo_state:
                logger.warning(f"No Apollo state on page {page}")
                break

            lots = self._extract_lots_from_apollo(apollo_state)
            self.stats.pages_scraped += 1

            new_lots = []
            for lot in lots:
                item_id = self._get_item_id(lot)
                if item_id and item_id not in seen_ids:
                    seen_ids.add(item_id)
                    new_lots.append(lot)

            if not new_lots:
                logger.info(f"No new items found on page {page}")
                break

            logger.info(
                f"Page {page}: found {len(new_lots)} new items "
                f"(total lots in state: {len(lots)})"
            )

            for lot in new_lots:
                if total_items >= test_limit:
                    logger.info(f"Test mode limit reached ({test_limit} items)")
                    return
                item_id = self._get_item_id(lot)
                if item_id:
                    total_items += 1
                    self.stats.items_found += 1
                    yield (item_id, self._enrich_lot_data(lot))
                else:
                    logger.warning("Lot without ID, skipping")
                    self.stats.errors += 1

            if len(lots) < ITEMS_PER_PAGE // 2:
                logger.info("Partial page received, likely end of results")
                break

            page += 1
            self._delay()

    def scrape_all(self) -> Iterator[tuple[str, dict, Optional[str]]]:
        """
        Scrape all configured categories.

        Yields: (item_id, raw_json, category) tuples
        """
        # Catalogue mode is not a category sweep — it is a known set of pages.
        if self.config.auction_id:
            for item_id, item in self.scrape_catalog():
                yield (item_id, item, None)
            return

        categories = self.config.search_categories or [None]

        for category in categories:
            logger.info(f"Starting category: {category or 'all open lots'}")

            for item_id, item in self.scrape_category(category):
                yield (item_id, item, category)

            if self.config.test_mode and self.stats.items_found >= self.config.test_limit:
                logger.info("Test mode: stopping after reaching limit")
                break

            # Delay between categories
            if category != categories[-1]:
                self._delay()

    def get_stats(self) -> ScrapeStats:
        """Get current scraping statistics."""
        return self.stats

    def reset_stats(self) -> None:
        """Reset statistics for a new scrape run."""
        self.stats = ScrapeStats()
