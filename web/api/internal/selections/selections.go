// Package selections owns everything a person does to a lot.
//
// Writes go to web.* and nowhere else. A verdict is an opinion, not an
// observation, so it never reaches raw.* -- that boundary is what keeps
// `dbt run --full-refresh` safe to type.
package selections

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Verdicts, in the order the review bar renders them.
//
// passed and false_positive are split because they say opposite things about
// the wishlist: one means the regex found the right product and I do not want
// it, the other means the regex is wrong. Collapsing them would throw away the
// only signal that drives regex tuning.
var Verdicts = []string{"starred", "bidding", "won", "lost", "passed", "false_positive"}

func Valid(v string) bool {
	for _, ok := range Verdicts {
		if v == ok {
			return true
		}
	}
	return false
}

type Store struct{ pool *pgxpool.Pool }

func New(pool *pgxpool.Pool) *Store { return &Store{pool: pool} }

// Event is one entry in a lot's history.
type Event struct {
	Verdict    string
	Notes      string
	OccurredAt string
}

// Apply records a verdict and returns the lot's resulting state, which is ""
// when the verdict was cleared.
//
// The UI toggles: clicking the verdict a lot already has removes it. That is
// handled here rather than in the handler so the read of the old value and the
// write of the new one happen in the same transaction -- two rapid clicks on a
// phone would otherwise race and could leave the log disagreeing with the fold.
func (s *Store) Apply(ctx context.Context, source, itemID, verdict string) (string, error) {
	if !Valid(verdict) {
		return "", fmt.Errorf("unknown verdict %q", verdict)
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return "", err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	var current string
	err = tx.QueryRow(ctx,
		`SELECT status FROM web.lot_review WHERE source = $1 AND item_id = $2 FOR UPDATE`,
		source, itemID).Scan(&current)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return "", err
	}

	clearing := current == verdict
	result := verdict

	if clearing {
		if _, err := tx.Exec(ctx,
			`DELETE FROM web.lot_review WHERE source = $1 AND item_id = $2`,
			source, itemID); err != nil {
			return "", err
		}
		result = ""
	} else {
		if _, err := tx.Exec(ctx, `
			INSERT INTO web.lot_review (source, item_id, status)
			VALUES ($1, $2, $3)
			ON CONFLICT (source, item_id) DO UPDATE
			SET status = EXCLUDED.status, updated_at = now()`,
			source, itemID, verdict); err != nil {
			return "", err
		}
	}

	// The log is the record and the review row is the fold of it, so the event
	// is written in the same transaction. A NULL verdict is a real event -- "I
	// un-starred this" is worth keeping, and a gap would read as never touched.
	var logged any
	if !clearing {
		logged = verdict
	}
	if _, err := tx.Exec(ctx,
		`INSERT INTO web.lot_event (source, item_id, verdict) VALUES ($1, $2, $3)`,
		source, itemID, logged); err != nil {
		return "", err
	}

	if err := tx.Commit(ctx); err != nil {
		return "", err
	}
	return result, nil
}

// History returns a lot's events, newest first.
func (s *Store) History(ctx context.Context, source, itemID string) ([]Event, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT coalesce(verdict, ''), coalesce(notes, ''),
		       to_char(occurred_at AT TIME ZONE 'America/Toronto', 'Mon DD HH24:MI')
		FROM web.lot_event
		WHERE source = $1 AND item_id = $2
		ORDER BY occurred_at DESC, event_id DESC`, source, itemID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []Event
	for rows.Next() {
		var e Event
		if err := rows.Scan(&e.Verdict, &e.Notes, &e.OccurredAt); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// Ref identifies one lot in a bulk request.
type Ref struct {
	Source string
	ItemID string
}

// ParseRef reads the "source:item_id" form a checkbox carries. Split on the
// first colon only: HiBid ids are numeric but Police Auctions ids are not
// guaranteed to be, and a colon inside one must not silently truncate it.
func ParseRef(s string) (Ref, bool) {
	i := strings.Index(s, ":")
	if i <= 0 || i == len(s)-1 {
		return Ref{}, false
	}
	return Ref{Source: s[:i], ItemID: s[i+1:]}, true
}

// ApplyMany sets one verdict across many lots, or clears them all when verdict
// is "".
//
// Deliberately NOT the toggle Apply uses. Toggling a selection means the
// result depends on what each lot already was, so the same click would star
// some and un-star others -- fine for one lot the user is looking at, useless
// for twenty they have ticked. Bulk sets, or bulk clears, and nothing else.
//
// One transaction for the whole batch: a bulk action that half-applied would
// leave the user with no way to tell which half.
func (s *Store) ApplyMany(ctx context.Context, refs []Ref, verdict string) (int, error) {
	if len(refs) == 0 {
		return 0, nil
	}
	clearing := verdict == ""
	if !clearing && !Valid(verdict) {
		return 0, fmt.Errorf("unknown verdict %q", verdict)
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return 0, err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	for _, ref := range refs {
		if clearing {
			if _, err := tx.Exec(ctx,
				`DELETE FROM web.lot_review WHERE source = $1 AND item_id = $2`,
				ref.Source, ref.ItemID); err != nil {
				return 0, err
			}
		} else {
			if _, err := tx.Exec(ctx, `
				INSERT INTO web.lot_review (source, item_id, status)
				VALUES ($1, $2, $3)
				ON CONFLICT (source, item_id) DO UPDATE
				SET status = EXCLUDED.status, updated_at = now()`,
				ref.Source, ref.ItemID, verdict); err != nil {
				return 0, err
			}
		}

		var logged any
		if !clearing {
			logged = verdict
		}
		if _, err := tx.Exec(ctx,
			`INSERT INTO web.lot_event (source, item_id, verdict, notes)
			 VALUES ($1, $2, $3, 'bulk')`,
			ref.Source, ref.ItemID, logged); err != nil {
			return 0, err
		}
	}

	if err := tx.Commit(ctx); err != nil {
		return 0, err
	}
	return len(refs), nil
}
