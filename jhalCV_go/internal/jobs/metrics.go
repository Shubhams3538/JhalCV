package jobs

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"sync"
	"time"

	"jhalcv/internal/storage"
	"jhalcv/internal/usererr"
)

// Labels name each timed stage of a refresh. Inclusive spans overlap, so
// they must never be added together.
var Labels = map[string]string{
	"source_failure": "Company collection failures",
	"http":           "HTTP requests", "browser": "Browser rendering", "browser_queue": "Wait for browser slot",
	"ai": "AI calls", "ai_queue": "Wait for AI slot", "ai_cache_hit": "AI cache hits",
	"database": "Database (including lock waits)", "listing": "Listing collection (inclusive)",
	"discovery": "Portal discovery (inclusive)", "details": "Posting details (inclusive)",
	"detail_queue": "Wait for detail worker", "source_queue": "Wait for company worker",
	"source": "Company collection (inclusive)", "eligibility": "Eligibility batches (inclusive)",
	"browser_startup": "Chrome process startup", "browser_navigation": "Browser page loading and scripts",
	"browser_dom": "Browser HTML extraction",
}

// StageStats are the timings of one stage.
type StageStats struct {
	Stage      string  `json:"stage"`
	Label      string  `json:"label"`
	Calls      int     `json:"calls"`
	Seconds    float64 `json:"seconds"`
	MaxSeconds float64 `json:"max_seconds"`
	Failures   int     `json:"failures"`
}

type stageSet struct {
	order []string
	stats map[string]*StageStats
}

func (s *stageSet) add(stage string, seconds float64, failed bool) {
	if s.stats == nil {
		s.stats = map[string]*StageStats{}
	}
	st := s.stats[stage]
	if st == nil {
		st = &StageStats{Stage: stage, Label: Labels[stage]}
		s.stats[stage] = st
		s.order = append(s.order, stage)
	}
	st.Calls++
	st.Seconds += max(0, seconds)
	st.MaxSeconds = max(st.MaxSeconds, seconds)
	if failed {
		st.Failures++
	}
}

func (s *stageSet) list() []StageStats {
	out := []StageStats{}
	for _, k := range s.order {
		out = append(out, *s.stats[k])
	}
	return out
}

// Run collects the timings of one refresh.
type Run struct {
	ID        string
	Mode      string
	StartedAt string
	start     time.Time
	end       time.Time
	mu        sync.Mutex
	status    string
	stages    stageSet
	companies []string
	byCompany map[string]*companyStats
}

type companyStats struct {
	id     int64
	name   string
	stages stageSet
}

func (r *Run) add(stage string, seconds float64, failed bool, company *companyKey) {
	if _, ok := Labels[stage]; !ok {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.stages.add(stage, seconds, failed)
	if company != nil {
		key := jsonInt(company.id)
		c := r.byCompany[key]
		if c == nil {
			c = &companyStats{id: company.id, name: company.name}
			r.byCompany[key] = c
			r.companies = append(r.companies, key)
		}
		c.stages.add(stage, seconds, failed)
	}
}

func jsonInt(n int64) string {
	b, _ := json.Marshal(n)
	return string(b)
}

// RunReport is a saved or live timing report.
type RunReport struct {
	ID             string          `json:"id"`
	Mode           string          `json:"mode"`
	StartedAt      string          `json:"started_at"`
	Status         string          `json:"status"`
	ElapsedSeconds float64         `json:"elapsed_seconds"`
	Stages         []StageStats    `json:"stages"`
	Companies      []CompanyReport `json:"companies"`
}

// CompanyReport is one company's timings.
type CompanyReport struct {
	ID     int64        `json:"id"`
	Name   string       `json:"name"`
	Stages []StageStats `json:"stages"`
}

func (r *Run) snapshot() RunReport {
	r.mu.Lock()
	defer r.mu.Unlock()
	end := r.end
	if end.IsZero() {
		end = time.Now()
	}
	out := RunReport{ID: r.ID, Mode: r.Mode, StartedAt: r.StartedAt, Status: r.status,
		ElapsedSeconds: end.Sub(r.start).Seconds(), Stages: r.stages.list(), Companies: []CompanyReport{}}
	for _, key := range r.companies {
		c := r.byCompany[key]
		out.Companies = append(out.Companies, CompanyReport{c.id, c.name, c.stages.list()})
	}
	return out
}

type runKey struct{}
type companyKey struct {
	id   int64
	name string
}

// Measure times one stage of the refresh running in ctx. Call the returned
// function when the stage ends, with whether it failed.
func Measure(ctx context.Context, stage string) func(failed bool) {
	run, _ := ctx.Value(runKey{}).(*Run)
	if run == nil {
		return func(bool) {}
	}
	company, _ := ctx.Value(companyKey{}).(*companyKey)
	start := time.Now()
	return func(failed bool) { run.add(stage, time.Since(start).Seconds(), failed, company) }
}

// Timed runs fn as one measured stage.
func Timed[T any](ctx context.Context, stage string, fn func() (T, error)) (T, error) {
	end := Measure(ctx, stage)
	v, err := fn()
	end(err != nil)
	return v, err
}

// Mark records an event, or a wait that has already happened.
func Mark(ctx context.Context, stage string, seconds float64) {
	run, _ := ctx.Value(runKey{}).(*Run)
	if run != nil {
		company, _ := ctx.Value(companyKey{}).(*companyKey)
		run.add(stage, seconds, false, company)
	}
}

// WithCompany attributes timings in ctx to one company.
func WithCompany(ctx context.Context, id int64, name string) context.Context {
	return context.WithValue(ctx, companyKey{}, &companyKey{id, name})
}

// Benchmarks keeps timing reports in .local/job-benchmarks.
type Benchmarks struct {
	folder  string
	mu      sync.Mutex
	active  *Run
	warning bool
}

// NewBenchmarks stores reports under root.
func NewBenchmarks(root string) *Benchmarks {
	return &Benchmarks{folder: filepath.Join(root, ".local", "job-benchmarks")}
}

// Start begins a timed run. Call finish with the run's error when it ends;
// the report is saved then.
func (b *Benchmarks) Start(ctx context.Context, mode string) (context.Context, func(error)) {
	id := make([]byte, 16)
	_, _ = rand.Read(id)
	run := &Run{ID: hex.EncodeToString(id), Mode: mode, start: time.Now(), status: "running",
		StartedAt: time.Now().UTC().Format("2006-01-02T15:04:05.000000-07:00"), byCompany: map[string]*companyStats{}}
	b.mu.Lock()
	b.active = run
	b.mu.Unlock()
	return context.WithValue(ctx, runKey{}, run), func(err error) {
		run.mu.Lock()
		run.status = "complete"
		if err != nil {
			run.status = "failed"
		}
		run.end = time.Now()
		run.mu.Unlock()
		if os.MkdirAll(b.folder, 0o700) != nil || storage.WriteJSON(filepath.Join(b.folder, run.ID+".json"), run.snapshot()) != nil {
			b.mu.Lock()
			b.warning = true // Never fail paid work because timing could not be saved.
			b.mu.Unlock()
		}
	}
}

var runID = regexp.MustCompile(`^[a-f0-9]{32}$`)

// BenchmarkReport is the Test lab's timing view.
type BenchmarkReport struct {
	Report             *RunReport       `json:"report"`
	History            []map[string]any `json:"history"`
	PersistenceWarning bool             `json:"persistence_warning"`
}

// Report returns run id (or the latest run) and the ten newest runs.
func (b *Benchmarks) Report(id string) (*BenchmarkReport, error) {
	if id != "" && !runID.MatchString(id) {
		return nil, usererr.New("Invalid benchmark run.")
	}
	b.mu.Lock()
	active, warning := b.active, b.warning
	b.mu.Unlock()
	var report *RunReport
	if active != nil && (id == "" || id == active.ID) {
		r := active.snapshot()
		report = &r
	}
	entries, err := os.ReadDir(b.folder)
	if err != nil && !errors.Is(err, fs.ErrNotExist) {
		return nil, err
	}
	type file struct {
		path string
		mod  time.Time
	}
	var files []file
	for _, e := range entries {
		if filepath.Ext(e.Name()) != ".json" {
			continue
		}
		if info, err := e.Info(); err == nil {
			files = append(files, file{filepath.Join(b.folder, e.Name()), info.ModTime()})
		}
	}
	sort.SliceStable(files, func(i, j int) bool { return files[i].mod.After(files[j].mod) })
	history := []map[string]any{}
	for _, f := range files {
		raw, err := os.ReadFile(f.path)
		if err != nil {
			continue
		}
		var data RunReport
		if json.Unmarshal(raw, &data) != nil {
			continue
		}
		if id == "" {
			if report == nil || (report.Status != "running" && data.StartedAt > report.StartedAt) {
				d := data
				report = &d
			}
		} else if report == nil && id == data.ID {
			d := data
			report = &d
		}
		if len(history) < 10 {
			history = append(history, map[string]any{"id": data.ID, "mode": data.Mode, "started_at": data.StartedAt,
				"status": data.Status, "elapsed_seconds": data.ElapsedSeconds})
		}
	}
	if id != "" && report == nil {
		return nil, usererr.New("This benchmark run is no longer available.")
	}
	return &BenchmarkReport{report, history, warning}, nil
}
