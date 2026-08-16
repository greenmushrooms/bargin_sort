"""
HiBid Auction Scraper.

Scrapes auction items from HiBid based on zip code and radius.
Adapted from: https://github.com/jkoelmel/texas_auctions_scraper

HiBid uses Angular with Apollo GraphQL. The SSR response contains the
Apollo state cache in a <script id="hibid-state"> tag, which we parse
to extract lot data.
"""

import logging
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
# auction's lot count says more are still due.
SHORT_PAGE_RETRIES = 3


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

    @staticmethod
    def _search_result_refs(apollo_state: dict) -> Optional[list[str]]:
        """
        The lot refs HiBid actually returned for this search, in its order.

        The Apollo cache also holds lots the page merely referenced — featured
        and related items from other auctions, carrying a stub auction with no
        id or location. Those are not search results and must not be mistaken
        for inventory in the requested radius.
        """
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

    def scrape_category(self, category: Optional[str] = None) -> Iterator[tuple[str, dict]]:
        """
        Scrape all items from a category.

        Yields: (item_id, raw_json) tuples
        """
        page = 1
        total_items = 0
        test_limit = self.config.test_limit if self.config.test_mode else float("inf")
        seen_ids = set()

        if self.config.auction_id:
            logger.info(f"Scraping catalogue for auction {self.config.auction_id}")
        else:
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
                # Not the end of the results — the run is being cut short, so
                # log it loudly enough that a truncated scrape is not read as a
                # complete one. _fetch_page has already counted the error.
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

            # A page that rendered only part of its lots is indistinguishable
            # from the last page: both come back short carrying a valid
            # lotSearch node, so `is_incomplete` cannot separate them and the
            # short-page rule below believes the wrong one. Observed live — a
            # 310-lot catalogue whose first page rendered 10 lots was captured
            # as 10 and reported success.
            #
            # The auction's own lot count is what breaks the tie. While more
            # lots are still due, a short page is a first reading rather than
            # the truth, and gets re-fetched with a fresh browser.
            expected = self.config.auction_lot_count or 0
            attempt = 0
            while (
                expected
                and len(lots) < ITEMS_PER_PAGE
                and total_items + len(lots) < expected
                and attempt < SHORT_PAGE_RETRIES
            ):
                attempt += 1
                logger.warning(
                    f"Page {page} rendered {len(lots)} lots with "
                    f"{expected - total_items} still due; re-fetching "
                    f"({attempt}/{SHORT_PAGE_RETRIES})"
                )
                self._rotate_flaresolverr_session()
                retry_html = self.fetch_page(url)
                if not retry_html:
                    break
                retry_state = self._extract_apollo_state(retry_html)
                if not retry_state:
                    break
                self.stats.pages_scraped += 1
                retry_lots = self._extract_lots_from_apollo(retry_state)
                if len(retry_lots) > len(lots):
                    lots = retry_lots

            # Filter out lots we've already seen (duplicates across pages)
            new_lots = []
            for lot in lots:
                item_id = self._get_item_id(lot)
                if item_id and item_id not in seen_ids:
                    seen_ids.add(item_id)
                    new_lots.append(lot)

            if not new_lots:
                logger.info(f"No new items found on page {page}")
                break

            logger.info(f"Page {page}: found {len(new_lots)} new items (total lots in state: {len(lots)})")

            for lot in new_lots:
                if total_items >= test_limit:
                    logger.info(f"Test mode limit reached ({test_limit} items)")
                    return

                item_id = self._get_item_id(lot)
                if item_id:
                    enriched_lot = self._enrich_lot_data(lot)
                    total_items += 1
                    self.stats.items_found += 1
                    yield (item_id, enriched_lot)
                else:
                    logger.warning("Lot without ID, skipping")
                    self.stats.errors += 1

            # A page that came back short is the last one. A catalogue is exact
            # about this — it returns a full page until it runs out — so the
            # check can be too, which keeps the scrape from asking for a page
            # past the end and paying three confirmation fetches to learn it.
            # The radius search is looser, hence the wider margin there.
            page_limit = ITEMS_PER_PAGE if self.config.auction_id else ITEMS_PER_PAGE // 2
            if len(lots) < page_limit:
                logger.info("Partial page received, likely end of results")
                break

            page += 1
            self._delay()

    def scrape_all(self) -> Iterator[tuple[str, dict, Optional[str]]]:
        """
        Scrape all configured categories.

        Yields: (item_id, raw_json, category) tuples
        """
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
