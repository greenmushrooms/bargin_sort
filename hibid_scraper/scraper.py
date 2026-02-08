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
import random
import re
import time
from dataclasses import dataclass
from typing import Iterator, Optional

import requests
from bs4 import BeautifulSoup

from config import Config

logger = logging.getLogger(__name__)

# HiBid base URL (works for nationwide search)
HIBID_BASE_URL = "https://hibid.com"

# User agent to avoid being blocked
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Items per page (HiBid's default is 100, but we use smaller batches for stability)
ITEMS_PER_PAGE = 100


@dataclass
class ScrapeStats:
    """Statistics for a scrape operation."""

    items_found: int = 0
    items_added: int = 0
    items_updated: int = 0
    errors: int = 0
    pages_scraped: int = 0


class HiBidScraper:
    """Scraper for HiBid auction listings."""

    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })
        self.stats = ScrapeStats()

    def _delay(self) -> None:
        """Apply random delay between requests."""
        delay = random.uniform(
            self.config.request_delay_min, self.config.request_delay_max
        )
        logger.debug(f"Sleeping for {delay:.2f} seconds")
        time.sleep(delay)

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

    def _fetch_page(self, url: str, retries: int = 3) -> Optional[str]:
        """Fetch a page with retry logic."""
        for attempt in range(retries):
            try:
                logger.debug(f"Fetching: {url} (attempt {attempt + 1}/{retries})")
                response = self.session.get(url, timeout=30)
                response.raise_for_status()
                return response.text
            except requests.RequestException as e:
                logger.warning(f"Request failed: {e}")
                if attempt < retries - 1:
                    wait_time = (attempt + 1) * 5
                    logger.info(f"Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"All retries exhausted for {url}")
                    self.stats.errors += 1
                    return None
        return None

    def _extract_apollo_state(self, html: str) -> Optional[dict]:
        """
        Extract Apollo GraphQL state from HiBid's SSR response.

        HiBid embeds the Apollo cache in <script id="hibid-state">.
        """
        try:
            soup = BeautifulSoup(html, "html.parser")

            # Find the hibid-state script tag
            state_script = soup.find("script", {"id": "hibid-state"})
            if not state_script or not state_script.string:
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

        # Second pass: collect lots and resolve auction references
        for key, value in apollo_state.items():
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
            html = self._fetch_page(url)

            if not html:
                logger.warning(f"Failed to fetch page {page}")
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
