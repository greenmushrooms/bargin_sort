// Package lots reads over the pipeline and the match cache.
//
// Everything here is read-only against raw.*, silver.* and silver_enhanced.*.
// The one table this package writes is web.wishlist_match, which is a cache of
// its own reads and is disposable.
package lots

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/jackc/pgx/v5/pgxpool"
)

type Store struct{ pool *pgxpool.Pool }

func New(pool *pgxpool.Pool) *Store { return &Store{pool: pool} }

// WishlistRow is one standing question, with a live count beside it.
type WishlistRow struct {
	Slug     string
	Label    string
	Priority int
	MaxBid   *int
	Live     int
}

// Lot is a matched lot as the list renders it.
type Lot struct {
	Source   string
	ItemID   string
	Slug     string
	Title    string
	LotURL   string
	HighBid  *float64
	MaxBid   *int
	KM       *int
	CloseAt  *time.Time
	Verdict  string
	Distance string

	// StaleClose means the stored close time has passed but the lot still
	// reads OPEN -- it may have been extended. Only a refresh settles it.
	StaleClose bool
}

// Sidebar returns every wishlist row, including the ones matching nothing.
// A row with zero live matches is information -- the native-4K projector row
// has never matched anything in the whole corpus -- so it is not filtered out.
const sidebarSQL = `
SELECT w.slug, w.label, coalesce(w.priority, 9) AS priority, w.max_bid_cad,
       count(l.item_id) FILTER (WHERE l.event_ends_at > now()) AS live
FROM reference.wishlist w
LEFT JOIN web.wishlist_match m ON m.slug = w.slug
LEFT JOIN silver_enhanced.fct_lots l
       ON l.source = m.source AND l.item_id = m.item_id
GROUP BY w.slug, w.label, w.priority, w.max_bid_cad
ORDER BY coalesce(w.priority, 9), w.label`

func (s *Store) Sidebar(ctx context.Context) ([]WishlistRow, error) {
	rows, err := s.pool.Query(ctx, sidebarSQL)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []WishlistRow
	for rows.Next() {
		var r WishlistRow
		if err := rows.Scan(&r.Slug, &r.Label, &r.Priority, &r.MaxBid, &r.Live); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// Live lists matched lots that have not closed yet, soonest first.
//
// The close test is on fct_lots.event_ends_at, which is the bid close as
// reported by the dedicated auction scrape -- not eventDateEnd, which is the
// event's end DATE at midnight and reads up to 46 hours early.
const liveSQL = `
SELECT m.slug, l.source, l.item_id, l.title, l.lot_url,
       coalesce(s.high_bid, l.high_bid), w.max_bid_cad,
       round(l.distance_km)::int AS km,
       coalesce(s.close_at, l.event_ends_at) AS close_at,
       coalesce(s.close_at, l.event_ends_at) <= now() AS stale_close,
       coalesce(r.status, '')
FROM web.wishlist_match m
JOIN silver_enhanced.fct_lots l ON l.source = m.source AND l.item_id = m.item_id
JOIN reference.wishlist w ON w.slug = m.slug
LEFT JOIN web.lot_snapshot s ON s.source = m.source AND s.item_id = m.item_id
LEFT JOIN web.lot_review   r ON r.source = m.source AND r.item_id = m.item_id
-- Same two rules as liveLotsCTE: a refresh that saw CLOSED is final, and
-- otherwise a one-hour grace window, because a lot extended after the last
-- auction scrape still reads OPEN while its stored close has lapsed.
WHERE (s.lot_status IS NULL OR s.lot_status <> 'CLOSED')
  AND coalesce(s.close_at, l.event_ends_at) > now() - interval '1 hour'
  AND ($1 = '' OR m.slug = $1)
ORDER BY coalesce(s.close_at, l.event_ends_at), l.title
LIMIT $2`

func (s *Store) Live(ctx context.Context, slug string, limit int) ([]Lot, error) {
	rows, err := s.pool.Query(ctx, liveSQL, slug, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []Lot
	for rows.Next() {
		var l Lot
		if err := rows.Scan(&l.Slug, &l.Source, &l.ItemID, &l.Title, &l.LotURL,
			&l.HighBid, &l.MaxBid, &l.KM, &l.CloseAt, &l.StaleClose, &l.Verdict); err != nil {
			return nil, err
		}
		out = append(out, l)
	}
	return out, rows.Err()
}

// Snapshot is what the last refresh of a lot saw.
type Snapshot struct {
	RefreshedAt time.Time
	HighBid     *float64
	MinBid      *float64
	BidCount    *int64
	LotStatus   string
	TimeLeft    string
	CloseAt     *time.Time
	ImageURLs   []string
	Error       string
}

// Snapshot reads the most recent refresh of one lot. Absent means never
// refreshed, which the pane renders differently from refreshed-and-failed.
func (s *Store) Snapshot(ctx context.Context, source, itemID string) (*Snapshot, error) {
	var snap Snapshot
	err := s.pool.QueryRow(ctx, `
		SELECT refreshed_at, high_bid, min_bid, bid_count,
		       coalesce(lot_status, ''), coalesce(time_left, ''),
		       close_at, coalesce(image_urls, '{}'), coalesce(refresh_error, '')
		FROM web.lot_snapshot
		WHERE source = $1 AND item_id = $2`, source, itemID).
		Scan(&snap.RefreshedAt, &snap.HighBid, &snap.MinBid, &snap.BidCount,
			&snap.LotStatus, &snap.TimeLeft, &snap.CloseAt, &snap.ImageURLs, &snap.Error)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, nil
		}
		return nil, err
	}
	return &snap, nil
}
