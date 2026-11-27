// Package llm asks an AI for structured (JSON Schema) answers. It tries the
// user's API keys and subscription CLIs in order, fails over between them,
// and caches answers locally. It is the Go version of resume/llm.py.
package llm

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"time"

	"jhalcv/internal/usage"
)

// DefaultModel is used when a caller does not choose an OpenAI model.
const DefaultModel = "gpt-4o-mini"

// Error is any failure to get a valid answer. Callers fall back to rules
// rather than crash.
type Error struct {
	Message string
	Status  int    // HTTP status, when a provider answered with an error.
	Body    string // A short, secret-free hint about the failure.
	Auth    bool   // The key was missing or rejected; retrying cannot help.
}

func (e *Error) Error() string { return e.Message }

func newError(msg string) error { return &Error{Message: msg} }

// IsError reports whether err is an AI failure (as opposed to a bug).
func IsError(err error) bool {
	var e *Error
	return errors.As(err, &e)
}

// Route is one way to reach an AI: a saved API key, an environment key, or a
// subscription CLI (Claude Code or Codex).
type Route struct {
	ID       string `json:"id"`
	Provider string `json:"provider"`
	Label    string `json:"label,omitempty"`
	Model    string `json:"model"`
	Key      string `json:"-"` // Never encoded: keys must not reach the page.
	Source   string `json:"source,omitempty"`
	CLI      string `json:"cli,omitempty"`
}

// Used names a provider and model that answered.
type Used struct {
	Provider string `json:"provider"`
	Model    string `json:"model"`
}

// Session is the set of routes one operation may use, and what happened.
// Like credential_scope() in Python, it travels with the context.
type Session struct {
	Routes   []Route
	CacheDir string
	// CLINames limits which CLIs may be used. Nil allows every installed CLI;
	// an empty, non-nil list allows none.
	CLINames []string

	mu             sync.Mutex
	failed         map[string]bool
	preferred      string
	events         []string
	used           []Used
	lastCallFailed bool
}

// NewSession starts a session over routes, caching answers in cacheDir.
func NewSession(routes []Route, cacheDir string, cliNames []string) *Session {
	return &Session{Routes: routes, CacheDir: cacheDir, CLINames: cliNames, failed: map[string]bool{}}
}

// Events counts providers that failed during the session.
func (s *Session) Events() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.events)
}

// UsedProviders lists each provider and model that answered, in order.
func (s *Session) UsedProviders() []Used {
	s.mu.Lock()
	defer s.mu.Unlock()
	return slices.Clone(s.used)
}

// LastCallFailed reports whether the most recent Ask found no working AI.
func (s *Session) LastCallFailed() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.lastCallFailed
}

// HasRoute reports whether any API route or allowed CLI is still usable.
func (s *Session) HasRoute() bool {
	s.mu.Lock()
	for _, r := range s.Routes {
		if !s.failed[r.ID] {
			s.mu.Unlock()
			return true
		}
	}
	s.mu.Unlock()
	return len(s.cliRoutes()) > 0
}

type sessionKey struct{}

// WithSession attaches a session to ctx.
func WithSession(ctx context.Context, s *Session) context.Context {
	return context.WithValue(ctx, sessionKey{}, s)
}

// SessionFrom returns the session attached to ctx, or nil.
func SessionFrom(ctx context.Context) *Session {
	s, _ := ctx.Value(sessionKey{}).(*Session)
	return s
}

// CLIOrder is the order subscription CLIs are tried in. Claude Code comes
// first: it is the subscription this workspace's owner has.
var CLIOrder = []string{"claude", "codex"}

var cliModels = map[string]string{"claude": "sonnet", "codex": "codex-default"}

// CLIRoutes lists installed subscription CLIs the session allows.
func CLIRoutes(ctx context.Context) []Route {
	if s := SessionFrom(ctx); s != nil {
		return s.cliRoutes()
	}
	return (&Session{}).cliRoutes()
}

func (s *Session) cliRoutes() []Route {
	var routes []Route
	for _, name := range CLIOrder {
		if s.CLINames != nil && !slices.Contains(s.CLINames, name) {
			continue
		}
		if Installed(name) {
			routes = append(routes, Route{ID: "cli:" + name, Provider: name, Model: cliModels[name], CLI: name})
		}
	}
	return routes
}

// ── route health: failed routes cool down across requests ──

const routeCooldown = 60 * time.Second

type health struct {
	until   time.Time
	success time.Time
}

var (
	healthMu sync.Mutex
	healths  = map[string]health{}
)

func healthKey(r Route, s *Session) string {
	return cacheKey([]any{s.CacheDir, r.Provider, r.Model, r.Key, r.CLI})
}

func routeHealth(r Route, s *Session) health {
	healthMu.Lock()
	defer healthMu.Unlock()
	return healths[healthKey(r, s)]
}

func noteHealth(r Route, s *Session, ok bool) {
	healthMu.Lock()
	defer healthMu.Unlock()
	if len(healths) > 256 {
		clear(healths)
	}
	key := healthKey(r, s)
	h := healths[key]
	if ok {
		healths[key] = health{success: time.Now()}
	} else {
		healths[key] = health{until: time.Now().Add(routeCooldown), success: h.success}
	}
}

// ── cache ──

// cacheKey hashes everything that can change the answer. Leaving anything
// out gives stale answers that look correct, far worse than a cache miss.
// encoding/json writes map keys sorted, so equal payloads hash equally.
func cacheKey(payload any) string {
	data, _ := json.Marshal(payload)
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func cacheRead(dir, key string) (any, bool) {
	data, err := os.ReadFile(filepath.Join(dir, key+".json"))
	if err != nil {
		return nil, false
	}
	var entry struct {
		Response json.RawMessage `json:"response"`
	}
	if json.Unmarshal(data, &entry) != nil || entry.Response == nil {
		return nil, false
	}
	var value any
	if json.Unmarshal(entry.Response, &value) != nil {
		return nil, false
	}
	return value, true
}

func cacheWrite(dir, key string, request, response any) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	// The request is kept for debugging. It never contains a key: keys travel
	// in headers, not in the body. Prompts are private, so the file is 0600.
	data, err := json.MarshalIndent(map[string]any{"request": request, "response": response}, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, "."+key+".*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), filepath.Join(dir, key+".json"))
}

// ── the call ──

// Request is one structured question.
type Request struct {
	Prompt      string
	Schema      Schema
	Model       string // OpenAI model for environment keys; DefaultModel if empty.
	System      string
	Temperature float64
	SchemaName  string // Default "response".
	NoCache     bool
	Timeout     time.Duration // Per API call; default 60s.
}

type payload struct {
	Version     int     `json:"version"`
	Provider    string  `json:"provider"`
	Model       string  `json:"model"`
	Prompt      string  `json:"prompt"`
	System      *string `json:"system"`
	Schema      Schema  `json:"schema"`
	Temperature float64 `json:"temperature"`
	SchemaName  string  `json:"schema_name"`
}

// Ask tries the session's API keys (preferred first), then installed CLIs,
// and returns the first valid answer and whether it came from the cache.
// Errors never contain keys.
func Ask(ctx context.Context, req Request) (map[string]any, bool, error) {
	s := SessionFrom(ctx)
	if s == nil {
		s = NewSession(nil, "", nil)
	}
	if req.SchemaName == "" {
		req.SchemaName = "response"
	}
	if req.Timeout == 0 {
		req.Timeout = 60 * time.Second
	}

	s.mu.Lock()
	var routes []Route
	for _, r := range s.Routes {
		if r.Provider != "zai" {
			routes = append(routes, r)
		}
	}
	preferred := s.preferred
	s.mu.Unlock()
	slices.SortStableFunc(routes, func(a, b Route) int { return boolOrder(a.ID != preferred, b.ID != preferred) })
	cli := s.cliRoutes()
	if len(routes) == 0 && len(cli) == 0 {
		return nil, false, &Error{Message: "No API keys configured. Add a key in Settings or use rules mode.", Auth: true}
	}
	candidates := append(slices.Clone(routes), cli...)

	strict := Strictify(req.Schema)
	payloads := map[string]payload{}
	for _, r := range candidates {
		p := payload{Version: 2, Provider: r.Provider, Model: r.Model, Prompt: req.Prompt, Schema: strict,
			Temperature: req.Temperature, SchemaName: req.SchemaName}
		if r.CLI != "" {
			p.Version = 3
		}
		if req.System != "" {
			p.System = &req.System
		}
		payloads[r.ID] = p
	}

	checked := func(value any, r Route) (map[string]any, error) {
		if err := ValidateOutput(value, strict); err != nil {
			return nil, err
		}
		answer, ok := value.(map[string]any)
		if !ok {
			return nil, newError("Provider response has an invalid value type.")
		}
		encoded, _ := json.Marshal(answer)
		for _, other := range s.Routes {
			if other.Key != "" && strings.Contains(string(encoded), other.Key) {
				return nil, newError("Provider response contained sensitive data and was discarded.")
			}
		}
		return answer, nil
	}
	used := func(r Route) {
		s.mu.Lock()
		defer s.mu.Unlock()
		s.lastCallFailed = false
		s.preferred = r.ID
		u := Used{r.Provider, r.Model}
		if !slices.Contains(s.used, u) {
			s.used = append(s.used, u)
		}
	}

	// Check every route's cache before calling anyone: a warm answer from a
	// fallback must not wait behind a cold or failing first provider.
	if !req.NoCache && s.CacheDir != "" {
		for _, r := range candidates {
			value, ok := cacheRead(s.CacheDir, cacheKey(payloads[r.ID]))
			if !ok {
				continue
			}
			answer, err := checked(value, r)
			if err != nil {
				continue
			}
			usage.RecordCache(ctx, r.Provider, r.Model)
			used(r)
			return answer, true, nil
		}
	}

	// Keep the saved key order, but try the CLI that last worked first.
	slices.SortStableFunc(cli, func(a, b Route) int {
		return routeHealth(b, s).success.Compare(routeHealth(a, s).success)
	})
	deadline := time.Now().Add(120 * time.Second)
	for _, r := range append(routes, cli...) {
		s.mu.Lock()
		failed := s.failed[r.ID]
		s.mu.Unlock()
		if failed || routeHealth(r, s).until.After(time.Now()) {
			continue
		}
		if time.Now().After(deadline) || ctx.Err() != nil {
			break
		}
		var raw any
		var err error
		if r.CLI != "" {
			prompt := req.Prompt
			if req.System != "" {
				prompt = "SYSTEM INSTRUCTIONS\n" + req.System + "\n\nUNTRUSTED INPUT DATA\n" + req.Prompt
			}
			raw, err = askCLI(ctx, r.CLI, prompt, strict)
		} else {
			timeout := min(req.Timeout, 30*time.Second, time.Until(deadline))
			raw, err = call(ctx, r, req, max(timeout, time.Second))
		}
		var answer map[string]any
		if err == nil {
			answer, err = checked(raw, r)
		}
		if err != nil {
			noteHealth(r, s, false)
			s.mu.Lock()
			s.failed[r.ID] = true
			s.events = append(s.events, r.Provider)
			s.mu.Unlock()
			continue
		}
		noteHealth(r, s, true)
		used(r)
		if !req.NoCache && s.CacheDir != "" {
			// Never discard a successful answer because caching failed.
			_ = cacheWrite(s.CacheDir, cacheKey(payloads[r.ID]), payloads[r.ID], answer)
		}
		return answer, false, nil
	}
	s.mu.Lock()
	s.lastCallFailed = true
	s.mu.Unlock()
	return nil, false, newError("Configured AI providers are unavailable or cooling down. Using rules mode.")
}

// askCLI runs a subscription CLI in an empty temporary folder.
func askCLI(ctx context.Context, name, prompt string, schema Schema) (any, error) {
	dir, err := os.MkdirTemp("", "jhalcv-tailor-")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(dir)
	answer, err := RunCLI(ctx, name, prompt, schema, dir)
	if err != nil {
		msg := err.Error()
		if len(msg) > 160 {
			msg = msg[:160]
		}
		return nil, newError(name + " CLI could not answer: " + msg)
	}
	return answer, nil
}

// boolOrder sorts false before true.
func boolOrder(a, b bool) int {
	switch {
	case a == b:
		return 0
	case !a:
		return -1
	}
	return 1
}
