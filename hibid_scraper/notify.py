#!/usr/bin/env python3
"""
Wishlist alerts to Telegram — Prefect flow.

Answers "what turned up overnight that I said I wanted", once, per lot. The
matching rules are the `reference.wishlist` seed; this module only decides what
is *new*, formats it, and sends it.

Why a flow of its own rather than a tail on the scrape: the scrapes run at
05:30, 07:30 and 08:00 and each one only sees its own slice, so a notifier
bolted onto any of them would either fire three times a morning or miss
whatever the other two found. Run after all three and there is exactly one
message, assembled from the finished silver tables.

Deliberately once per lot, not once per run. A wishlist match stays matched for
the ~4 days its lot stays open, so re-announcing live matches daily would bury
the new ones. raw.wishlist_alerts records what has been said; the close time is
in the message so a single morning alert is enough to act on.

Configuration — the flow declines quietly if either is unset, so the deployment
is safe to create before the blocks exist:

    TELEGRAM_BOT_TOKEN   bargin-sort--telegram-bot-token
    TELEGRAM_CHAT_ID     bargin-sort--telegram-chat-id
"""

import html
import logging
import sys
from typing import Optional

import requests
from prefect import flow, task
from psycopg2.extras import RealDictCursor

from config import Config
from database import Database

# Telegram rejects anything over 4096 characters. Messages are assembled to sit
# under this instead, leaving room for the header that gets prepended to the
# first chunk and for multi-byte characters counted per code point.
MAX_MESSAGE_CHARS = 3500

TELEGRAM_TIMEOUT_SECONDS = 20

# Priority 1 rows are the ones worth interrupting someone for; the rest are
# still sent, just visually demoted.
PRIORITY_ICON = {1: "⭐", 2: "\U0001f539", 3: "▫️"}


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


logger = logging.getLogger(__name__)


# The live-lot and match logic is analyses/wishlist_check.sql, kept in step with
# it deliberately: that query is the thing a human runs to ask "is there one
# right now", and an alert that disagrees with it would be untrustworthy.
#
# The two additions here are the budget filter and the already-sent filter.
# Exclusions read the title plus the "Title:" line HiBid buries in the
# description rather than the whole description — the haystack matters, see the
# note in wishlist_check.sql.
#
# Raw string: the `[^\n]` in the buried-title pattern has to reach Postgres as
# a backslash and an n. Let Python collapse it to a newline and the class still
# happens to mean the same thing, which is exactly the kind of accident that
# survives review and breaks on the next edit.
WISHLIST_MATCH_SQL = r"""
WITH live AS (
  SELECT f.item_id, f.source, f.title, f.high_bid, f.min_bid, f.bid_count,
         f.lot_url, round(f.distance_km) AS km,
         coalesce(m.close_at, f.event_ends_at) AS close_at,
         coalesce((SELECT h.raw_json ->> 'description' FROM raw.hibid h
                    WHERE h.item_id = f.item_id
                    ORDER BY h.scraped_at DESC LIMIT 1), '') AS descr
  FROM silver_enhanced.fct_lots f
  LEFT JOIN silver_enhanced.auction_manifest m ON m.auction_id = f.auction_id
  WHERE f.recency_rank = 1
    AND coalesce(m.close_at, f.event_ends_at) > now()
    AND f.lot_status = 'OPEN'
), named AS (
  SELECT l.*,
         l.title || ' ' ||
           coalesce(substring(l.descr from 'Title: ([^\n]*)'), '') AS full_title
  FROM live l
)
SELECT DISTINCT ON (w.slug, l.item_id)
       w.slug, w.label, w.priority, w.max_bid_cad,
       l.item_id, l.source, l.title, l.high_bid, l.min_bid, l.bid_count, l.km,
       l.lot_url,
       to_char(l.close_at AT TIME ZONE 'America/Toronto', 'Dy HH24:MI') AS closes
FROM named l
JOIN reference.wishlist w
  ON  CASE w.match_scope WHEN 'title' THEN l.title
                         ELSE l.title || ' ' || l.descr END ~* w.match_regex
  AND l.full_title !~* w.exclude_regex
-- min_bid is what it would cost to enter, so it is the number to compare with
-- the cap. A lot already bid past the cap is not an opportunity, and alerting
-- on it would train the reader to skim.
WHERE coalesce(l.min_bid, l.high_bid, 0) <= w.max_bid_cad
  AND NOT EXISTS (
        SELECT 1 FROM raw.wishlist_alerts a
        WHERE a.item_id = l.item_id AND a.slug = w.slug
      )
ORDER BY w.slug, l.item_id, l.close_at
"""


@task(name="fetch_wishlist_matches")
def fetch_wishlist_matches(config: Config) -> list[dict]:
    """Live wishlist matches within budget that have not been announced yet."""
    db = Database(config)
    try:
        db.connect()
        with db.conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(WISHLIST_MATCH_SQL)
            matches = [dict(row) for row in cursor.fetchall()]
    finally:
        db.close()

    # DISTINCT ON collapses per (slug, lot); this orders the survivors the way
    # the message reads — most wanted first, cheapest entry first within a row.
    matches.sort(key=lambda m: (m["priority"], m["label"], m["min_bid"] or 0))
    logger.info(f"{len(matches)} new wishlist matches within budget")
    return matches


def _money(value) -> str:
    """Format a bid the way the site does, dropping a pointless .00."""
    if value is None:
        return "-"
    number = float(value)
    return f"${number:,.0f}" if number == int(number) else f"${number:,.2f}"


def _lot_block(match: dict) -> str:
    """One lot, as two lines: a linked title and its numbers."""
    title = html.escape(str(match["title"] or "").strip())
    url = html.escape(str(match["lot_url"] or ""), quote=True)

    facts = [f"{_money(match['high_bid'])} now"]
    if match["min_bid"] is not None:
        facts.append(f"{_money(match['min_bid'])} to enter")
    # Omitted rather than shown as zero when unknown: Police Auctions does not
    # publish a bid count, and "0 bids" there would read as "nobody wants it".
    if match["bid_count"] is not None:
        facts.append(f"{match['bid_count']} bids")
    if match["km"] is not None:
        facts.append(f"{int(match['km'])} km")
    if match["closes"]:
        facts.append(f"closes {match['closes']}")

    return f'• <a href="{url}">{title}</a>\n  <i>{" · ".join(facts)}</i>'


def format_alert_messages(matches: list[dict]) -> list[tuple[str, list[dict]]]:
    """
    Render matches as (message_text, lots_in_that_message) pairs.

    Paired rather than returned as bare strings so the caller can record
    exactly the lots a successful send covered. Splitting one big alert into
    chunks means a partial failure is possible, and the lots in an unsent chunk
    have to stay unrecorded so tomorrow's run retries them.
    """
    if not matches:
        return []

    messages: list[tuple[str, list[dict]]] = []
    blocks: list[str] = []
    covered: list[dict] = []
    current_label: Optional[str] = None
    length = 0

    def flush() -> None:
        nonlocal blocks, covered, length, current_label
        if blocks:
            messages.append(("\n".join(blocks), covered))
        blocks, covered, length = [], [], 0
        current_label = None

    for match in matches:
        parts = []
        if match["label"] != current_label:
            icon = PRIORITY_ICON.get(match["priority"], "")
            cap = _money(match["max_bid_cad"])
            heading = html.escape(str(match["label"]))
            parts.append(f"\n{icon} <b>{heading}</b>  <i>(cap {cap})</i>")

        parts.append(_lot_block(match))
        addition = "\n".join(parts)

        # Start a new message rather than overrun; repeat the heading there so a
        # chunk boundary never orphans a lot under no heading at all.
        if length and length + len(addition) > MAX_MESSAGE_CHARS:
            # "cont." only when the break genuinely splits one wishlist row. A
            # boundary that happens to fall where a new row starts is not a
            # continuation of anything, and saying so would misdescribe it.
            continued = match["label"] == current_label
            flush()
            icon = PRIORITY_ICON.get(match["priority"], "")
            cap = _money(match["max_bid_cad"])
            heading = html.escape(str(match["label"]))
            note = "cont. cap" if continued else "cap"
            addition = (
                f"\n{icon} <b>{heading}</b>  <i>({note} {cap})</i>\n"
                + _lot_block(match)
            )

        blocks.append(addition)
        covered.append(match)
        length += len(addition)
        current_label = match["label"]

    flush()

    lot_count = len(matches)
    total = len(messages)
    header = (
        f"\U0001f3f7️ <b>Wishlist matches</b> — {lot_count} new "
        f"lot{'s' if lot_count != 1 else ''}"
    )

    # Number every part, not just the first. A reader who gets 3 of 4 because
    # the last send failed can see that they did, which is the whole point of
    # counting them.
    for index, (text, lots) in enumerate(messages):
        prefix = header if index == 0 else "\U0001f3f7️ <b>Wishlist matches</b>"
        if total > 1:
            prefix += f"  <i>({index + 1}/{total})</i>"
        messages[index] = (f"{prefix}\n{text}", lots)
    return messages


def send_telegram_message(text: str, bot_token: str, chat_id: str) -> bool:
    """Send one message. Returns whether Telegram accepted it."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        response = requests.post(
            url,
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                # Auction pages render as a large unhelpful card otherwise, and
                # a batch of ten lots would be mostly preview.
                "disable_web_page_preview": True,
            },
            timeout=TELEGRAM_TIMEOUT_SECONDS,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as e:
        logger.error(f"Telegram send failed: {e}")
        return False

    if not payload.get("ok"):
        logger.error(
            f"Telegram rejected message: {payload.get('description')} "
            f"— preview: {text[:120]}"
        )
        return False
    return True


@task(name="send_wishlist_alerts")
def send_wishlist_alerts(config: Config, matches: list[dict], dry_run: bool) -> dict:
    """
    Send the alerts and record what actually went out.

    Recording follows the send, per chunk, so a Bot API failure costs a repeat
    tomorrow rather than silence forever.
    """
    messages = format_alert_messages(matches)
    if not messages:
        logger.info("No new wishlist matches — nothing to send")
        return {"matches": 0, "messages": 0, "sent": 0, "recorded": 0, "dry_run": dry_run}

    if dry_run:
        for text, lots in messages:
            logger.info(f"Dry run — would send {len(lots)} lots:\n{text}")
        return {
            "matches": len(matches),
            "messages": len(messages),
            "sent": 0,
            "recorded": 0,
            "dry_run": True,
        }

    sent = 0
    recorded = 0
    db = Database(config)
    try:
        db.connect()
        for text, lots in messages:
            if not send_telegram_message(
                text, config.telegram_bot_token, config.telegram_chat_id
            ):
                # Leave this chunk and every later one unrecorded so the next
                # run picks them up. Carrying on would keep hammering an API
                # that has already said no.
                logger.error(
                    f"Stopping after {sent} of {len(messages)} messages; "
                    f"{len(lots)} lots left unrecorded for the next run"
                )
                break
            sent += 1

            with db.conn.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO raw.wishlist_alerts (item_id, slug, high_bid)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (item_id, slug) DO NOTHING
                    """,
                    [(lot["item_id"], lot["slug"], lot["high_bid"]) for lot in lots],
                )
            db.conn.commit()
            recorded += len(lots)
    finally:
        db.close()

    result = {
        "matches": len(matches),
        "messages": len(messages),
        "sent": sent,
        "recorded": recorded,
        "dry_run": False,
    }
    logger.info(f"Wishlist alerts: {result}")

    # A partial send is a real failure — silver is fine but the reader did not
    # get told, and a green run would hide that.
    if sent < len(messages):
        raise RuntimeError(
            f"Only {sent} of {len(messages)} wishlist alert messages were sent"
        )
    return result


@flow(name="notify_wishlist")
def notify_wishlist(dry_run: bool = False) -> dict:
    """
    Send new wishlist matches to Telegram.

    `dry_run` formats and logs the messages without sending or recording them,
    which is how a new wishlist row should be checked before it is trusted with
    a schedule.
    """
    setup_logging()

    config = Config.from_env()

    # Not an error: the notifier is optional infrastructure and the rest of the
    # project runs without it. Failing here would turn "the blocks are not
    # created yet" into a red deployment every morning.
    if not config.telegram_bot_token or not config.telegram_chat_id:
        logger.warning(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID unset — skipping wishlist alerts"
        )
        return {"skipped": "telegram not configured"}

    matches = fetch_wishlist_matches(config)
    return send_wishlist_alerts(config, matches, dry_run=dry_run)


if __name__ == "__main__":
    notify_wishlist(dry_run="--dry-run" in sys.argv)
