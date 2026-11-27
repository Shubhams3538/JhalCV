package jobs

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"jhalcv/internal/storage"
	"jhalcv/internal/usage"
	"jhalcv/internal/usererr"
)

// Refresh limits.
const (
	titleScreenMin = 16
	maxDetails     = 40 // Detail pages fetched per source per run.
	sourceWorkers  = 8
	detailWorkers  = 12
	detailWindow   = 3 // One source cannot reserve every detail worker.
	sourceSeconds  = 150 * time.Second
	maxChecks      = 100_000
	resolveEvery   = 14 * 24 * time.Hour // How often a page is re-examined for a feed.
	resolveRetry   = 24 * time.Hour      // Sooner for a page that keeps failing.
)

// testSlice is how many direct-feed and page companies a test refresh reads.
var testSlice = [2]int{20, 10}

func keywords(value string) []string {
	var out []string
	for _, w := range strings.Split(value, ",") {
		if w = strings.ToLower(strings.TrimSpace(w)); w != "" {
			out = append(out, w)
		}
	}
	return out
}

// TitleMatches applies a source's include and exclude title keywords.
func TitleMatches(title, include, exclude string) bool {
	title = strings.ToLower(title)
	inc, exc := keywords(include), keywords(exclude)
	contains := func(w string) bool { return strings.Contains(title, w) }
	return (len(inc) == 0 || slices.ContainsFunc(inc, contains)) && !slices.ContainsFunc(exc, contains)
}

// Service refreshes career sources while an independent queue reviews
// ready job text.
type Service struct {
	Root       string
	Store      *Store
	Benchmarks *Benchmarks
	Usage      *usage.Store
	scratch    bool // A Test lab copy, never the saved collection.
	lockPath   string
	// NewAsker builds the job AI for one run.
	NewAsker func() Asker

	mu        sync.Mutex
	progress  map[string]any
	lastPrune time.Time
}

// NewService opens the jobs database root/.local/<dbName>.
func NewService(root, dbName string, ledger *usage.Store, newAsker func() Asker) (*Service, error) {
	store, err := OpenStore(root, dbName)
	if err != nil {
		return nil, err
	}
	return &Service{Root: root, Store: store, Benchmarks: NewBenchmarks(root), Usage: ledger,
		scratch: dbName != "jobs.db", lockPath: filepath.Join(root, ".local", "jobs.lock"), NewAsker: newAsker,
		progress: map[string]any{"running": false, "message": "", "finished_at": nil, "error": nil}}, nil
}

// Progress is a copy of the current run's progress.
func (s *Service) Progress() map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make(map[string]any, len(s.progress))
	for k, v := range s.progress {
		out[k] = v
	}
	return out
}

// Running reports whether a refresh is in progress.
func (s *Service) Running() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.progress["running"] == true
}

func (s *Service) update(values map[string]any) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for k, v := range values {
		s.progress[k] = v
	}
}

func (s *Service) say(message string) { s.update(map[string]any{"message": message}) }

// Experience is the years used for eligibility.
type Experience struct {
	Years    float64 `json:"years"`
	Computed float64 `json:"computed"`
	Custom   bool    `json:"custom"`
}

// ExperienceYears is the saved override, or the years the profile covers.
func (s *Service) ExperienceYears(ctx context.Context, p *storage.Profile) (Experience, error) {
	saved, ok, err := s.Store.Meta(ctx, "experience_years")
	if err != nil {
		return Experience{}, err
	}
	computed := ProfileYears(p)
	e := Experience{Years: computed, Computed: computed, Custom: ok}
	if ok {
		e.Years, _ = strconv.ParseFloat(saved, 64)
	}
	return e, nil
}

// SetExperienceYears saves an override ("" or nil clears it) and clears
// verdicts so jobs are rechecked.
func (s *Service) SetExperienceYears(ctx context.Context, value any) error {
	switch v := value.(type) {
	case nil:
		if err := s.Store.SetMeta(ctx, "experience_years", nil); err != nil {
			return err
		}
	default:
		if str, isText := v.(string); isText && str == "" {
			if err := s.Store.SetMeta(ctx, "experience_years", nil); err != nil {
				return err
			}
			break
		}
		years, ok := toFloat(v)
		if !ok {
			return usererr.New("Enter your years of experience as a number, e.g. 1.5.")
		}
		years = roundTenth(years)
		if years < 0 || years > 50 {
			return usererr.New("Enter years of experience between 0 and 50.")
		}
		value := formatYears(years)
		if err := s.Store.SetMeta(ctx, "experience_years", &value); err != nil {
			return err
		}
	}
	return s.Store.ResetEligibility(ctx)
}

func toFloat(v any) (float64, bool) {
	switch x := v.(type) {
	case float64:
		return x, true
	case bool:
		if x {
			return 1, true
		}
		return 0, true
	case string:
		f, err := strconv.ParseFloat(strings.TrimSpace(x), 64)
		return f, err == nil
	}
	return 0, false
}

func roundTenth(v float64) float64 {
	return float64(int64(v*10+copySign(0.5, v))) / 10
}

func copySign(a, b float64) float64 {
	if b < 0 {
		return -a
	}
	return a
}

// Locations normalizes a comma-separated location list.
func Locations(value any) string {
	raw := firstRunes(text(value), 500)
	var parts []string
	for _, p := range strings.Split(raw, ",") {
		if p = strings.TrimSpace(p); p != "" {
			parts = append(parts, p)
		}
	}
	return strings.Join(parts, ", ")
}

// AddSource saves a new careers page.
func (s *Service) AddSource(ctx context.Context, name, link, include, exclude, locations any) (*Source, error) {
	clean := firstRunes(strings.Join(strings.Fields(text(name)), " "), 100)
	if clean == "" {
		return nil, usererr.New("Enter the company name.")
	}
	raw, _ := link.(string)
	checked, err := CheckURL(ctx, raw)
	if err != nil {
		return nil, err
	}
	return s.Store.AddSource(ctx, clean, checked, Detect(checked), firstRunes(text(include), 500), firstRunes(text(exclude), 500), Locations(locations))
}

// EditSourceURL changes a company's careers link. A Jibe site keeps its kind
// when the new link is on the same host.
func (s *Service) EditSourceURL(ctx context.Context, id int64, link any) error {
	raw, _ := link.(string)
	checked, err := CheckURL(ctx, raw)
	if err != nil {
		return err
	}
	kind := Detect(checked)
	sources, err := s.Store.Sources(ctx)
	if err != nil {
		return err
	}
	for _, src := range sources {
		if src.ID == id && src.Kind == "jibe" && hostOf(src.URL) == hostOf(checked) {
			kind = "jibe"
		}
	}
	return s.Store.EditSourceURL(ctx, id, checked, kind)
}

func hostOf(link string) string {
	u, err := url.Parse(link)
	if err != nil {
		return ""
	}
	return u.Hostname()
}

// PreviewSources validates a company CSV.
func (s *Service) PreviewSources(ctx context.Context, content any) (*ImportPreview, error) {
	sources, err := s.Store.Sources(ctx)
	if err != nil {
		return nil, err
	}
	return PreviewCSV(ctx, content, sources)
}

// ImportSources saves reviewed CSV rows.
func (s *Service) ImportSources(ctx context.Context, rows any, locations any) (map[string]int, error) {
	list, ok := rows.([]any)
	if !ok || len(list) < 1 || len(list) > MaxImportRows {
		return nil, usererr.Errorf("Choose between 1 and %d companies to import.", MaxImportRows)
	}
	var entries []ImportRowEntry
	for _, r := range list {
		row, ok := r.(map[string]any)
		if !ok {
			return nil, usererr.New("Each company must have a company name and URL.")
		}
		name, link, err := ValidateEntry(ctx, row["company"], row["url"])
		if err != nil {
			return nil, err
		}
		entries = append(entries, ImportRowEntry{name, link, Detect(link)})
	}
	return s.Store.ImportSources(ctx, entries, Locations(locations))
}

// Start begins a refresh in the background; false if one is running.
func (s *Service) Start(p *storage.Profile, recheckOnly bool, sourceIDs []int64, test bool) bool {
	s.mu.Lock()
	if s.progress["running"] == true {
		s.mu.Unlock()
		return false
	}
	phase := "fetch"
	if recheckOnly {
		phase = "review"
	}
	s.progress = map[string]any{"running": true, "phase": phase, "test": test, "message": "Starting…", "finished_at": nil, "error": nil}
	s.mu.Unlock()
	go func() {
		summary, err := s.Refresh(context.Background(), p, recheckOnly, sourceIDs, test)
		if err != nil {
			msg := err.Error()
			if msg == "" {
				msg = "The refresh failed."
			}
			s.update(map[string]any{"running": false, "message": "", "error": msg, "finished_at": time.Now().Unix()})
			return
		}
		s.update(map[string]any{"running": false, "message": summary, "finished_at": time.Now().Unix()})
	}()
	return true
}

// Refresh reads every source (or only sourceIDs) and checks new jobs. A test
// refresh uses the Test lab scratch store, starting blank.
func (s *Service) Refresh(ctx context.Context, p *storage.Profile, recheckOnly bool, sourceIDs []int64, test bool) (summary string, err error) {
	if test && !s.scratch {
		return "", usererr.New("Test runs use the Test lab scratch database; the saved collection is never cleared.")
	}
	if err := os.MkdirAll(filepath.Dir(s.lockPath), 0o755); err != nil {
		return "", err
	}
	lock, err := os.OpenFile(s.lockPath, os.O_RDWR|os.O_CREATE|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return "", err
	}
	defer lock.Close()
	if syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB) != nil {
		return "", usererr.New("Another job refresh is already running.")
	}
	defer syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
	if test && !recheckOnly {
		if err := s.testReset(ctx); err != nil {
			return "", err
		}
	}
	mode := "refresh"
	switch {
	case test && !recheckOnly:
		mode = "test"
	case recheckOnly:
		mode = "review"
	}
	ctx, finish := s.Benchmarks.Start(ctx, mode)
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("refresh failed: %v", r)
		}
		finish(err)
	}()
	return s.refreshLocked(ctx, p, recheckOnly, sourceIDs, test)
}

func (s *Service) testReset(ctx context.Context) error {
	main, err := OpenStore(s.Root, "jobs.db")
	if err != nil {
		return err
	}
	defer main.Close()
	sources, err := main.SourcesByID(ctx)
	if err != nil {
		return err
	}
	years, ok, err := main.Meta(ctx, "experience_years")
	if err != nil {
		return err
	}
	var yearsPtr *string
	if ok {
		yearsPtr = &years
	}
	return s.Store.ResetTestCollection(ctx, sources, yearsPtr)
}

func (s *Service) refreshLocked(ctx context.Context, p *storage.Profile, recheckOnly bool, sourceIDs []int64, test bool) (string, error) {
	if err := s.Store.PruneOld(ctx); err != nil {
		return "", err
	}
	agents := s.NewAsker()
	if recheckOnly {
		return s.reviewSaved(ctx, p, agents, sourceIDs)
	}
	all, err := s.Store.Sources(ctx)
	if err != nil {
		return "", err
	}
	var chosen []Source
	for _, src := range all {
		if sourceIDs == nil || slices.Contains(sourceIDs, src.ID) {
			chosen = append(chosen, src)
		}
	}
	sampled := test && sourceIDs == nil
	if sampled {
		// A random slice of each kind, so test runs cover different companies.
		before := len(chosen)
		slices.SortFunc(chosen, func(a, b Source) int { return int(a.ID - b.ID) })
		var feeds, pages []Source
		for _, src := range chosen {
			if src.Kind == "page" {
				pages = append(pages, src)
			} else {
				feeds = append(feeds, src)
			}
		}
		chosen = append(sample(feeds, testSlice[0]), sample(pages, testSlice[1])...)
		var names []string
		for _, src := range chosen {
			names = append(names, src.Name)
		}
		slices.Sort(names)
		s.say(fmt.Sprintf("Test mode: %d direct + %d page companies chosen at random (%d skipped): %s.",
			testSlice[0], testSlice[1], before-len(chosen), strings.Join(names, ", ")))
	}

	exp, err := s.ExperienceYears(ctx, p)
	if err != nil {
		return "", err
	}
	s.update(map[string]any{"phase": "fetch", "total": len(chosen), "completed": 0, "active": []string{},
		"started_at": time.Now().Unix(), "review_total": 0, "review_completed": 0, "review_active": 0, "review_queued": 0})
	// Direct feeds finish first; slow rendered pages cannot hold up the queue.
	slices.SortStableFunc(chosen, func(a, b Source) int { return boolInt(a.Kind == "page") - boolInt(b.Kind == "page") })
	var scope []int64
	if sourceIDs != nil || sampled {
		scope = []int64{}
		for _, src := range chosen {
			scope = append(scope, src.ID)
		}
	}
	reviews := StartReview(ctx, s.Store, exp.Years, agents, s.reviewProgress, maxChecks)
	found, added, failures := s.fetchIntoReviews(ctx, chosen, agents, exp.Years, reviews)
	// Catch saved jobs of failed sources too, without scheduling an ID twice.
	if !reviews.Full() {
		rest, err := s.Store.Unchecked(ctx, 0, scope, reviews.Seen(), true)
		if err == nil {
			err = reviews.Add(rest)
		}
		if err != nil {
			_ = reviews.Close()
			return "", err
		}
	}
	total, completed := reviews.Totals()
	s.update(map[string]any{"phase": "review", "total": total, "completed": completed, "active": []string{}})
	reviews.Report()
	if err := reviews.Close(); err != nil {
		return "", err
	}
	_, checked := reviews.Totals()

	if err := s.Store.PruneOld(ctx); err != nil {
		return "", err
	}
	now := strconv.FormatInt(time.Now().Unix(), 10)
	if err := s.Store.SetMeta(ctx, "last_refresh", &now); err != nil {
		return "", err
	}
	label := ""
	if sourceIDs != nil || sampled {
		var names []string
		for _, src := range chosen {
			names = append(names, src.Name)
		}
		label = strings.Join(names, ", ") + ": "
	}
	parts := []string{fmt.Sprintf("%s%d matching job(s), %d new", label, found, added)}
	if checked > 0 {
		parts = append(parts, fmt.Sprintf("%d checked by %s", checked, usedNames(agents)))
	}
	if len(failures) > 0 {
		parts = append(parts, "could not read: "+strings.Join(failures, ", "))
	}
	out := strings.Join(parts, ". ")
	return strings.ToUpper(out[:1]) + out[1:] + ".", nil
}

func usedNames(agents Asker) string {
	var names []string
	for _, a := range agents.Used() {
		names = append(names, AgentNames[a])
	}
	if len(names) == 0 {
		return "text rules"
	}
	return strings.Join(names, ", ")
}

func sample(items []Source, n int) []Source {
	out := slices.Clone(items)
	rand.Shuffle(len(out), func(i, j int) { out[i], out[j] = out[j], out[i] })
	return out[:min(n, len(out))]
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

type sourceResult struct {
	src          Source
	found, added int
	failure      string
}

// fetchIntoReviews reads sources on a worker pool. Between finished
// sources, one ready source's unchecked jobs enter the review queue, so a
// large company cannot hold back another company's reviews.
func (s *Service) fetchIntoReviews(ctx context.Context, chosen []Source, agents Asker, years float64, reviews *ReviewQueue) (found, added int, failures []string) {
	window := max(detailWindow, detailWorkers/max(1, min(len(chosen), sourceWorkers)))
	details := NewSlots(detailWorkers)
	results := make(chan sourceResult)
	workers := NewSlots(sourceWorkers)
	var active sync.Map
	activeNames := func() []string {
		var names []string
		for _, src := range chosen {
			if _, ok := active.Load(src.ID); ok {
				names = append(names, src.Name)
			}
		}
		return orEmptyStrings(names)
	}
	for _, src := range chosen {
		queued := time.Now()
		go func() {
			workers <- struct{}{}
			defer func() { <-workers }()
			active.Store(src.ID, true)
			s.update(map[string]any{"active": activeNames()})
			sctx := WithCompany(ctx, src.ID, src.Name)
			Mark(sctx, "source_queue", time.Since(queued).Seconds())
			end := Measure(sctx, "source")
			result := s.safeRefreshSource(sctx, src, agents, details, years, window)
			end(result.failure != "")
			active.Delete(src.ID)
			s.mu.Lock()
			s.progress["active"] = activeNames()
			done, _ := s.progress["completed"].(int)
			s.progress["completed"] = done + 1
			s.mu.Unlock()
			results <- result
		}()
	}
	var ready []int64
	for pending := len(chosen); pending > 0 || len(ready) > 0; {
		var r sourceResult
		got := false
		if pending > 0 {
			if len(ready) > 0 {
				select {
				case r = <-results:
					got = true
				default:
				}
			} else {
				r, got = <-results, true
			}
		}
		if got {
			pending--
			found += r.found
			added += r.added
			if r.failure != "" {
				failures = append(failures, r.failure)
			}
			completed, _ := s.Progress()["completed"].(int)
			s.say(fmt.Sprintf("%d/%d companies completed", completed, len(chosen)))
			ready = append(ready, r.src.ID)
		}
		if reviews.Full() {
			ready = nil
		} else if len(ready) > 0 {
			id := ready[0]
			ready = ready[1:]
			batch, err := s.Store.Unchecked(ctx, Batch, []int64{id}, reviews.Seen(), true)
			if err == nil {
				err = reviews.Add(batch)
			}
			if err == nil && len(batch) == Batch {
				ready = append(ready, id)
			}
		}
	}
	return found, added, failures
}

func orEmptyStrings(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

// safeRefreshSource turns any failure, even a panic in a reader, into one
// company's failure: one company must never abort the others.
func (s *Service) safeRefreshSource(ctx context.Context, src Source, agents Asker, details Slots, years float64, window int) (result sourceResult) {
	defer func() {
		if r := recover(); r != nil {
			msg := fmt.Sprint(r)
			_ = s.Store.SourceResult(ctx, src.ID, nil, &msg)
			result = sourceResult{src: src, failure: src.Name}
		}
	}()
	return s.refreshSource(ctx, src, agents, details, years, window)
}

func (s *Service) reviewProgress(stats ReviewStats) {
	s.mu.Lock()
	s.progress["review_total"], s.progress["review_completed"] = stats.Total, stats.Completed
	s.progress["review_active"], s.progress["review_queued"] = stats.Active, stats.Queued
	var message string
	if s.progress["phase"] == "review" {
		s.progress["completed"] = stats.Completed
		message = fmt.Sprintf("Reviewing jobs: %d/%d completed", stats.Completed, stats.Total)
	} else {
		message = fmt.Sprintf("%v/%v companies completed; %d/%d jobs reviewed", orZero(s.progress["completed"]), orZero(s.progress["total"]), stats.Completed, stats.Total)
	}
	s.progress["message"] = message + fmt.Sprintf("; %d in review, %d queued.", stats.Active, stats.Queued)
	s.mu.Unlock()
}

func orZero(v any) any {
	if v == nil {
		return 0
	}
	return v
}

func (s *Service) reviewSaved(ctx context.Context, p *storage.Profile, agents Asker, sourceIDs []int64) (string, error) {
	// Snapshot the queue once: uncertain answers must never loop, and jobs
	// arriving later belong to the next run.
	todo, err := s.Store.Unchecked(ctx, 0, sourceIDs, nil, true)
	if err != nil {
		return "", err
	}
	s.update(map[string]any{"phase": "review", "total": len(todo), "completed": 0, "active": []string{}, "started_at": time.Now().Unix()})
	exp, err := s.ExperienceYears(ctx, p)
	if err != nil {
		return "", err
	}
	reviews := StartReview(ctx, s.Store, exp.Years, agents, s.reviewProgress, 0)
	addErr := reviews.Add(todo)
	if err := reviews.Close(); err != nil {
		return "", err
	}
	if addErr != nil {
		return "", addErr
	}
	now := strconv.FormatInt(time.Now().Unix(), 10)
	if err := s.Store.SetMeta(ctx, "last_review", &now); err != nil {
		return "", err
	}
	if len(todo) == 0 {
		return "No unreviewed saved jobs.", nil
	}
	return fmt.Sprintf("Reviewed %d saved job(s) using %s. Uncertain matches or dates remain in Needs review.", len(todo), usedNames(agents)), nil
}

var allowedPageParams = setOf("page", "sort", "sort_by", "sort_by_new", "descending")

// upgradePageSource looks for the hiring platform behind a careers page, at
// most every two weeks (daily while the source keeps failing).
func (s *Service) upgradePageSource(ctx context.Context, src Source) Source {
	end := Measure(ctx, "discovery")
	defer end(false)
	interval := resolveEvery
	if src.LastError != nil && *src.LastError != "" {
		interval = resolveRetry
	}
	if src.Kind != "page" || time.Since(time.Unix(src.ResolvedAt, 0)) < interval {
		return src
	}
	// A role or team filter in the URL must not be lost by switching from the
	// search page to a whole-board feed.
	u, _ := url.Parse(src.URL)
	for _, kv := range queryPairs(u.RawQuery) {
		key := strings.ToLower(kv[0])
		if !allowedPageParams[key] && !strings.HasPrefix(key, "utm_") {
			return src
		}
	}
	_ = s.Store.NoteResolveAttempt(ctx, src.ID)
	country := ""
	if slices.Contains(strings.Split(strings.ToLower(src.Locations), ","), "india") {
		country = "India"
	}
	found := Resolve(ctx, src.Name, src.URL, false, false, country)
	if found == nil || (found.Kind == "workday" && !strings.Contains(found.URL, "?") && src.Locations != "") {
		return src
	}
	upgraded, err := s.Store.UpgradeSource(ctx, src.ID, found.URL, found.Kind)
	if err != nil || upgraded == nil {
		return src
	}
	return *upgraded
}

func (s *Service) refreshSource(ctx context.Context, src Source, agents Asker, detailSlots Slots, years float64, window int) sourceResult {
	ctx = WithBudget(ctx, time.Now().Add(sourceSeconds))
	result := sourceResult{src: src}
	attempted := map[string]bool{}
	missingLocations := map[string]bool{}

	err := func() error {
		src = s.upgradePageSource(ctx, src)
		items, err := Timed(ctx, "listing", func() ([]Listing, error) {
			if src.Kind == "page" {
				return PageJobs(ctx, src.URL, AskWith(agents))
			}
			reader, ok := Readers[src.Kind]
			if !ok {
				return nil, fetchErr("Unknown source kind %q.", src.Kind)
			}
			return reader(ctx, src.URL)
		})
		if err != nil {
			return err
		}
		var kept []Listing
		for _, j := range items {
			// A listing without a location is kept: its detail page supplies
			// one, and views still hide jobs that never get one.
			if Freshness(j.Posted) != "expired" && TitleMatches(j.Title, src.Include, src.Exclude) &&
				(j.Location == "" || LocationMatches(j.Location, src.Locations)) {
				kept = append(kept, j)
			}
		}
		items = NewestFirst(kept, func(l Listing) string { return l.Posted })
		items = items[:min(len(items), MaxJobs)]
		detail := Details[src.Kind]
		for _, item := range items {
			if item.Location == "" {
				missingLocations[item.ExternalID] = true
			}
		}
		known, err := s.Store.KnownIDs(ctx, src.ID)
		if err != nil {
			return err
		}
		needsDetail := func(item Listing) bool {
			return !known[item.ExternalID] && item.NeedsDetail != "" && (item.Requirements == "" || Freshness(item.Posted) == "unknown")
		}
		// Screening pays off only when a large detail-only listing would
		// otherwise cost many requests.
		var screening []Listing
		for _, item := range items {
			if needsDetail(item) {
				screening = append(screening, item)
			}
		}
		screened, screenedBy := map[string]string{}, ""
		if detail != nil && len(screening) >= titleScreenMin {
			screened, screenedBy = ScreenTitles(ctx, screening, years, agents)
		}
		if detail != nil {
			var candidates []Listing
			for _, item := range items {
				if _, dropped := screened[item.ExternalID]; needsDetail(item) && !dropped {
					candidates = append(candidates, item)
				}
			}
			candidates = candidates[:min(len(candidates), maxDetails)]
			for _, c := range candidates {
				attempted[c.ExternalID] = true
			}
			byID := map[string]int{}
			for i, item := range items {
				byID[item.ExternalID] = i
			}
			for _, r := range s.details(ctx, candidates, detail, detailSlots, window) {
				i := byID[r.item.ExternalID]
				applyDetail(&items[i], r.detail)
				if Freshness(items[i].Posted) == "expired" {
					_ = s.Store.DismissExternal(ctx, src.ID, items[i].ExternalID)
					known[items[i].ExternalID] = true
				}
			}
		}
		var fresh []Listing
		detailsByID := map[string]Listing{}
		for _, j := range items {
			if Freshness(j.Posted) != "expired" {
				fresh = append(fresh, j)
				if j.NeedsDetail != "" {
					detailsByID[j.ExternalID] = j
				}
			}
		}
		items = fresh
		newIDs, err := s.Store.SaveListing(ctx, src.ID, items)
		if err != nil {
			return err
		}
		if len(screened) > 0 {
			var ext []string
			for id := range screened {
				ext = append(ext, id)
			}
			ids, err := s.Store.IDsByExternal(ctx, src.ID, ext)
			if err != nil {
				return err
			}
			by := screenedBy
			if by == "" {
				by = "rules"
			}
			verdicts := map[int64]Verdict{}
			no := "no"
			for e, id := range ids {
				verdicts[id] = Verdict{Eligible: "unclear", Reason: "Not checked: title screened out.", By: by, Relevant: &no, RelevanceReason: screened[e]}
			}
			if err := s.Store.SetEligibility(ctx, verdicts); err != nil {
				return err
			}
		}
		for ext := range attempted {
			_ = s.Store.NoteDetailAttempt(ctx, src.ID, ext)
		}
		if detail != nil && src.Kind != "page" {
			candidates, err := s.Store.DetailCandidates(ctx, src.ID, missingLocations)
			if err != nil {
				return err
			}
			var requests []Listing
			for _, row := range candidates {
				item, isListed := detailsByID[row.ExternalID]
				if isListed && !attempted[row.ExternalID] && len(requests) < maxDetails-len(attempted) {
					item.SavedID = row.ID
					requests = append(requests, item)
					_ = s.Store.NoteDetailAttempt(ctx, src.ID, row.ExternalID)
				}
			}
			for _, r := range s.details(ctx, requests, detail, detailSlots, window) {
				_ = s.Store.SetDetail(ctx, r.item.SavedID, r.detail)
			}
		}
		count := len(items)
		if err := s.Store.SourceResult(ctx, src.ID, &count, nil); err != nil {
			return err
		}
		result.found, result.added = len(items), len(newIDs)
		return nil
	}()
	if err != nil {
		if errors.Is(err, context.Canceled) {
			return sourceResult{src: src, failure: src.Name}
		}
		result.failure = src.Name
		Mark(ctx, "source_failure", 0)
		msg := firstRunes(err.Error(), 500)
		if msg == "" {
			msg = "Could not read this source."
		}
		_ = s.Store.SourceResult(ctx, src.ID, nil, &msg)
	}
	if src.Kind == "page" {
		candidates, err := s.Store.DetailCandidates(ctx, src.ID, missingLocations)
		if err == nil {
			var requests []Listing
			for _, row := range candidates {
				if !attempted[row.ExternalID] && row.URL != "" && len(requests) < maxDetails-len(attempted) {
					requests = append(requests, Listing{NeedsDetail: row.URL, SavedID: row.ID})
					_ = s.Store.NoteDetailAttempt(ctx, src.ID, row.ExternalID)
				}
			}
			for _, r := range s.details(ctx, requests, Details["page"], detailSlots, window) {
				_ = s.Store.SetDetail(ctx, r.item.SavedID, r.detail)
			}
		}
	}
	return result
}

func applyDetail(item *Listing, d Detail) {
	if d.Title != "" {
		item.Title = d.Title
	}
	if d.Location != "" {
		item.Location = d.Location
	}
	item.Requirements, item.Description = d.Requirements, d.Description
	if d.Posted != "" {
		item.Posted = d.Posted
	}
}

type detailResult struct {
	item   Listing
	detail Detail
}

// details fetches detail pages on the shared detail workers, at most window
// at a time for this source, until the source's deadline. Unreadable
// postings stay eligible for a later fetch.
func (s *Service) details(ctx context.Context, items []Listing, reader DetailReader, slots Slots, window int) []detailResult {
	deadline := Deadline(ctx)
	var mu sync.Mutex
	var out []detailResult
	var wg sync.WaitGroup
	own := make(chan struct{}, window)
	for _, item := range items {
		if !deadline.IsZero() && !time.Now().Before(deadline) {
			break
		}
		own <- struct{}{}
		queued := time.Now()
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer func() { <-own }()
			defer func() { _ = recover() }()
			if err := slots.Acquire(ctx, "Detail workers are busy.", "detail_queue"); err != nil {
				return
			}
			defer slots.Release()
			Mark(ctx, "detail_queue", time.Since(queued).Seconds())
			dctx := WithBudget(ctx, deadline)
			end := Measure(dctx, "details")
			if _, err := Remaining(dctx, fetchTimeout); err != nil {
				end(true)
				return
			}
			d, err := reader(dctx, item)
			end(err != nil)
			if err == nil {
				mu.Lock()
				out = append(out, detailResult{item, d})
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	return out
}

// State is the Jobs page.
type State struct {
	Sources         []Source       `json:"sources"`
	Jobs            []JobRow       `json:"jobs"`
	Progress        map[string]any `json:"progress"`
	Experience      Experience     `json:"experience"`
	LastRefresh     *string        `json:"last_refresh"`
	UnreviewedCount int            `json:"unreviewed_count"`
	Agents          []string       `json:"agents"`
}

// State gathers the Jobs page. Saved verdicts are re-guarded once whenever
// the guard or the experience changes; no AI is needed for that.
func (s *Service) State(ctx context.Context, p *storage.Profile) (*State, error) {
	s.mu.Lock()
	prune := time.Since(s.lastPrune) >= time.Minute
	if prune {
		s.lastPrune = time.Now()
	}
	s.mu.Unlock()
	if prune {
		if err := s.Store.PruneOld(ctx); err != nil {
			return nil, err
		}
	}
	exp, err := s.ExperienceYears(ctx, p)
	if err != nil {
		return nil, err
	}
	guarded := GuardVersion + ":" + formatYears(exp.Years)
	if current, _, err := s.Store.Meta(ctx, "eligibility_guard"); err != nil {
		return nil, err
	} else if current != guarded {
		if _, err := s.Store.GuardSaved(ctx, func(v Verdict, title, posting string) Verdict {
			return Guard(v, title, posting, exp.Years)
		}); err != nil {
			return nil, err
		}
		if err := s.Store.SetMeta(ctx, "eligibility_guard", &guarded); err != nil {
			return nil, err
		}
	}
	sources, err := s.Store.Sources(ctx)
	if err != nil {
		return nil, err
	}
	for i := range sources {
		sources[i].Label = SourceLabels[sources[i].Kind]
		if sources[i].Label == "" {
			sources[i].Label = "Career page"
		}
	}
	jobs, err := s.Store.Jobs(ctx)
	if err != nil {
		return nil, err
	}
	var last *string
	if v, ok, err := s.Store.Meta(ctx, "last_refresh"); err != nil {
		return nil, err
	} else if ok {
		last = &v
	}
	unchecked, err := s.Store.Unchecked(ctx, 0, nil, nil, false)
	if err != nil {
		return nil, err
	}
	return &State{Sources: sources, Jobs: jobs, Progress: s.Progress(), Experience: exp, LastRefresh: last,
		UnreviewedCount: len(unchecked), Agents: s.AgentLabels()}, nil
}

// AgentLabels names the job AI in the order it is tried.
func (s *Service) AgentLabels() []string {
	out := []string{}
	for _, a := range s.NewAsker().Order() {
		out = append(out, AgentNames[a])
	}
	return out
}
