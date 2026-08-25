package db

import (
	"context"
	"fmt"
	"os"
	"path/filepath"

	"github.com/jackc/pgx/v5/pgxpool"
)

// migrations are applied at startup, in order. Every file is written to be
// re-runnable, so there is no version table and no migration step to remember
// after a git pull -- forgetting one is what makes a local tool annoying
// enough to stop using.
//
// The list is explicit rather than a glob, and matches web/db.py's tuple while
// both servers exist: during the port either one may boot first, and a glob
// would silently apply a half-finished file that happened to be on disk.
var migrations = []string{
	"001_web_schema.sql",
	"002_wishlist_match.sql",
	"003_brand_tier.sql",
	"004_lot_event.sql",
}

// Migrate applies the web schema from dir.
func Migrate(ctx context.Context, pool *pgxpool.Pool, dir string) error {
	for _, name := range migrations {
		path := filepath.Join(dir, name)
		body, err := os.ReadFile(path)
		if err != nil {
			return fmt.Errorf("read %s: %w", path, err)
		}
		if _, err := pool.Exec(ctx, string(body)); err != nil {
			return fmt.Errorf("apply %s: %w", name, err)
		}
	}
	return nil
}
