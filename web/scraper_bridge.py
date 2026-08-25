"""
The one place this project reaches into `hibid_scraper/`.

The site and the scraper are separate programs that happen to share a
repository, and the refresh button is the only thing that needs both. Rather
than let `sys.path` surgery spread through the codebase, every import of
scraper code goes through here — so the coupling is a single file you can read,
and `grep scraper_bridge` answers "what does the site depend on the scraper
for" completely.

What is borrowed, and why none of it is worth reimplementing:

  * `PageFetcher`  — FlareSolverr sessions, Cloudflare retries, and the
                     browser-leak fix that came out of forty-two orphaned
                     Chrome processes. Three distinct failure modes, all of
                     them learned the hard way.
  * `apollo`       — HiBid ships its GraphQL cache in the page; reading it
                     correctly means knowing that ROOT_QUERY is the only node
                     that says what the query actually returned.
  * `Database`     — bronze is monthly-partitioned and append-only, and a row
                     is only admitted to silver if its run is `completed`.
                     Writing there by hand would get one of those wrong.
  * `Config`       — same `DB_*` and `FLARESOLVERR_*` environment names the
                     scraper uses, so one .env configures both.
"""

import sys
from pathlib import Path

SCRAPER_DIR = Path(__file__).resolve().parent.parent / "hibid_scraper"

if not SCRAPER_DIR.is_dir():  # pragma: no cover - a broken checkout
    raise RuntimeError(f"hibid_scraper/ not found beside web/ (looked in {SCRAPER_DIR})")

# Prepend rather than append: the scraper's modules have flat, generic names
# (`config`, `database`, `fetcher`) and losing the race to a same-named module
# elsewhere on the path would fail confusingly and late.
if str(SCRAPER_DIR) not in sys.path:
    sys.path.insert(0, str(SCRAPER_DIR))

import apollo  # noqa: E402
from config import Config  # noqa: E402
from database import Database  # noqa: E402
from fetcher import PageFetcher  # noqa: E402

__all__ = ["apollo", "Config", "Database", "PageFetcher", "SCRAPER_DIR"]
