package lots

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
)

// Detail is one lot, as the detail pane shows it: the pipeline's figures, what
// the last refresh saw, and the verdict.
type Detail struct {
	Source     string
	ItemID     string
	Title      string
	LotURL     string
	Descr      string
	EventName  string
	EventCity  string
	KM         *int
	ScrapedAt  time.Time
	CloseAt    *time.Time
	StaleClose bool

	HighBid  *float64
	MinBid   *float64
	BidCount *int64

	// What the pipeline last built, kept beside the overlay so the pane can
	// show that a refresh actually moved something rather than silently
	// replacing one number with another.
	PipelineHighBid *float64

	RefreshedAt  *time.Time
	RefreshError string
	LotStatus    string
	TimeLeft     string
	ImageURLs    []string

	Verdict string
	Notes   string
}

const detailSQL = `
WITH ` + liveLotsCTE + `
SELECT
    l.source, l.item_id, l.title, l.lot_url, l.descr,
    coalesce(l.event_name, ''), coalesce(l.event_city, ''), l.km,
    coalesce(s.close_at, l.close_at) AS close_at,
    l.stale_close,
    coalesce(s.high_bid,  l.high_bid)  AS high_bid,
    coalesce(s.min_bid,   l.min_bid)   AS min_bid,
    coalesce(s.bid_count, l.bid_count) AS bid_count,
    l.high_bid                         AS pipeline_high_bid,
    s.refreshed_at, coalesce(s.refresh_error, ''),
    coalesce(s.lot_status, ''), coalesce(s.time_left, ''),
    coalesce(s.image_urls, '{}'),
    coalesce(r.status, ''), coalesce(r.notes, '')
FROM named l
LEFT JOIN web.lot_snapshot s ON s.source = l.source AND s.item_id = l.item_id
LEFT JOIN web.lot_review   r ON r.source = l.source AND r.item_id = l.item_id
WHERE l.source = $1 AND l.item_id = $2`

// Lot returns one lot, or nil when it is no longer live.
//
// nil is not an error: a lot that has closed and aged past the grace window is
// gone from the queryable set, and the pane says so rather than 404ing -- the
// user clicked a row that was on screen a moment ago and deserves an
// explanation, not a dead end.
func (s *Store) Lot(ctx context.Context, source, itemID string) (*Detail, error) {
	var d Detail
	err := s.pool.QueryRow(ctx, detailSQL, source, itemID).Scan(
		&d.Source, &d.ItemID, &d.Title, &d.LotURL, &d.Descr,
		&d.EventName, &d.EventCity, &d.KM, &d.CloseAt, &d.StaleClose,
		&d.HighBid, &d.MinBid, &d.BidCount, &d.PipelineHighBid,
		&d.RefreshedAt, &d.RefreshError, &d.LotStatus, &d.TimeLeft, &d.ImageURLs,
		&d.Verdict, &d.Notes)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, nil
		}
		return nil, err
	}
	return &d, nil
}
