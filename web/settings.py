"""
Site configuration.

Deliberately the scraper's environment variable names (`DB_*`,
`FLARESOLVERR_*`), so one `.env` drives both programs and there is never a
question of which credentials the refresh is using. Anything specific to the
site is prefixed `WEB_`.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from scraper_bridge import Config

WEB_DIR = Path(__file__).resolve().parent
REPO_DIR = WEB_DIR.parent

# web/.env first so the site can point at a different database than a scraper
# run happening on the same machine; the repo root is the fallback.
for candidate in (WEB_DIR / ".env", REPO_DIR / ".env"):
    if candidate.is_file():
        load_dotenv(candidate)
        break


def scraper_config() -> Config:
    """
    The scraper's own Config, for the refresh path.

    Built per call rather than cached at import: it holds the database
    password, and a long-lived module global is the sort of thing that ends up
    in a traceback.
    """
    return Config.from_env()


class Settings:
    """Site-only settings. Database settings live on the scraper Config."""

    host: str = os.getenv("WEB_HOST", "127.0.0.1")
    port: int = int(os.getenv("WEB_PORT", "7780"))

    # How many lots the ad-hoc search will return. There are ~75k open lots and
    # a bare "usb" matches thousands of them; a cap keeps a careless query from
    # rendering a page nobody can read.
    search_limit: int = int(os.getenv("WEB_SEARCH_LIMIT", "200"))

    # Refresh is a real browser fetch through FlareSolverr and takes seconds.
    # The UI blocks on it, so the ceiling is short enough that a wedged
    # FlareSolverr shows an error rather than an eternal spinner.
    refresh_timeout_s: int = int(os.getenv("WEB_REFRESH_TIMEOUT_S", "90"))


settings = Settings()
