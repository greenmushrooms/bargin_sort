# web — the triage site

A local, single-user site for the question the pipeline cannot answer on its
own: *of the lots that match my wishlist right now, which ones do I actually
want, and what is this one going for at this second?*

The scrape flow and the Telegram notifier both push — three sweeps a morning
and one message. This pulls: it is the thing you open when you are deciding
whether to bid.

```
[scrape flow] --writes--> raw.*  --dbt--> silver.* --> silver_enhanced.*
                                                             |
                                                        reads |
                                                             v
                     [htmx UI] <---> [FastAPI] ---writes---> web.*
                                          |
                                          +--fetches--> hibid.com / policeauctionscanada.com
                                          |             (the Refresh button)
                                          +--appends---> raw.hibid / raw.police_auctions
```

Boundaries, in the same spirit as `job_searcher_web`:

- `raw.*`, `silver.*`, `silver_enhanced.*` and `reference.*` are **read-only**
  from this project.
- Everything a person does here is written to **`web.*`** and nowhere else.
- The one exception is documented and deliberate: the Refresh button appends an
  observation to the landing table through the scraper's own `Database` class.
  A fetch of a lot is a fetch of a lot regardless of what triggered it, and
  bronze is append-only.
- The pipeline does not import or call this project.

## What it does

**Wishlist matches.** The left column is `reference.wishlist` — nineteen
standing questions — with a live count against each. Selecting one filters the
list. Rows with zero matches still show: the native-4K projector row has never
matched anything in the whole corpus, and that is information, not a bug.

**Selection state.** Six verdicts, and "no verdict" is the seventh and most
important — a lot with no row in `web.lot_review` is unread, which is what
makes the list a queue. Clicking the verdict a lot already has clears it.

| verdict | means |
| --- | --- |
| Star | wanted; the shortlist |
| Bidding | a bid is in |
| Won / Lost | how it ended |
| Pass | matched correctly, not wanted |
| Not this | **the wishlist regex is wrong** — this is not the product |

`Pass` and `Not this` are split because they say opposite things about the
wishlist. Seven rounds of regex tuning have come out of false positives — IPX7
read as a Sony PX7, "Unified Minds" as UniFi, the holy grail of skincare as a
Canyon Grail — and every one was found by hand. Marking them here makes the
next round a query:

```sql
SELECT r.notes, m.title, m.slug
FROM web.lot_review r
JOIN web.wishlist_match m USING (source, item_id)
WHERE r.status = 'false_positive';
```

**Refresh.** Per-lot, on demand. Fetches the lot's own page and returns two
things a sweep does not have: the price as of now, and the rest of the
photographs — HiBid's catalogue pages carry a partial gallery and Police
Auctions' browse cards carry exactly one thumbnail, while both detail pages
carry everything. Takes 5–10 seconds because it drives a real browser through
FlareSolverr. The result is written to `web.lot_snapshot` (what the UI reads,
immediately) and appended to bronze (where it belongs).

**Ad-hoc search.** The box above the list searches title + description across
every live lot, not just the matched ones. A query beginning with `/` is run as
a POSIX regex the same way the wishlist matcher runs one — which makes it the
place to try a rule out against the live corpus before committing it to the
seed. Try `/ubiquiti|unifi` and watch a box of Pokémon "Unified Minds" come
back.

## Running it

```bash
cd web
cp .env.example .env      # then fill DB_PASSWORD
uv sync
uv run uvicorn app:app --reload --port 7780
```

Then <http://localhost:7780>.

`DB_PASSWORD` is the app role's, held in the Prefect secret block
`bargin-sort--database-password`:

```bash
prefect block inspect secret/bargin-sort--database-password
```

The schema in `sql/` is applied on every boot — the files are idempotent, and
remembering to run a migration step after a `git pull` is the thing that makes
a local tool annoying enough to stop using.

Refresh needs FlareSolverr reachable at `FLARESOLVERR_URL` (published on the
host as `:8191`). Without it, HiBid refreshes will be 403'd by Cloudflare;
everything else on the site works.

## Layout

```
web/
├── app.py             FastAPI routes — every one returns HTML, never JSON
├── lots.py            reads over the pipeline + the match cache
├── refresh.py         the refresh button: fetch one lot, bank it
├── scraper_bridge.py  the single, documented import of hibid_scraper/
├── db.py              pooled psycopg2 + boot migrations
├── settings.py        config (the scraper's own DB_* names)
├── sql/               web schema
├── templates/         Jinja2 fragments
└── static/            css, ~50 lines of js, vendored htmx
```

## Why the match set is cached

The matcher costs ~4.5 seconds: nineteen wishlist rows crossed with ~18,000
live lots is 340,000 evaluations of regexes built to be picky, over title plus
description. That is fine three times a day and absurd per page load, so it is
materialised into `web.wishlist_match`.

Caching is safe because the inputs move on a schedule: `fct_lots` is a dbt
table and `reference.wishlist` is a dbt seed. `web.match_build` records the
newest `scraped_at` and a digest of the seed, so "is this stale" is a
millisecond comparison of inputs rather than a guess about elapsed time. It
rebuilds itself when either moves; **Rescan** forces it, which is what you want
after editing `seeds/wishlist.csv` and running `dbt seed`.

This is not the same as promoting the matcher to a dbt model, which
`analyses/wishlist_check.sql` argues against and is still right about. The
cache lives in `web`, is disposable, and can be dropped without touching the
pipeline.

## A timezone question, settled

Police Auctions close times *look* four hours out: `stg_police_lots` parses
`ends_at` with `to_timestamp(..., 'MM/DD/YYYY HH24:MI:SS')` under a session
whose `TimeZone` is `Etc/UTC`, against a Toronto site. It is correct — the
site's `data-action-time` is genuinely UTC.

Checked against bronze rather than by eye. `/Browse` lists only open lots, so
every observation of an open lot must have its true close in the future.
Reading `ends_at` as UTC, hours-to-close over 3,559 observations has a hard
floor at zero: one observation at −0.07h, then 36 in the 0–1h band. Were the
string Toronto local, lots would stay listed through their final four hours and
the −4h…0h buckets would be full. They are empty.

What misleads the eye is page 1 sorted by ending-soonest — staggered closes at
`20:20 / 20:23 / 20:25`, which reads as an 8:20 PM local pattern. It is 20:20
UTC: a 4:20 PM Toronto close, three minutes apart. `web/refresh.py` uses the
identical expression, so the site and `fct_lots` cannot drift.
