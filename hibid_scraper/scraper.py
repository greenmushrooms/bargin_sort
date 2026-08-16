"""
HiBid Auction Scraper.

Scrapes auction items from HiBid based on zip code and radius.
Adapted from: https://github.com/jkoelmel/texas_auctions_scraper

HiBid uses Angular with Apollo GraphQL. The SSR response contains the
Apollo state cache in a <script id="hibid-state"> tag, which we parse
to extract lot data.
"""

import json
import logging
import re
from typing import Iterator, Optional

from bs4 import BeautifulSoup

from config import Config
from fetcher import PageFetcher, ScrapeStats

logger = logging.getLogger(__name__)

# HiBid base URL (works for nationwide search)
HIBID_BASE_URL = "https://hibid.com"

# Items per page (HiBid's default is 100, but we use smaller batches for stability)
ITEMS_PER_PAGE = 100

# How many times the end-of-results stub must repeat before it is believed.
END_OF_RESULTS_CONFIRMATIONS = 3


class HiBidScraper(PageFetcher):
    """Scraper for HiBid auction listings."""

    source = "hibid"

    def _build_url(self, category: Optional[str], page: int) -> str:
        """Build HiBid search URL."""
        # Base URL pattern for lots
        if category:
            url = f"{HIBID_BASE_URL}/lots/{category}/"
        else:
            url = f"{HIBID_BASE_URL}/lots/"

        # Query parameters
        params = {
            "status": "open",
            "zip": self.config.zip_code,
            "miles": str(self.config.radius_miles),
            "apage": str(page),
            "ipp": str(ITEMS_PER_PAGE),
        }

        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{url}?{query_string}"

    @staticmethod
    def is_end_of_results(html: str) -> bool:
        """
        Detect the stub HiBid serves for a page past the last one.

        It answers ~183 bytes with no state script at all, which is what
        separates a real end from a flaky render — those come back as a full
        page carrying a state but no `lotSearch` node, and are worth retrying.
        """
        return len(html) < 1000 and 'id="hibid-state"' not in html

    @staticmethod
    def is_incomplete(html: str) -> bool:
        """
        A page carrying an Apollo state but no search results is not finished.

        HiBid often serves a state populated only with site chrome and no
        `lotSearch` node. Those pages parse cleanly and yield zero lots, which
        pagination would otherwise read as the end of the results.
        """
        # A populated state serialises as {"apollo.state":{"Lot:123":...}, so the
        # opening brace-quote is what separates a real payload from an empty one.
        return not (
            'id="hibid-state"' in html
            and '"apollo.state":{"' in html
            and "lotSearch(" in html
        )

    def _extract_apollo_state(self, html: str) -> Optional[dict]:
        """
        Extract Apollo GraphQL state from HiBid's SSR response.

        HiBid embeds the Apollo cache in <script id="hibid-state">.
        """
        try:
            soup = BeautifulSoup(html, "html.parser")

            # Find the hibid-state script tag
            # `not state_script` would also be true for an empty tag, since a
            # Tag's truthiness is its child count — test for None explicitly.
            state_script = soup.find("script", {"id": "hibid-state"})
            if state_script is None or not state_script.string:
                logger.warning("No hibid-state script found in response")
                return None

            state_data = json.loads(state_script.string)
            return state_data.get("apollo.state", {})

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Apollo state JSON: {e}")
            self.stats.errors += 1
            return None
        except Exception as e:
            logger.error(f"Error extracting Apollo state: {e}")
            self.stats.errors += 1
            return None

    @staticmethod
    def _search_result_refs(apollo_state: dict) -> Optional[list[str]]:
        """
        The lot refs HiBid actually returned for this search, in its order.

        The Apollo cache also holds lots the page merely referenced — featured
        and related items from other auctions, carrying a stub auction with no
        id or location. Those are not search results and must not be mistaken
        for inventory in the requested radius.
        """
        root = apollo_state.get("ROOT_QUERY", {})
        for key, value in root.items():
            if key.startswith("lotSearch") and isinstance(value, dict):
                results = (value.get("pagedResults") or {}).get("results")
                if isinstance(results, list):
                    return [
                        r["__ref"]
                        for r in results
                        if isinstance(r, dict) and "__ref" in r
                    ]
        return None

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

            # Check if we got fewer lots than expected (end of results)
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
