package lots

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/jackc/pgx/v5/pgconn"
)

// Search modes, returned so the UI can say which one it did.
const (
	ModeEmpty = "empty"
	ModeText  = "text"
	ModeRegex = "regex"
)

// searchSQL is completed with the predicate for whichever mode is in play.
//
// The snapshot overlay wins over the pipeline's figures: between a refresh and
// the next dbt build, web.lot_snapshot is the only place the current price
// exists, and it is precisely the lot the user just asked about.
const searchSQL = `
WITH ` + liveLotsCTE + `
SELECT
    ''::text AS slug, l.source, l.item_id, l.title, l.lot_url,
    coalesce(s.high_bid, l.high_bid)   AS high_bid,
    NULL::integer                      AS max_bid_cad,
    l.km,
    coalesce(s.close_at, l.close_at)   AS close_at,
    l.stale_close,
    coalesce(r.status, '')             AS review_status
FROM named l
LEFT JOIN web.lot_snapshot s ON s.source = l.source AND s.item_id = l.item_id
LEFT JOIN web.lot_review   r ON r.source = l.source AND r.item_id = l.item_id
WHERE %s
ORDER BY coalesce(l.min_bid, l.high_bid, 0), l.close_at
LIMIT $2`

// Search runs a free query over every live lot, in either of two modes.
//
// Plain text is a case-insensitive substring of title or description. A query
// starting with `/` is a POSIX regex, run the way the wishlist matcher runs
// one -- which makes this the place to try a rule out against the live corpus
// before committing it to the seed. Seven rounds of false positives came from
// rules that looked right in the abstract and were caught by exactly this.
//
// Returns the rows and the mode used. An invalid regex comes back as a mode of
// "error: ..." rather than an error, because iterating on a broken pattern is
// the point of regex mode and it has to fail readably.
func (s *Store) Search(ctx context.Context, query string, limit int) ([]Lot, string, error) {
	query = strings.TrimSpace(query)
	if query == "" {
		return nil, ModeEmpty, nil
	}

	var predicate, arg, mode string
	if strings.HasPrefix(query, "/") {
		pattern := strings.TrimSpace(strings.TrimPrefix(query, "/"))
		if pattern == "" {
			return nil, ModeEmpty, nil
		}
		predicate = `(l.title || ' ' || l.descr) ~* $1`
		arg, mode = pattern, ModeRegex
	} else {
		predicate = `(l.title || ' ' || l.descr) ILIKE $1`
		arg, mode = "%"+query+"%", ModeText
	}

	rows, err := s.pool.Query(ctx, fmt.Sprintf(searchSQL, predicate), arg, limit)
	if err != nil {
		if mode == ModeRegex {
			if msg, ok := regexComplaint(err); ok {
				return nil, "error: " + msg, nil
			}
		}
		return nil, mode, err
	}
	defer rows.Close()

	var out []Lot
	for rows.Next() {
		var l Lot
		if err := rows.Scan(&l.Slug, &l.Source, &l.ItemID, &l.Title, &l.LotURL,
			&l.HighBid, &l.MaxBid, &l.KM, &l.CloseAt, &l.StaleClose, &l.Verdict); err != nil {
			return nil, mode, err
		}
		out = append(out, l)
	}
	if err := rows.Err(); err != nil {
		if mode == ModeRegex {
			if msg, ok := regexComplaint(err); ok {
				return nil, "error: " + msg, nil
			}
		}
		return nil, mode, err
	}
	return out, mode, nil
}

// regexComplaint recognises Postgres objecting to the pattern rather than to
// anything this code did. 2201B is invalid_regular_expression; 2201C is an
// invalid escape inside one.
func regexComplaint(err error) (string, bool) {
	var pg *pgconn.PgError
	if !errors.As(err, &pg) {
		return "", false
	}
	switch pg.Code {
	case "2201B", "2201C":
		return pg.Message, true
	}
	return "", false
}
