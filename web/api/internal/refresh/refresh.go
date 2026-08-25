// Package refresh runs one lot refresh by calling out to Python.
//
// The fetch itself stays in refresh.py because it needs the scraper's
// FlareSolverr handling, its Apollo reader and its bronze writer -- see
// web/refresh_cli.py for why none of that is worth a second implementation.
// This package owns everything around the call: how many may run at once, how
// long one may take, and what a failure looks like to the browser.
package refresh

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// Result is one refresh attempt. Error set means nothing else is.
type Result struct {
	Source string `json:"source"`
	ItemID string `json:"item_id"`
	OK     bool   `json:"ok"`
	Error  string `json:"error"`
}

type Runner struct {
	python  string
	dir     string
	timeout time.Duration

	// Two at a time, and this is load-bearing rather than tidiness.
	//
	// refresh.py carried a threading.Semaphore(2) for this, but every call is
	// its own process now, so that limit no longer binds anything and the
	// ceiling has to live here instead. Each refresh drives a real Chrome
	// through FlareSolverr; forty-two orphaned browsers is why the scraper
	// closes its sessions so carefully, and a page of refresh buttons with an
	// impatient user behind it is exactly how that happens again.
	slots chan struct{}
}

func NewRunner(python, dir string, timeout time.Duration) *Runner {
	// Resolved now, against this process's working directory. The child runs
	// with cmd.Dir set to web/, and a relative interpreter path would be
	// resolved against that instead -- silently looking one directory too far
	// up and failing with a bare "no such file or directory".
	if abs, err := filepath.Abs(python); err == nil {
		python = abs
	}
	return &Runner{
		python:  python,
		dir:     dir,
		timeout: timeout,
		slots:   make(chan struct{}, 2),
	}
}

// Refresh fetches one lot. An ordinary failure -- a withdrawn lot, a
// Cloudflare block -- comes back as a Result with Error set, not as an error:
// those are things to tell the user in the pane they are looking at. A
// returned error means the subprocess itself broke.
func (r *Runner) Refresh(ctx context.Context, source, itemID string) (Result, error) {
	// Wait for a slot, but give up if the browser goes away first.
	select {
	case r.slots <- struct{}{}:
		defer func() { <-r.slots }()
	case <-ctx.Done():
		return Result{}, ctx.Err()
	}

	// The timeout starts after the slot is acquired, not before: queueing
	// behind another refresh is not this refresh being slow, and charging it
	// for the wait would fail the second of two simultaneous clicks.
	runCtx, cancel := context.WithTimeout(ctx, r.timeout)
	defer cancel()

	cmd := exec.CommandContext(runCtx, r.python, "refresh_cli.py",
		"--source", source, "--item-id", itemID)
	cmd.Dir = r.dir

	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	err := cmd.Run()
	if runCtx.Err() == context.DeadlineExceeded {
		return Result{Source: source, ItemID: itemID,
			Error: fmt.Sprintf("Refresh gave up after %s.", r.timeout)}, nil
	}
	if err != nil {
		return Result{}, fmt.Errorf("refresh_cli: %w: %s", err, tail(stderr.String()))
	}

	var out Result
	if err := json.Unmarshal(bytes.TrimSpace(stdout.Bytes()), &out); err != nil {
		// stdout is meant to be JSON and nothing else, so a parse failure means
		// something wrote to it that should not have. stderr is where the
		// explanation will be.
		return Result{}, fmt.Errorf("refresh_cli returned unparseable output: %w: %s",
			err, tail(stderr.String()))
	}
	return out, nil
}

// tail keeps the last few lines of a traceback, which is the half that says
// what broke.
func tail(s string) string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	if len(lines) > 4 {
		lines = lines[len(lines)-4:]
	}
	return strings.Join(lines, " | ")
}
