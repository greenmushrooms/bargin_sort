"""
One refresh, as a subprocess, for the Go server to call.

Why a subprocess and not a port of refresh.py to Go: the fetch needs
`PageFetcher` (FlareSolverr sessions, Cloudflare retry classes, and the
close-on-every-path fix that came out of forty-two orphaned Chrome processes),
`apollo` (HiBid ships its GraphQL cache in the page) and the scraper's
`Database` (bronze is monthly-partitioned, append-only, and a row only reaches
silver from a run marked `completed`). Reimplementing those in a second
language is how those failure modes come back, and a refresh already costs
5-10 seconds of real browser -- interpreter startup is not the expensive part.

Why a subprocess and not a long-lived sidecar: refresh.py already builds a
fetcher per call and closes it in a `finally`, so there is no warm session for
a sidecar to preserve. A one-shot process adds process exit as a second net
under the Chrome leak.

The one thing that does NOT survive the move: refresh.py's
`threading.Semaphore(2)`. Each call is its own process now, so that limit is
meaningless here and the Go caller owns it instead. Losing it is how a page of
refresh buttons and an impatient user recreate the forty-two browsers.

stdout is JSON and nothing else; logs go to stderr.
"""

import argparse
import json
import logging
import sys
from datetime import datetime


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--item-id", required=True)
    args = parser.parse_args()

    # Logs to stderr so stdout stays parseable. The Go caller surfaces stderr
    # only when the JSON fails to parse, which is the case where it helps.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)

    # refresh.py records the snapshot through the site's own pool, so it has
    # to exist even though this process serves no requests. Opened and closed
    # around the single call: a refresh holds a connection for the length of a
    # browser fetch, and leaving one behind per subprocess would exhaust the
    # server's connections faster than the browsers would.
    from db import close_pool, init_pool
    from refresh import refresh_lot

    init_pool()
    try:
        result = refresh_lot(args.source, args.item_id)
    finally:
        close_pool()

    json.dump(
        {
            "source": result.source,
            "item_id": result.item_id,
            "ok": result.ok,
            "error": result.error,
            "observed": result.observed,
        },
        sys.stdout,
        default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v),
    )
    sys.stdout.write("\n")

    # Exit 0 even when the refresh failed. A withdrawn lot and a Cloudflare
    # block are ordinary outcomes the user needs told about in the pane they
    # are looking at; the exit code is reserved for "this process broke".
    return 0


if __name__ == "__main__":
    sys.exit(main())
