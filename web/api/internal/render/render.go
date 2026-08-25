// Package render parses HTML templates from disk once at startup and writes
// fragments to http.ResponseWriter. Every handler returns HTML, never JSON --
// the browser is the only client and htmx swaps fragments directly.
package render

import (
	"bytes"
	"fmt"
	"html/template"
	"net/http"
	"path/filepath"
)

type Renderer struct {
	tpl *template.Template
}

// New parses every *.html under dir into one set. Template names are the
// file's basename without extension (lot_row.html -> "lot_row").
func New(dir string) (*Renderer, error) {
	pattern := filepath.Join(dir, "*.html")
	matches, err := filepath.Glob(pattern)
	if err != nil {
		return nil, fmt.Errorf("glob %s: %w", pattern, err)
	}
	if len(matches) == 0 {
		return nil, fmt.Errorf("no templates found in %s", dir)
	}
	tpl, err := template.New("").Funcs(funcs()).ParseFiles(matches...)
	if err != nil {
		return nil, fmt.Errorf("parse templates: %w", err)
	}
	return &Renderer{tpl: tpl}, nil
}

// HTML renders name to w. The template is executed into a buffer first so a
// template error becomes a clean 500 rather than a half-written fragment that
// htmx would happily swap into the page.
func (r *Renderer) HTML(w http.ResponseWriter, status int, name string, data any) {
	var buf bytes.Buffer
	if err := r.tpl.ExecuteTemplate(&buf, name+".html", data); err != nil {
		http.Error(w, "template error: "+err.Error(), http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(status)
	_, _ = buf.WriteTo(w)
}
