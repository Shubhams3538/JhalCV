// Package usage keeps a local ledger of AI calls: token counts and estimated
// prices, never prompts or credentials. It reads and writes the same
// .local/usage.db as resume/usage.py.
package usage

import (
	"context"
	"database/sql"
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"

	_ "modernc.org/sqlite" // Registers the "sqlite" database driver.

	"jhalcv/internal/usererr"
)

// Checked is the date the price table below was last verified.
const Checked = "2026-10-25"

// Sources link to each provider's official pricing.
var Sources = map[string]string{
	"openai":     "https://developers.openai.com/api/docs/pricing",
	"anthropic":  "https://platform.claude.com/docs/en/about-claude/pricing",
	"gemini":     "https://ai.google.dev/gemini-api/docs/pricing",
	"openrouter": "https://openrouter.ai/docs/cookbook/administration/usage-accounting",
}

// rate is USD per million tokens: fresh input, output, cache read, cache write.
type rate struct{ fresh, output, cached, creation float64 }

var rates = map[string]rate{
	"gpt-6.1-sol":           {1, 5, .05, 1.25},
	"gpt-5.4-mini":          {.75, 4.5, .075, .75},
	"gpt-5.5":               {5, 30, .5, 5},
	"gpt-4o-mini":           {.15, .6, .075, .15},
	"claude-sonnet-5-5":     {2, 10, .1, 2.5},
	"claude-opus-5-5":       {4, 20, .2, 5},
	"claude-haiku-5-5":      {.1, .5, .01, .125},
	"claude-sonnet-4-6":     {3, 15, .3, 3.75},
	"claude-sonnet-4-5":     {3, 15, .3, 3.75},
	"claude-haiku-4-5":      {1, 5, .1, 1.25},
	"gemini-3.8-flash":      {.75, 3.75, .075, .75},
	"gemini-3.5-flash-lite": {.3, 2.5, .03, .3},
}

// References are the models a Codex subscription call can be valued at, in
// the same order Python lists them.
var References = []string{"gpt-6.1-sol", "gpt-5.4-mini", "gpt-5.5", "gpt-4o-mini"}

var dateSuffix = regexp.MustCompile(`-(?:\d{8}|\d{4}-\d{2}-\d{2})$`)

// ModelKey turns a provider's model name into a price-table key.
func ModelKey(model string) string {
	if i := strings.LastIndex(model, "/"); i >= 0 {
		model = model[i+1:]
	}
	model = dateSuffix.ReplaceAllString(model, "")
	if strings.HasPrefix(model, "claude-") {
		model = strings.ReplaceAll(model, "5.5", "5-5")
	}
	return model
}

// Known reports whether model has a price.
func Known(model string) bool {
	_, ok := rates[ModelKey(model)]
	return ok
}

func estimate(model string, inp, out *int, read, write int, at string, write1h int) *float64 {
	key := ModelKey(model)
	r, ok := rates[key]
	if !ok || inp == nil || out == nil || read+write > *inp {
		return nil
	}
	// Unknown long-context tiers are left unpriced rather than undercounted.
	if key == "gpt-6.1-sol" && *inp > 272000 {
		return nil
	}
	if key == "gpt-5.5" && *inp > 272000 {
		r = rate{r.fresh * 2, r.output * 1.5, r.cached * 2, r.creation * 2}
	}
	if key == "claude-haiku-5-5" && *inp > 100000 {
		r = rate{.5, 2.5, .05, .625}
	}
	if key == "gemini-3.8-flash" && len(at) >= 10 && at[:10] >= "2027-01-01" {
		r = rate{1.5, 7.5, .15, 1.5}
	}
	cost := (float64(*inp-read-write)*r.fresh + float64(*out)*r.output + float64(read)*r.cached +
		float64(write-write1h)*r.creation + float64(write1h)*r.fresh*2) / 1_000_000
	return &cost
}

// Store is the ledger database.
type Store struct {
	db   *sql.DB
	mu   sync.Mutex
	fail bool // An accounting write failed; shown as a warning.
}

// Open opens (creating if needed) root/.local/usage.db.
func Open(root string) (*Store, error) {
	path := filepath.Join(root, ".local", "usage.db")
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	db, err := sql.Open("sqlite", "file:"+path+"?_pragma=busy_timeout(10000)")
	if err != nil {
		return nil, err
	}
	for _, stmt := range []string{
		"CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
		"CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at TEXT NOT NULL, data TEXT NOT NULL)",
	} {
		if _, err := db.Exec(stmt); err != nil {
			db.Close()
			return nil, err
		}
	}
	if _, err := db.Exec("INSERT OR IGNORE INTO meta VALUES ('started_at', ?), ('codex_reference', 'gpt-6.1-sol')", now()); err != nil {
		db.Close()
		return nil, err
	}
	_ = os.Chmod(path, 0o600)
	return &Store{db: db}, nil
}

// Close closes the database.
func (s *Store) Close() error { return s.db.Close() }

// now is the current UTC time in Python's isoformat() layout.
func now() string {
	return time.Now().UTC().Format("2006-01-02T15:04:05.000000-07:00")
}

func (s *Store) reference() string {
	var value string
	if err := s.db.QueryRow("SELECT value FROM meta WHERE key='codex_reference'").Scan(&value); err != nil {
		return "gpt-6.1-sol"
	}
	return value
}

// SetReference chooses the model that values Codex subscription calls.
func (s *Store) SetReference(model any) (*Summary, error) {
	name, _ := model.(string)
	if !slices.Contains(References, name) {
		return nil, usererr.New("Choose a listed valuation model.")
	}
	if _, err := s.db.Exec("UPDATE meta SET value=? WHERE key='codex_reference'", name); err != nil {
		return nil, err
	}
	return s.Summary()
}

// event is one ledger row, stored as JSON in the same shape as Python's.
type event struct {
	Provider          string   `json:"provider"`
	Model             string   `json:"model"`
	ValuationModel    string   `json:"valuation_model"`
	Channel           string   `json:"channel"`
	Purpose           string   `json:"purpose"`
	InputTokens       *int     `json:"input_tokens"`
	OutputTokens      *int     `json:"output_tokens"`
	CachedInputTokens int      `json:"cached_input_tokens"`
	CacheWriteTokens  int      `json:"cache_write_tokens"`
	CostUSD           *float64 `json:"cost_usd"`
	CostBasis         string   `json:"cost_basis"`
	CacheHit          bool     `json:"cache_hit"`
	RatesChecked      string   `json:"rates_checked"`
}

// Call describes one AI call to record. Nil token counts are unknown.
type Call struct {
	Provider, Model string
	Input, Output   *int
	Read, Write     int
	Write1h         int
	Channel         string   // "api" (default) or "subscription"
	Purpose         string   // "resume" (default) or "jobs"
	Reported        *float64 // A price the provider reported itself.
	Basis           string   // How Reported was measured.
	CacheHit        bool
}

// Record adds one call to the ledger. It never fails the caller: accounting
// problems must never cause a paid inference to be retried.
func (s *Store) Record(c Call) {
	at := now()
	if c.Channel == "" {
		c.Channel = "api"
	}
	if c.Purpose == "" {
		c.Purpose = "resume"
	}
	if c.Input != nil && (c.Read+c.Write > *c.Input || c.Write1h > c.Write) {
		c.Input = nil
	}
	valuation := c.Model
	if c.Provider == "codex" {
		valuation = s.reference()
	}
	cost, basis := money(c.Reported), c.Basis
	if cost == nil {
		cost, basis = estimate(valuation, c.Input, c.Output, c.Read, c.Write, at, c.Write1h), "estimated"
	}
	if cost == nil {
		basis = "unknown"
	}
	data, err := json.Marshal(event{
		Provider: c.Provider, Model: c.Model, ValuationModel: valuation, Channel: c.Channel,
		Purpose: c.Purpose, InputTokens: c.Input, OutputTokens: c.Output,
		CachedInputTokens: c.Read, CacheWriteTokens: c.Write, CostUSD: cost,
		CostBasis: basis, CacheHit: c.CacheHit, RatesChecked: Checked,
	})
	if err == nil {
		_, err = s.db.Exec("INSERT INTO events(at,data) VALUES (?,?)", at, string(data))
	}
	if err != nil {
		s.mu.Lock()
		s.fail = true
		s.mu.Unlock()
	}
}

func money(v *float64) *float64 {
	if v == nil || math.IsInf(*v, 0) || math.IsNaN(*v) || *v < 0 {
		return nil
	}
	return v
}

// Totals add up a set of calls.
type Totals struct {
	InputTokens         int     `json:"input_tokens"`
	OutputTokens        int     `json:"output_tokens"`
	CachedInputTokens   int     `json:"cached_input_tokens"`
	CacheWriteTokens    int     `json:"cache_write_tokens"`
	CostUSD             float64 `json:"cost_usd"`
	APICostUSD          float64 `json:"api_cost_usd"`
	SubscriptionCostUSD float64 `json:"subscription_cost_usd"`
	Calls               int     `json:"calls"`
	CacheHits           int     `json:"cache_hits"`
	Unmeasured          int     `json:"unmeasured"`
	Unpriced            int     `json:"unpriced"`
}

func (t *Totals) add(e event) {
	if e.CacheHit {
		t.CacheHits++
		return
	}
	t.Calls++
	if e.InputTokens == nil || e.OutputTokens == nil {
		t.Unmeasured++
	}
	if e.CostUSD == nil {
		t.Unpriced++
	}
	t.InputTokens += deref(e.InputTokens)
	t.OutputTokens += deref(e.OutputTokens)
	t.CachedInputTokens += e.CachedInputTokens
	t.CacheWriteTokens += e.CacheWriteTokens
	if e.CostUSD != nil {
		t.CostUSD += *e.CostUSD
		if e.Channel == "subscription" {
			t.SubscriptionCostUSD += *e.CostUSD
		} else {
			t.APICostUSD += *e.CostUSD
		}
	}
}

func deref(p *int) int {
	if p == nil {
		return 0
	}
	return *p
}

// Group is the totals for one provider, model and channel.
type Group struct {
	Totals
	Provider       string `json:"provider"`
	Model          string `json:"model"`
	Channel        string `json:"channel"`
	ValuationModel string `json:"valuation_model"`
}

// Summary is what the Usage page shows.
type Summary struct {
	StartedAt       string            `json:"started_at"`
	CodexReference  string            `json:"codex_reference"`
	ReferenceModels []string          `json:"reference_models"`
	RatesChecked    string            `json:"rates_checked"`
	Sources         map[string]string `json:"sources"`
	Total           Totals            `json:"total"`
	Today           Totals            `json:"today"`
	Groups          []*Group          `json:"groups"`
	AccountingError bool              `json:"accounting_error"`
}

// Summary adds up every recorded call.
func (s *Store) Summary() (*Summary, error) {
	out := &Summary{ReferenceModels: References, RatesChecked: Checked, Sources: Sources, Groups: []*Group{}}
	rows, err := s.db.Query("SELECT key, value FROM meta")
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var key, value string
		if err := rows.Scan(&key, &value); err != nil {
			rows.Close()
			return nil, err
		}
		switch key {
		case "started_at":
			out.StartedAt = value
		case "codex_reference":
			out.CodexReference = value
		}
	}
	rows.Close()

	rows, err = s.db.Query("SELECT at, data FROM events ORDER BY id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	groups := map[[4]string]*Group{}
	today := time.Now().Format(time.DateOnly)
	for rows.Next() {
		var at, data string
		if err := rows.Scan(&at, &data); err != nil {
			return nil, err
		}
		var e event
		if json.Unmarshal([]byte(data), &e) != nil {
			continue
		}
		key := [4]string{e.Provider, e.Model, e.Channel, e.ValuationModel}
		g, ok := groups[key]
		if !ok {
			g = &Group{Provider: key[0], Model: key[1], Channel: key[2], ValuationModel: key[3]}
			groups[key] = g
			out.Groups = append(out.Groups, g)
		}
		out.Total.add(e)
		g.Totals.add(e)
		if when, err := time.Parse("2006-01-02T15:04:05.999999-07:00", at); err == nil && when.Local().Format(time.DateOnly) == today {
			out.Today.add(e)
		}
	}
	s.mu.Lock()
	out.AccountingError = s.fail
	s.mu.Unlock()
	return out, rows.Err()
}

// ── Scopes: which ledger and purpose the current request records into ──

type scopeKey struct{}

type scope struct {
	store   *Store
	purpose string
}

// WithScope makes AI calls made with ctx record into store for purpose
// ("resume" or "jobs"), like usage_scope() in Python.
func WithScope(ctx context.Context, store *Store, purpose string) context.Context {
	if store == nil {
		return context.WithValue(ctx, scopeKey{}, (*scope)(nil))
	}
	return context.WithValue(ctx, scopeKey{}, &scope{store, purpose})
}

func from(ctx context.Context) *scope {
	s, _ := ctx.Value(scopeKey{}).(*scope)
	return s
}

// RecordCache records an answer served from a local cache (no new tokens).
func RecordCache(ctx context.Context, provider, model string) {
	if s := from(ctx); s != nil {
		zero := 0.0
		s.store.Record(Call{Provider: provider, Model: model, Input: intp(0), Output: intp(0),
			Purpose: s.purpose, Reported: &zero, Basis: "local cache", CacheHit: true})
	}
}

// RecordAPI records a direct provider API call from its decoded response.
func RecordAPI(ctx context.Context, provider, model string, response map[string]any) {
	s := from(ctx)
	if s == nil {
		return
	}
	usageKey := "usage"
	if provider == "gemini" {
		usageKey = "usageMetadata"
	}
	u, _ := response[usageKey].(map[string]any)
	var inp, out *int
	read, write, hour := 0, 0, 0
	switch provider {
	case "openai", "openrouter":
		inp, out = count(u["prompt_tokens"]), count(u["completion_tokens"])
		if details, ok := u["prompt_tokens_details"].(map[string]any); ok {
			read, write = deref(count(details["cached_tokens"])), deref(count(details["cache_write_tokens"]))
		}
	case "anthropic", "zai": // Z.AI's endpoint reports Anthropic-style usage.
		inp, out = count(u["input_tokens"]), count(u["output_tokens"])
		read, write = deref(count(u["cache_read_input_tokens"])), deref(count(u["cache_creation_input_tokens"]))
		if inp != nil {
			inp = intp(*inp + read + write)
		}
		if detail, ok := u["cache_creation"].(map[string]any); ok {
			hour = deref(count(detail["ephemeral_1h_input_tokens"]))
		}
	case "gemini":
		inp, out = count(u["promptTokenCount"]), count(u["candidatesTokenCount"])
		if inp != nil {
			inp = intp(*inp + deref(count(u["toolUsePromptTokenCount"])))
		}
		if out != nil {
			out = intp(*out + deref(count(u["thoughtsTokenCount"])))
		}
		read = deref(count(u["cachedContentTokenCount"]))
	}
	modelKey := "model"
	if provider == "gemini" {
		modelKey = "modelVersion"
	}
	// Only accept model-like metadata; never store arbitrary response content.
	if actual, ok := response[modelKey].(string); ok && Known(actual) {
		model = actual
	}
	channel := "api"
	if provider == "zai" {
		channel = "subscription" // Coding Plan quota, not pay-per-token.
	}
	var reported *float64
	if provider == "openrouter" {
		reported = number(u["cost"])
	}
	s.store.Record(Call{Provider: provider, Model: model, Input: inp, Output: out, Read: read,
		Write: write, Write1h: hour, Channel: channel, Purpose: s.purpose, Reported: reported, Basis: "reported"})
}

var claudeModel = regexp.MustCompile(`^claude-[a-z0-9.-]{1,100}$`)

// RecordAgent records a Codex or Claude Code CLI call from its output.
func RecordAgent(ctx context.Context, name, stdout string) {
	s := from(ctx)
	if s == nil {
		return
	}
	if name == "codex" {
		found := false
		for _, line := range strings.Split(stdout, "\n") {
			var ev map[string]any
			if json.Unmarshal([]byte(line), &ev) != nil || ev["type"] != "turn.completed" {
				continue
			}
			u, _ := ev["usage"].(map[string]any)
			s.store.Record(Call{Provider: "codex", Model: "codex-default", Input: count(u["input_tokens"]),
				Output: count(u["output_tokens"]), Read: deref(count(u["cached_input_tokens"])),
				Write: deref(count(u["cache_write_input_tokens"])), Channel: "subscription", Purpose: s.purpose})
			found = true
		}
		if !found {
			s.store.Record(Call{Provider: "codex", Model: "codex-default", Channel: "subscription", Purpose: s.purpose})
		}
		return
	}

	var data map[string]any
	_ = json.Unmarshal([]byte(stdout), &data)
	if models, ok := data["modelUsage"].(map[string]any); ok && len(models) > 0 {
		for model, raw := range models {
			u, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			if !claudeModel.MatchString(model) {
				model = "unreported model"
			}
			read, write := deref(count(u["cacheReadInputTokens"])), deref(count(u["cacheCreationInputTokens"]))
			inp := count(u["inputTokens"])
			if inp != nil {
				inp = intp(*inp + read + write)
			}
			s.store.Record(Call{Provider: "claude", Model: model, Input: inp, Output: count(u["outputTokens"]),
				Read: read, Write: write, Channel: "subscription", Purpose: s.purpose,
				Reported: number(u["costUSD"]), Basis: "CLI estimate"})
		}
		return
	}
	u, _ := data["usage"].(map[string]any)
	read, write := deref(count(u["cache_read_input_tokens"])), deref(count(u["cache_creation_input_tokens"]))
	inp := count(u["input_tokens"])
	if inp != nil {
		inp = intp(*inp + read + write)
	}
	s.store.Record(Call{Provider: "claude", Model: "sonnet (unreported model)", Input: inp,
		Output: count(u["output_tokens"]), Read: read, Write: write, Channel: "subscription",
		Purpose: s.purpose, Reported: number(data["total_cost_usd"]), Basis: "CLI estimate"})
}

// count accepts a decoded JSON whole number that is not negative.
func count(v any) *int {
	n, ok := v.(float64)
	if !ok || n < 0 || n != math.Trunc(n) || n > math.MaxInt32*1024 {
		return nil
	}
	return intp(int(n))
}

func number(v any) *float64 {
	n, ok := v.(float64)
	if !ok {
		return nil
	}
	return &n
}

func intp(n int) *int { return &n }
