// Command server is the triage site: an htmx front end over the wishlist
// matches, talking to Go.
//
// Every route returns HTML. The refresh path, which needs the scraper's
// FlareSolverr handling and its bronze writer, is reached by calling out to
// Python rather than reimplemented here -- see web/README.md.
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"

	"github.com/greenmushrooms/bargin_sort/api/internal/config"
	"github.com/greenmushrooms/bargin_sort/api/internal/db"
	"github.com/greenmushrooms/bargin_sort/api/internal/lots"
	"github.com/greenmushrooms/bargin_sort/api/internal/refresh"
	"github.com/greenmushrooms/bargin_sort/api/internal/render"
	"github.com/greenmushrooms/bargin_sort/api/internal/selections"
)

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}

func run() error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	pool, err := db.New(ctx, cfg.DSN())
	if err != nil {
		return fmt.Errorf("database: %w", err)
	}
	defer pool.Close()

	if err := db.Migrate(ctx, pool, cfg.SQLDir); err != nil {
		return fmt.Errorf("migrate: %w", err)
	}

	rnd, err := render.New(cfg.TemplateDir)
	if err != nil {
		return fmt.Errorf("templates: %w", err)
	}

	store := lots.New(pool)
	sel := selections.New(pool)
	refresher := refresh.NewRunner(cfg.PythonBin, cfg.RefreshDir,
		time.Duration(cfg.RefreshTimeoutSeconds)*time.Second)

	r := chi.NewRouter()
	r.Use(middleware.Recoverer)

	r.Get("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		if err := pool.Ping(context.Background()); err != nil {
			http.Error(w, "db unreachable", http.StatusServiceUnavailable)
			return
		}
		fmt.Fprintln(w, "ok")
	})

	r.Get("/", func(w http.ResponseWriter, req *http.Request) {
		slug := req.URL.Query().Get("slug")
		sidebar, err := store.Sidebar(req.Context())
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		live, err := store.Live(req.Context(), slug, cfg.SearchLimit)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		rnd.HTML(w, http.StatusOK, "index", map[string]any{
			"Sidebar":  sidebar,
			"Lots":     live,
			"Selected": slug,
			"Verdicts": selections.Verdicts,
		})
	})

	// The list on its own, for htmx to swap when a wishlist row is clicked.
	r.Get("/lots", func(w http.ResponseWriter, req *http.Request) {
		live, err := store.Live(req.Context(), req.URL.Query().Get("slug"), cfg.SearchLimit)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		rnd.HTML(w, http.StatusOK, "lot_list", map[string]any{
			"Lots":     live,
			"Verdicts": selections.Verdicts,
		})
	})

	// A verdict. Returns the row's review bar so htmx swaps just that cell --
	// re-rendering the list would lose the scroll position on a phone, which
	// is where most of the triaging actually happens.
	r.Post("/lot/{source}/{itemID}/review", func(w http.ResponseWriter, req *http.Request) {
		source := chi.URLParam(req, "source")
		itemID := chi.URLParam(req, "itemID")
		verdict := req.FormValue("verdict")

		result, err := sel.Apply(req.Context(), source, itemID, verdict)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		rnd.HTML(w, http.StatusOK, "review_bar", map[string]any{
			"Source":   source,
			"ItemID":   itemID,
			"Verdict":  result,
			"Verdicts": selections.Verdicts,
		})
	})

	// Bulk verdict over ticked rows. Re-renders the whole list rather than
	// each row: a bulk change moves many rows at once and swapping them
	// individually would need one out-of-band target per lot.
	r.Post("/review/bulk", func(w http.ResponseWriter, req *http.Request) {
		if err := req.ParseForm(); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		var refs []selections.Ref
		for _, raw := range req.Form["lot"] {
			if ref, ok := selections.ParseRef(raw); ok {
				refs = append(refs, ref)
			}
		}
		// "clear" is spelled out in the form rather than sent as an empty
		// string, so a checkbox posting nothing cannot be read as "clear all".
		verdict := req.FormValue("verdict")
		if verdict == "clear" {
			verdict = ""
		}
		n, err := sel.ApplyMany(req.Context(), refs, verdict)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		log.Printf("bulk verdict %q applied to %d lot(s)", verdict, n)

		live, err := store.Live(req.Context(), req.FormValue("slug"), cfg.SearchLimit)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		rnd.HTML(w, http.StatusOK, "lot_list", map[string]any{
			"Lots":     live,
			"Verdicts": selections.Verdicts,
			"Applied":  n,
		})
	})

	// Free search over every live lot. Plain text is a substring; a query
	// starting with `/` is a POSIX regex, which makes this the place to try a
	// wishlist rule against the live corpus before committing it to the seed.
	r.Get("/search", func(w http.ResponseWriter, req *http.Request) {
		q := req.URL.Query().Get("q")
		found, mode, err := store.Search(req.Context(), q, cfg.SearchLimit)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		rnd.HTML(w, http.StatusOK, "search_results", map[string]any{
			"Lots":     found,
			"Mode":     mode,
			"Query":    q,
			"Limit":    cfg.SearchLimit,
			"Verdicts": selections.Verdicts,
		})
	})

	r.Get("/lot/{source}/{itemID}", func(w http.ResponseWriter, req *http.Request) {
		source := chi.URLParam(req, "source")
		itemID := chi.URLParam(req, "itemID")

		lot, err := store.Lot(req.Context(), source, itemID)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		if lot == nil {
			rnd.HTML(w, http.StatusOK, "lot_gone", map[string]any{
				"Source": source, "ItemID": itemID,
			})
			return
		}
		history, err := sel.History(req.Context(), source, itemID)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		rnd.HTML(w, http.StatusOK, "lot_detail", map[string]any{
			"Lot":      lot,
			"History":  history,
			"Verdicts": selections.Verdicts,
		})
	})

	// Refresh one lot: the live price and the full gallery, neither of which a
	// sweep has. Takes 5-10 seconds because it drives a real browser, so the
	// button is per-lot and never automatic.
	r.Post("/lot/{source}/{itemID}/refresh", func(w http.ResponseWriter, req *http.Request) {
		source := chi.URLParam(req, "source")
		itemID := chi.URLParam(req, "itemID")

		result, err := refresher.Refresh(req.Context(), source, itemID)
		if err != nil {
			log.Printf("refresh %s/%s: %v", source, itemID, err)
			rnd.HTML(w, http.StatusOK, "refresh_result", map[string]any{
				"Source": source, "ItemID": itemID,
				"Error": "The refresh could not run. Check the server log.",
			})
			return
		}
		if !result.OK {
			rnd.HTML(w, http.StatusOK, "refresh_result", map[string]any{
				"Source": source, "ItemID": itemID, "Error": result.Error,
			})
			return
		}

		// Read the snapshot back rather than rendering the subprocess's JSON:
		// refresh.py's upsert is what decides things like never replacing a
		// gallery with an empty one, and re-reading keeps one rendering path.
		snap, err := store.Snapshot(req.Context(), source, itemID)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		rnd.HTML(w, http.StatusOK, "refresh_result", map[string]any{
			"Source": source, "ItemID": itemID, "Snapshot": snap,
		})
	})

	// No-store on the assets. The CSS and script have been rewritten several
	// times in a sitting, and a phone that cached an earlier copy shows a page
	// that was genuinely broken hours ago -- which is indistinguishable, from
	// the other end of an SSH session, from a bug that is still there. This is
	// a single-user local tool, so revalidating every load costs nothing worth
	// counting.
	r.Handle("/static/*", http.StripPrefix("/static/",
		noStore(http.FileServer(http.Dir(cfg.StaticDir)))))

	addr := fmt.Sprintf("%s:%d", cfg.Host, cfg.Port)
	srv := &http.Server{
		Addr:              addr,
		Handler:           r,
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdownCtx)
	}()

	log.Printf("triage site on http://%s", addr)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}

// noStore stops a browser reusing a cached asset across a rebuild.
func noStore(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store, must-revalidate")
		h.ServeHTTP(w, r)
	})
}
