"""
bargin_sort — the triage site.

An htmx server: every route returns either the whole page or one fragment of
it, and the browser swaps fragments in. There is no client-side state and no
build step, which for a single-user local tool is the difference between
something that still runs in a year and something that needs a `npm install`
first.

Run it with:

    cd web && uv run uvicorn app:app --reload --port 7780
"""

import logging
import threading
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
import lots
import refresh as refresh_mod
from settings import WEB_DIR, settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_pool()

    # The first match build costs ~4.5 seconds. Doing it on a background
    # thread means the port is listening immediately and the first page load
    # renders — showing "building" on a cold database, or the previous cache
    # while a new one lands — rather than hanging on a blank screen.
    threading.Thread(target=_warm_matches, name="match-warm", daemon=True).start()
    yield
    db.close_pool()


def _warm_matches() -> None:
    try:
        lots.ensure_matches()
    except Exception:
        logger.exception("initial wishlist match build failed")


app = FastAPI(title="bargin_sort", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

templates = Jinja2Templates(directory=WEB_DIR / "templates")


# ---------------------------------------------------------------------------
# Presentation helpers, registered as Jinja filters
# ---------------------------------------------------------------------------


def money(value) -> str:
    """A bid, formatted the way the auction sites format it."""
    if value is None:
        return "—"
    number = float(value)
    return f"${number:,.0f}" if number == int(number) else f"${number:,.2f}"


def closes(value) -> str:
    """
    A close time as a human reads it: how long is left, then the wall clock.

    Toronto rather than UTC because every auction in the corpus is within
    50 km of Toronto, and a close time in the wrong zone is worse than no
    close time — it is a bid placed four hours late.
    """
    if value is None:
        return "—"
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    local = value.astimezone(ZoneInfo("America/Toronto"))
    delta = value - datetime.now(timezone.utc)
    seconds = delta.total_seconds()
    if seconds <= 0:
        return f"closed {local:%a %H:%M}"
    if seconds < 3600:
        return f"{int(seconds // 60)}m · {local:%H:%M}"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h · {local:%a %H:%M}"
    return f"{int(seconds // 86400)}d · {local:%a %H:%M}"


def ago(value) -> str:
    """How long ago something happened, at the resolution that matters."""
    if value is None:
        return "never"
    from datetime import datetime, timezone

    seconds = (datetime.now(timezone.utc) - value).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


templates.env.filters["money"] = money
templates.env.filters["closes"] = closes
templates.env.filters["ago"] = ago
templates.env.globals["REVIEW_LABELS"] = lots.REVIEW_LABELS
templates.env.globals["REVIEW_STATUSES"] = lots.REVIEW_STATUSES

# What the two new badges say. Short, because they sit on a 430px list row —
# the long form is the title attribute.
templates.env.globals["CONDITION_LABELS"] = {
    "sealed": "SEALED", "new": "open box", "tested": "tested",
    "good": "very good", "used": "used", "untested": "UNTESTED",
    "broken": "NOT WORKING", "asis": "AS-IS", "unknown": "?",
}
templates.env.globals["CONDITION_HELP"] = {
    "sealed": "Factory sealed — the closed box you are looking for",
    "new": "New, but the box has been opened",
    "tested": "Seller states it is functional; loose unit, not a sealed box",
    "good": "Graded very good / like new, with no working-order claim",
    "used": "Used, or shows signs of handling",
    "untested": "Seller says Power: Untested — a gamble, not stock",
    "broken": "Seller says it does not work",
    "asis": "Sold as-is, no recourse",
    "unknown": "The listing says nothing about condition",
}
templates.env.globals["TIER_HELP"] = {
    "premium": "Parts, firmware and a service path years from now",
    "solid": "Good hardware, thinner support",
    "budget": "Works, but treat it as disposable",
    "whitelabel": "No parts, no service, no firmware — untested means landfill",
}


# ---------------------------------------------------------------------------
# Filter state
# ---------------------------------------------------------------------------


class Filters:
    """
    The list's query parameters, in one object.

    Carried through every fragment so that a review click, a refresh and a
    re-sort all come back into the same filtered view. Losing the filter on an
    action is the single most annoying thing an htmx UI can do.
    """

    def __init__(
        self,
        slug: Optional[str] = None,
        status: Optional[str] = None,
        budget: bool = False,
        hide_judged: bool = False,
        boxed: bool = False,
        sort: str = "priority",
        q: str = "",
    ):
        self.slug = slug or None
        self.status = status or None
        self.budget = budget
        self.hide_judged = hide_judged
        self.boxed = boxed
        self.sort = sort if sort in lots.SORT_ORDERS else "priority"
        self.q = (q or "").strip()

    @property
    def searching(self) -> bool:
        return bool(self.q)


def _filters(
    slug: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    budget: Optional[str] = Query(None),
    hide_judged: Optional[str] = Query(None),
    boxed: Optional[str] = Query(None),
    # Closing soonest by default. Priority ranks by how much a category is
    # wanted, which is the right question when browsing and the wrong one
    # when something is about to close -- and the thing that goes wrong is
    # silent: a lot forty rows down is a lot you never saw.
    sort: str = Query("closing"),
    q: str = Query(""),
) -> Filters:
    return Filters(
        slug=slug,
        status=status,
        budget=bool(budget),
        hide_judged=bool(hide_judged),
        boxed=bool(boxed),
        sort=sort,
        q=q,
    )


def _rows_for(filters: Filters) -> tuple[list[dict], str]:
    """The list body for the current filter — matches, or a search."""
    if filters.searching:
        return lots.search_lots(filters.q, limit=settings.search_limit)
    rows = lots.list_matches(
        slug=filters.slug,
        status=filters.status,
        budget_only=filters.budget,
        hide_judged=filters.hide_judged,
        boxed_only=filters.boxed,
        sort=filters.sort,
    )
    return rows, "wishlist"


def _list_context(request: Request, filters: Filters) -> dict:
    rows, mode = _rows_for(filters)
    return {
        "request": request,
        "rows": rows,
        "mode": mode,
        "filters": filters,
        "limit": settings.search_limit,
    }


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request, filters: Filters = Depends(_filters)):
    """
    The whole page. Every later interaction replaces a fragment of it.

    The filter is read from the querystring here too, so a bookmarked
    `?slug=mini_pc_quicksync&budget=1` opens on that view — which is the point
    of keeping the state in the URL rather than in the session.
    """
    context = _list_context(request, filters)
    context.update(
        groups=lots.wishlist_groups(),
        counts=lots.review_counts(),
        build=lots.build_state(),
        stale=lots.matches_stale(),
        active_slug=filters.slug,
    )
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/lots", response_class=HTMLResponse)
def lot_list(request: Request, filters: Filters = Depends(_filters)):
    return templates.TemplateResponse(request, "lot_list.html", _list_context(request, filters))


@app.get("/sidebar", response_class=HTMLResponse)
def sidebar(request: Request, slug: Optional[str] = Query(None)):
    return templates.TemplateResponse(
        request,
        "sidebar.html",
        {
            "request": request,
            "groups": lots.wishlist_groups(),
            "counts": lots.review_counts(),
            "build": lots.build_state(),
            "stale": lots.matches_stale(),
            "active_slug": slug,
        },
    )


@app.get("/lot/{source}/{item_id}", response_class=HTMLResponse)
def lot_detail(request: Request, source: str, item_id: str):
    row = lots.lot_detail(source, item_id)
    if row is None:
        return templates.TemplateResponse(
            request, "lot_gone.html", {"source": source, "item_id": item_id}
        )
    return templates.TemplateResponse(request, "lot_detail.html", {"lot": row})


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

# htmx fires this on <body> when it sees the header, and both the list and the
# sidebar listen for it. A verdict changes a row's badge, the unread count
# beside its wishlist row and the counts on the filter chips — all outside the
# fragment that was swapped — so the alternative is three out-of-band swaps
# hand-assembled per response.
_LOT_UPDATED = {"HX-Trigger": "lot-updated"}


@app.post("/lot/{source}/{item_id}/refresh", response_class=HTMLResponse)
def lot_refresh(request: Request, source: str, item_id: str):
    """
    Re-fetch a lot and re-render its pane.

    Synchronous: it takes several seconds, and the user pressed the button and
    is watching. A background job would need a polling endpoint and a spinner
    that can lie; htmx's own request indicator already says "working" honestly.
    """
    result = refresh_mod.refresh_lot(source, item_id)
    row = lots.lot_detail(source, item_id)
    if row is None:
        return templates.TemplateResponse(
            request, "lot_gone.html", {"source": source, "item_id": item_id}
        )
    return templates.TemplateResponse(
        request,
        "lot_detail.html",
        {"lot": row, "just_refreshed": result},
        headers=_LOT_UPDATED,
    )


@app.post("/lot/{source}/{item_id}/review", response_class=HTMLResponse)
def lot_review(
    request: Request,
    source: str,
    item_id: str,
    status: str = Form(...),
    max_bid: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    """
    Record a verdict, and re-render the pane it was set from.

    Clicking the status a lot already has clears it instead, putting the lot
    back in the unread queue. A toggle rather than a separate "unread" button:
    the mistake being undone is almost always the click that just happened.
    """
    existing = db.query_one(
        "SELECT status FROM web.lot_review WHERE source = %s AND item_id = %s",
        (source, item_id),
    )
    if existing and existing["status"] == status and not (notes or max_bid):
        lots.clear_review(source, item_id)
    else:
        lots.set_review(
            source,
            item_id,
            status,
            max_bid=float(max_bid) if max_bid else None,
            notes=notes.strip() if notes and notes.strip() else None,
        )

    row = lots.lot_detail(source, item_id)
    if row is None:
        return HTMLResponse("", headers=_LOT_UPDATED)
    return templates.TemplateResponse(
        request, "lot_detail.html", {"lot": row}, headers=_LOT_UPDATED
    )


@app.post("/rescan", response_class=HTMLResponse)
def rescan(request: Request, slug: Optional[str] = Query(None)):
    """
    Re-run the matcher now.

    Normally unnecessary — the cache rebuilds itself when fct_lots or the
    wishlist seed moves. The button exists for the case that motivates it:
    editing wishlist.csv, running `dbt seed`, and wanting to see immediately
    whether the new regex catches what it was written for.
    """
    lots.rebuild_matches()
    return sidebar(request, slug=slug)


@app.get("/healthz")
def healthz():
    return {"ok": True, "matches": (lots.build_state() or {}).get("match_count")}


@app.get("/favicon.ico")
def favicon():
    return RedirectResponse("/static/favicon.svg")


# `uv run uvicorn app:app` ignores WEB_HOST/WEB_PORT — uvicorn takes those from
# its own flags. Running the module directly is what makes the .env settings
# mean something, and it is the shorter thing to type:
#
#     uv run python app.py
#
if __name__ == "__main__":
    import uvicorn

    # reload is deliberately off here. It needs an import string rather than the
    # app object and spawns a supervisor, and this path exists for "just serve
    # it" — the editing loop is still `uvicorn app:app --reload`.
    uvicorn.run(app, host=settings.host, port=settings.port)
