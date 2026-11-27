// Package timing reports the stages of the tailoring run in progress, so the
// page can show live progress. Only stage names and durations are kept.
package timing

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"slices"
	"sync"
	"time"
)

// Stage is one finished step of a run.
type Stage struct {
	ID      string  `json:"id"`
	Label   string  `json:"label"`
	Seconds float64 `json:"seconds"`
	Status  string  `json:"status"` // "completed" or "fallback"
}

// Snapshot is the progress the page polls.
type Snapshot struct {
	ID             string  `json:"id,omitempty"`
	Running        bool    `json:"running"`
	Stage          string  `json:"stage,omitempty"`
	Stages         []Stage `json:"stages"`
	ElapsedSeconds float64 `json:"elapsed_seconds"`
}

// Tailoring tracks the active tailoring run.
type Tailoring struct {
	mu      sync.Mutex
	started time.Time
	data    Snapshot
}

// New returns a tracker with no run yet.
func New() *Tailoring {
	return &Tailoring{data: Snapshot{Stages: []Stage{}}}
}

type key struct{}

// Run starts a run and returns a context its stages are recorded through,
// plus a function to call when the run ends.
func (t *Tailoring) Run(ctx context.Context) (context.Context, func()) {
	id := make([]byte, 12)
	_, _ = rand.Read(id)
	t.mu.Lock()
	t.started = time.Now()
	t.data = Snapshot{ID: hex.EncodeToString(id), Running: true, Stage: "Starting", Stages: []Stage{}}
	t.mu.Unlock()
	return context.WithValue(ctx, key{}, t), func() {
		t.mu.Lock()
		t.data.Running = false
		t.data.ElapsedSeconds = time.Since(t.started).Seconds()
		t.mu.Unlock()
	}
}

// Snapshot returns a copy of the current progress.
func (t *Tailoring) Snapshot() Snapshot {
	t.mu.Lock()
	defer t.mu.Unlock()
	out := t.data
	out.Stages = slices.Clone(t.data.Stages)
	if t.data.Running {
		out.ElapsedSeconds = time.Since(t.started).Seconds()
	}
	return out
}

// Begin marks the start of a stage in ctx's run. Call the returned function
// with whether the stage succeeded; a failed stage is shown as a fallback.
func Begin(ctx context.Context, id, label string) func(ok bool) {
	t, _ := ctx.Value(key{}).(*Tailoring)
	if t == nil {
		return func(bool) {}
	}
	start := time.Now()
	t.mu.Lock()
	t.data.Stage = label
	t.mu.Unlock()
	return func(ok bool) {
		status := "fallback"
		if ok {
			status = "completed"
		}
		t.mu.Lock()
		t.data.Stages = append(t.data.Stages, Stage{id, label, time.Since(start).Seconds(), status})
		t.mu.Unlock()
	}
}
