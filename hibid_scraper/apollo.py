"""
Reading HiBid's server-rendered Apollo cache.

Every HiBid page — lot search, auction search, a single catalogue — ships its
GraphQL cache in a <script id="hibid-state"> tag. The shape is always the same:
a flat map of "Type:id" keys, with the query's own results held in ROOT_QUERY
as a list of __ref pointers into that map.

The refs matter. The cache also holds objects the page merely referenced —
featured lots from other auctions, promoted listings from other provinces —
which are indistinguishable from results once you are just iterating keys. Only
the ROOT_QUERY node says what the query actually returned.
"""

import json
import logging
from typing import Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def extract_state(html: str) -> Optional[dict]:
    """
    Pull the Apollo cache out of a rendered page.

    Returns None when the page carries no usable state; the caller decides
    whether that is a retry or a genuine end.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")

        # `not state_script` would also be true for an empty tag, since a Tag's
        # truthiness is its child count — test for None explicitly.
        state_script = soup.find("script", {"id": "hibid-state"})
        if state_script is None or not state_script.string:
            logger.warning("No hibid-state script found in response")
            return None

        return json.loads(state_script.string).get("apollo.state", {})

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse Apollo state JSON: {e}")
        return None
    except Exception as e:
        logger.error(f"Error extracting Apollo state: {e}")
        return None


def paged_result_refs(
    state: dict, query_prefix: str, ref_field: Optional[str] = None
) -> Optional[list[str]]:
    """
    The refs a paged query actually returned, in the order it returned them.

    `query_prefix` is the ROOT_QUERY field name — "lotSearch" or
    "auctionSearch" — which arrives suffixed with its serialised arguments.

    `ref_field` names the wrapper field holding the pointer, for queries that
    return match objects instead of bare refs. lotSearch yields
    {"__ref": "Lot:1"} directly; auctionSearch wraps each hit in an
    AuctionMatchType and puts the pointer under "auction".

    None means the query node was absent, which is a different thing from an
    empty result and has to stay distinguishable: absent means the page did not
    finish rendering, empty means there is nothing left to page through.
    """
    root = state.get("ROOT_QUERY", {})
    for key, value in root.items():
        if key.startswith(query_prefix) and isinstance(value, dict):
            results = (value.get("pagedResults") or {}).get("results")
            if isinstance(results, list):
                refs = []
                for result in results:
                    if not isinstance(result, dict):
                        continue
                    node = result.get(ref_field) if ref_field else result
                    if isinstance(node, dict) and "__ref" in node:
                        refs.append(node["__ref"])
                return refs
    return None


def has_rendered_query(html: str, query_prefix: str) -> bool:
    """
    Whether a page carries a populated cache containing the expected query.

    HiBid often serves a state populated only with site chrome. Those pages
    parse cleanly and yield nothing, which pagination would otherwise read as
    the end of the results.
    """
    # A populated state serialises as {"apollo.state":{"Lot:123":..., so the
    # opening brace-quote is what separates a real payload from an empty one.
    return (
        'id="hibid-state"' in html
        and '"apollo.state":{"' in html
        and f"{query_prefix}(" in html
    )


def is_end_of_results(html: str) -> bool:
    """
    Detect the stub HiBid serves for a page past the last one.

    It answers ~183 bytes with no state script at all, which is what separates
    a real end from a flaky render — those come back as a full page carrying a
    state but no query node, and are worth retrying.
    """
    return len(html) < 1000 and 'id="hibid-state"' not in html
