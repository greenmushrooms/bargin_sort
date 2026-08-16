"""
HiBid auction discovery — the orchestrator's work queue.

One request to /auctions returns a page of auctions in the radius, each with
its close time, lot count and location. That is everything needed to decide
what to scrape and when, without touching a single lot.

Results come back sorted by close time ascending, which is what makes the
horizon stop cheap: once a page opens past the horizon, every later page is
further out still. In practice page one alone reaches about six days ahead.

Yields the same (item_id, payload, category) triple as the lot scrapers, so
the existing scrape_source task drives this unchanged.
"""

import logging
from datetime import datetime, timedelta
from typing import Iterator, Optional

import apollo
from config import Config
from fetcher import PageFetcher

logger = logging.getLogger(__name__)

HIBID_BASE_URL = "https://hibid.com"

# Auctions per page. HiBid caps the page at 100 regardless of what is asked,
# and reports that cap back as totalCount — so totalCount is a page length and
# not a universe size, and paging has to continue until a page comes back short.
AUCTIONS_PER_PAGE = 100

# Stop rather than page into next year. Only auctions closing within a day or
# two are ever acted on; the rest is headroom so a missed run can still catch
# up, and so the close-time history exists before an auction becomes urgent.
DEFAULT_HORIZON_DAYS = 14

# Guard against a pagination bug walking the whole site.
MAX_PAGES = 20


class AuctionDiscoveryScraper(PageFetcher):
    """Lists auctions near a postal code, with their close times."""

    source = "hibid_auctions"

    def _build_url(self, page: int) -> str:
        params = {
            "zip": self.config.zip_code,
            "miles": str(self.config.radius_miles),
            "apage": str(page),
            "ipp": str(AUCTIONS_PER_PAGE),
        }
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{HIBID_BASE_URL}/auctions?{query_string}"

    @staticmethod
    def is_end_of_results(html: str) -> bool:
        return apollo.is_end_of_results(html)

    @staticmethod
    def is_incomplete(html: str) -> bool:
        return not apollo.has_rendered_query(html, "auctionSearch")

    @staticmethod
    def _close_datetime(auction: dict) -> Optional[datetime]:
        """
        The auction's close, as HiBid states it.

        Naive local time with no offset. Left naive here on purpose: the
        timezone it belongs to is a property of where the auction is, which is
        a question silver answers with a geocode, not one the scraper should
        guess at.
        """
        raw = auction.get("bidCloseDateTime")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            logger.warning(f"Unparseable bidCloseDateTime: {raw!r}")
            return None

    def scrape_all(self) -> Iterator[tuple[str, dict, Optional[str]]]:
        """
        Page through auctions until the horizon or the end of the results.

        Yields: (auction_id, auction_payload, None)
        """
        horizon = datetime.now() + timedelta(days=self.config.discovery_horizon_days)
        test_limit = self.config.test_limit if self.config.test_mode else float("inf")

        logger.info(
            f"Discovering auctions: zip={self.config.zip_code}, "
            f"radius={self.config.radius_miles}mi, "
            f"horizon={self.config.discovery_horizon_days}d"
        )

        seen: set[str] = set()
        total = 0

        for page in range(1, MAX_PAGES + 1):
            html = self.fetch_page(self._build_url(page))

            if html == "":
                logger.info(f"Reached the end of the auction list at page {page}")
                return

            if html is None:
                # Not the end — the run is being cut short. Log it loudly so a
                # truncated discovery is not mistaken for a complete one.
                logger.error(
                    f"Giving up on auction page {page} after exhausting retries; "
                    f"discovery is incomplete beyond {total} auctions"
                )
                return

            state = apollo.extract_state(html)
            if not state:
                logger.warning(f"No Apollo state on auction page {page}")
                self.stats.errors += 1
                return

            # auctionSearch returns AuctionMatchType wrappers, not bare refs.
            refs = apollo.paged_result_refs(state, "auctionSearch", ref_field="auction")
            if refs is None:
                logger.warning(f"No auctionSearch node on page {page}")
                self.stats.errors += 1
                return

            self.stats.pages_scraped += 1
            page_closes: list[datetime] = []
            new_on_page = 0

            for ref in refs:
                auction = state.get(ref)
                if not isinstance(auction, dict):
                    continue

                auction_id = auction.get("id")
                if auction_id is None:
                    logger.warning(f"Auction without id at {ref}, skipping")
                    self.stats.errors += 1
                    continue

                item_id = str(auction_id)
                if item_id in seen:
                    continue
                seen.add(item_id)
                new_on_page += 1

                closes_at = self._close_datetime(auction)
                if closes_at:
                    page_closes.append(closes_at)

                total += 1
                self.stats.items_found += 1
                yield (item_id, auction, None)

                if total >= test_limit:
                    logger.info(f"Test mode limit reached ({test_limit} auctions)")
                    return

            logger.info(
                f"Auction page {page}: {new_on_page} new "
                f"(closing {min(page_closes).date() if page_closes else '?'} to "
                f"{max(page_closes).date() if page_closes else '?'})"
            )

            if not new_on_page:
                logger.info(f"No new auctions on page {page}; stopping")
                return

            # Sorted ascending by close, so a page that opens past the horizon
            # means every remaining page is further out still.
            if page_closes and min(page_closes) > horizon:
                logger.info(
                    f"Page {page} opens past the {self.config.discovery_horizon_days}d "
                    f"horizon; stopping"
                )
                return

            if len(refs) < AUCTIONS_PER_PAGE:
                logger.info("Short page received; end of the auction list")
                return

            self._delay()

        logger.warning(f"Hit the {MAX_PAGES}-page discovery cap")
