"""
Police Auctions Canada scraper.

Server-rendered ASP.NET behind Cloudflare, so there is no embedded state blob
to mine — listings are parsed straight out of the markup. Each /Browse page
carries 20 `div.panel.listing` cards.

Unlike HiBid there is no radius search: every lot is held at one location, so
the whole site is a single pickup point and geography is not a scrape
parameter. Distance is applied downstream in silver_enhanced, the same as for
HiBid, so both sources answer the same question.
"""

import logging
import re
from typing import Iterator, Optional

from bs4 import BeautifulSoup

from fetcher import PageFetcher

logger = logging.getLogger(__name__)

BASE_URL = "https://policeauctionscanada.com"

# Single warehouse, so location is a constant rather than a per-listing field.
# Postal code is from their own contact page, not inferred — a wrong one would
# silently shift every distance in the catalogue.
LOCATION = {
    "address": "Toronto, ON M8Z 2X3",
    "city": "Toronto",
    "province": "ON",
    "postal_code": "M8Z 2X3",
    "country_code": "CA",
}

LISTING_ID_RE = re.compile(r"/Listing/Details/(\d+)/")


class PoliceAuctionsScraper(PageFetcher):
    """Scraper for policeauctionscanada.com listings."""

    source = "police_auctions"

    # The site answers plain requests fine, and FlareSolverr's solver currently
    # crashes on it, so go direct and keep the browser only as a fallback.
    prefer_direct = True

    def _build_url(self, page: int) -> str:
        return f"{BASE_URL}/Browse?page={page}"

    @staticmethod
    def is_end_of_results(html: str) -> bool:
        """A page past the last one renders the shell with no listing cards."""
        return 'class="panel panel-default clearfix listing"' not in html

    @staticmethod
    def _money(node) -> Optional[float]:
        """
        Pull a number out of a price element.

        Prices render as `$<span class="NumberPart">2.74</span>`, so the digits
        live in a child span rather than in the element's own text.
        """
        if node is None:
            return None
        part = node.find(class_="NumberPart")
        text = (part or node).get_text(strip=True).replace("$", "").replace(",", "")
        try:
            return float(text)
        except ValueError:
            return None

    def _parse_listing(self, card) -> Optional[tuple[str, dict]]:
        """Turn one listing card into (item_id, payload)."""
        link = card.find("a", href=LISTING_ID_RE)
        if link is None:
            return None

        match = LISTING_ID_RE.search(link["href"])
        if not match:
            return None
        item_id = match.group(1)

        title_el = card.select_one("h1.title a") or card.select_one("h1.title")
        subtitle_el = card.select_one("h2.subtitle a")
        image_el = card.find("img")
        time_el = card.find(attrs={"data-action-time": True})

        # `sold` is only meaningful once bidding has ended; while a lot is live
        # the element is present but hidden, so presence alone proves nothing.
        ended = time_el is None or "awe-hidden" not in (
            card.select_one(".awe-rt-ShowStatusSuccessful") or {}
        ).get("class", ["awe-hidden"])

        payload = {
            "listing_id": item_id,
            "title": title_el.get_text(strip=True) if title_el else None,
            "seller": subtitle_el.get_text(strip=True) if subtitle_el else None,
            "url": f"{BASE_URL}{link['href']}",
            "image_url": image_el["src"] if image_el and image_el.has_attr("src") else None,
            "current_price": self._money(card.select_one(".awe-rt-CurrentPrice")),
            "minimum_bid": self._money(card.select_one(".awe-rt-MinimumBid")),
            "ends_at": time_el["data-action-time"] if time_el else None,
            "has_ended": ended,
            "location": LOCATION,
        }
        return item_id, payload

    def scrape_all(self) -> Iterator[tuple[str, dict, Optional[str]]]:
        """
        Page through the whole catalogue.

        Yields (item_id, payload, category) so the shape matches the HiBid
        scraper and the same flow can drive either. Category is always None —
        the browse listing is not split by category.
        """
        page = 1
        seen: set[str] = set()
        test_limit = self.config.test_limit if self.config.test_mode else float("inf")

        logger.info("Scraping Police Auctions Canada")

        while len(seen) < test_limit:
            html = self.fetch_page(self._build_url(page))

            if html == "":
                logger.info(f"Reached the end of the results at page {page}")
                break
            if html is None:
                logger.error(
                    f"Giving up on page {page} after exhausting retries; "
                    f"results are incomplete beyond {len(seen)} items"
                )
                break

            soup = BeautifulSoup(html, "html.parser")
            cards = soup.select("div.panel.listing")
            self.stats.pages_scraped += 1

            new_on_page = 0
            for card in cards:
                parsed = self._parse_listing(card)
                if parsed is None:
                    logger.warning("Listing card without an id, skipping")
                    self.stats.errors += 1
                    continue

                item_id, payload = parsed
                if item_id in seen:
                    continue
                seen.add(item_id)
                new_on_page += 1

                self.stats.items_found += 1
                yield (item_id, payload, None)

                if len(seen) >= test_limit:
                    logger.info(f"Test mode limit reached ({test_limit} items)")
                    return

            logger.info(f"Page {page}: found {new_on_page} new items ({len(cards)} cards)")

            # Every card already seen means pagination is looping rather than
            # advancing, which the end-of-results check would never catch.
            if new_on_page == 0:
                logger.info(f"No new items on page {page}, stopping")
                break

            page += 1
            self._delay()
