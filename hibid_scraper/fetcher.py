"""
Shared page fetching for source scrapers.

Transport only: Cloudflare handling, retries, and browser-session lifecycle.
What counts as a good page differs per site, so the two judgements a fetch has
to make are hooks for subclasses:

  * is_incomplete()     — the server answered, but with a page that has not
                          finished rendering. Retry; the session is fine.
  * is_end_of_results() — there is genuinely nothing more to page through.

Getting the second one wrong is expensive and quiet: a false end looks exactly
like a clean finish, so a stub has to repeat before it is believed.
"""

import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import requests

from config import Config

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# How many times an end-of-results signal must repeat before it is believed.
END_OF_RESULTS_CONFIRMATIONS = 3


class IncompletePageError(Exception):
    """The server answered before the page had finished rendering."""


class CloudflareBlockedError(requests.RequestException):
    """
    Cloudflare refused a direct request with a 403.

    Only reachable with FLARESOLVERR_URL unset, since a configured FlareSolverr
    drives a real browser and clears the challenge. Whether a direct request is
    blocked varies with the caller's IP reputation rather than being permanent.
    """


@dataclass
class ScrapeStats:
    """Statistics for a scrape operation."""

    items_found: int = 0
    items_added: int = 0
    items_updated: int = 0
    errors: int = 0
    pages_scraped: int = 0
    # Highest page that yielded lots, so a partial catalogue run can be
    # resumed rather than restarted.
    last_page: int = 0
    # Set only when pagination ran past the end of the catalogue, which is the
    # one case where the resume point should go backwards to 0. Without it, a
    # pass that simply fetched nothing is indistinguishable from a deliberate
    # reset, and progress gets clobbered — auction 767653 re-read the same 700
    # lots four times because its page-12 progress was overwritten with 7.
    progress_reset: bool = False
    # Pages banked and pages lost this pass. A lost page is one page of work
    # outstanding, not the end of the catalogue.
    pages_done: set = field(default_factory=set)
    pages_failed: set = field(default_factory=set)


class PageFetcher:
    """Fetches pages through FlareSolverr, with retries and session reuse."""

    # Source identifier, also used to name FlareSolverr browser sessions.
    source = "source"

    # Whether to fetch directly and fall back to FlareSolverr only on a block.
    #
    # HiBid is routed through FlareSolverr unconditionally because Cloudflare
    # blocks it unpredictably and losing pages to a 403 is worse than being
    # slow. Sites that are not challenged should set this: driving a browser
    # costs seconds per page, and FlareSolverr's solver is not always healthy —
    # under Chrome 142 it throws "Proxy is not a constructor" on some sites
    # while plain requests to them succeed.
    prefer_direct = False

    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })
        self.stats = ScrapeStats()
        self._flaresolverr_session: Optional[str] = None

    # -- hooks ---------------------------------------------------------------

    def is_incomplete(self, html: str) -> bool:
        """True when the page rendered partially and is worth retrying."""
        return False

    def is_end_of_results(self, html: str) -> bool:
        """True when the page says there is nothing left to page through."""
        return False

    # -- transport -----------------------------------------------------------

    def _delay(self) -> None:
        """Apply random delay between requests."""
        delay = random.uniform(
            self.config.request_delay_min, self.config.request_delay_max
        )
        logger.debug(f"Sleeping for {delay:.2f} seconds")
        time.sleep(delay)

    def _flaresolverr_command(self, payload: dict) -> dict:
        """Send a command to FlareSolverr and return its JSON envelope."""
        response = self.session.post(
            self.config.flaresolverr_url,
            json=payload,
            timeout=(self.config.flaresolverr_timeout_ms / 1000) + 30,
        )
        response.raise_for_status()
        return response.json()

    def _ensure_flaresolverr_session(self) -> None:
        """
        Create a reusable FlareSolverr browser session.

        Reusing one browser across pages keeps the solved Cloudflare cookies
        warm, so only the first request pays the challenge cost. Failure here
        is not fatal — requests fall back to a throwaway browser each time.
        """
        if self._flaresolverr_session:
            return

        session_id = f"{self.source}-{uuid.uuid4().hex[:8]}"
        payload = {"cmd": "sessions.create", "session": session_id}
        if self.config.flaresolverr_proxy:
            payload["proxy"] = {"url": self.config.flaresolverr_proxy}
        try:
            envelope = self._flaresolverr_command(payload)
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"Could not create FlareSolverr session: {e}")
            return

        if envelope.get("status") != "ok":
            logger.warning(f"FlareSolverr refused session: {envelope.get('message')}")
            return

        self._flaresolverr_session = session_id
        logger.info(f"FlareSolverr session created: {session_id}")

    def _rotate_flaresolverr_session(self) -> None:
        """Discard the browser session so the next request builds a fresh one."""
        if not self._flaresolverr_session:
            return
        try:
            self._flaresolverr_command(
                {"cmd": "sessions.destroy", "session": self._flaresolverr_session}
            )
        except (requests.RequestException, ValueError) as e:
            logger.debug(f"Could not destroy session {self._flaresolverr_session}: {e}")
        finally:
            self._flaresolverr_session = None

    def _fetch_via_flaresolverr(self, url: str) -> str:
        """Fetch a page through FlareSolverr, which solves Cloudflare's check."""
        self._ensure_flaresolverr_session()

        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": self.config.flaresolverr_timeout_ms,
        }
        if self._flaresolverr_session:
            payload["session"] = self._flaresolverr_session
        elif self.config.flaresolverr_proxy:
            # No session means a throwaway browser, which does not inherit the
            # session's proxy — so it has to be set per request or that fetch
            # would silently go out on the real IP.
            payload["proxy"] = {"url": self.config.flaresolverr_proxy}

        envelope = self._flaresolverr_command(payload)

        if envelope.get("status") != "ok":
            raise requests.RequestException(
                f"FlareSolverr error: {envelope.get('message')}"
            )

        solution = envelope.get("solution") or {}
        if solution.get("status") != 200:
            raise requests.RequestException(
                f"FlareSolverr got HTTP {solution.get('status')} for {url}"
            )

        return solution.get("response") or ""

    def _fetch_direct(self, url: str) -> str:
        """Fetch a page with a plain HTTP request."""
        response = self.session.get(url, timeout=30)
        if response.status_code == 403:
            raise CloudflareBlockedError(f"Cloudflare returned 403 for {url}")
        response.raise_for_status()
        return response.text

    def fetch_page(self, url: str, retries: int = 5) -> Optional[str]:
        """
        Fetch a page with retry logic.

        Returns the HTML, an empty string for a confirmed end of results, or
        None when the retries ran out — the caller must tell those apart, since
        only the last one means the scrape is incomplete.
        """
        stub_attempts = 0

        for attempt in range(retries):
            try:
                logger.debug(f"Fetching: {url} (attempt {attempt + 1}/{retries})")

                if self.prefer_direct:
                    try:
                        html = self._fetch_direct(url)
                    except CloudflareBlockedError as e:
                        if not self.config.flaresolverr_url:
                            raise
                        logger.warning(f"{e}; falling back to FlareSolverr")
                        html = self._fetch_via_flaresolverr(url)
                elif self.config.flaresolverr_url:
                    html = self._fetch_via_flaresolverr(url)
                else:
                    html = self._fetch_direct(url)

                if self.is_end_of_results(html):
                    # An end signal also shows up transiently mid-pagination,
                    # and believing the first one truncates the scrape silently.
                    stub_attempts += 1
                    if stub_attempts < END_OF_RESULTS_CONFIRMATIONS:
                        logger.info(
                            f"End-of-results signal for {url}; confirming "
                            f"({stub_attempts}/{END_OF_RESULTS_CONFIRMATIONS})"
                        )
                        self._rotate_flaresolverr_session()
                        time.sleep(2 * stub_attempts)
                        continue
                    logger.info(f"Confirmed end of results at {url}")
                    return ""

                if self.is_incomplete(html):
                    raise IncompletePageError(f"Page not fully rendered: {url}")

                return html

            except IncompletePageError as e:
                logger.warning(f"{e}; retrying ({attempt + 1}/{retries})")
                if attempt < retries - 1:
                    # A browser that keeps handing back the same unrendered page
                    # is probably serving it from cache, so start a clean one.
                    if attempt >= 1:
                        self._rotate_flaresolverr_session()
                    time.sleep(2 + 2 * attempt)
                    continue
                logger.error(f"Never got a rendered page for {url}")
                self.stats.errors += 1
                return None

            except (requests.RequestException, ValueError) as e:
                logger.warning(f"Request failed: {e}")
                # A dead browser session fails every remaining retry, so drop it
                # and let the next attempt build a fresh one. FlareSolverr reaps
                # the orphan on its own idle timeout.
                self._flaresolverr_session = None
                if attempt < retries - 1:
                    wait_time = (attempt + 1) * 5
                    logger.info(f"Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"All retries exhausted for {url}")
                    self.stats.errors += 1
                    return None
        return None

    def get_stats(self) -> ScrapeStats:
        return self.stats

    def reset_stats(self) -> None:
        self.stats = ScrapeStats()

    def close(self) -> None:
        """Release the FlareSolverr browser session and the connection pool."""
        if self._flaresolverr_session:
            logger.info(f"Closing FlareSolverr session {self._flaresolverr_session}")
            self._rotate_flaresolverr_session()
        self.session.close()
