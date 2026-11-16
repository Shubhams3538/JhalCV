// Package credentials keeps the user's AI API keys in .local/credentials.json
// and turns them, plus any keys in the environment or .env, into routes the
// llm package can try. Keys never leave this machine: what the page sees
// holds only a fixed mask. This is the Go version of resume/credentials.py.
package credentials

import (
	"bufio"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync"

	"jhalcv/internal/llm"
	"jhalcv/internal/storage"
	"jhalcv/internal/usererr"
)

// Mask is shown instead of every key.
const Mask = "••••••••"

// Model is one selectable model for a provider.
type Model struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

// Provider is an AI API the app can call with a key.
type Provider struct {
	ID           string  `json:"id"`
	Name         string  `json:"name"`
	DefaultModel string  `json:"default_model"`
	Env          string  `json:"-"`
	Models       []Model `json:"models"`
}

// Providers are listed in the order the Settings page shows them.
var Providers = []Provider{
	{"openai", "OpenAI", "gpt-5.4-mini", "OPENAI_API_KEY", []Model{
		{"gpt-5.4-mini", "GPT-5.4 Mini"}, {"gpt-5.5", "GPT-5.5"}, {"gpt-4o-mini", "GPT-4o Mini (legacy)"}}},
	{"gemini", "Gemini", "gemini-3.8-flash", "GEMINI_API_KEY", []Model{
		{"gemini-3.8-flash", "Gemini 3.8 Flash"}, {"gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite"}}},
	{"anthropic", "Anthropic", "claude-sonnet-5-5", "ANTHROPIC_API_KEY", []Model{
		{"claude-sonnet-5-5", "Claude Sonnet 5.5"}, {"claude-haiku-5-5", "Claude Haiku 5.5"}, {"claude-opus-5-5", "Claude Opus 5.5"}}},
	{"openrouter", "OpenRouter", "openai/gpt-5.4-mini", "OPENROUTER_API_KEY", []Model{
		{"openai/gpt-5.4-mini", "OpenAI GPT-5.4 Mini"}, {"openai/gpt-5.5", "OpenAI GPT-5.5"},
		{"anthropic/claude-sonnet-5.5", "Claude Sonnet 5.5"}, {"openai/gpt-4o-mini", "OpenAI GPT-4o Mini (legacy)"}}},
	// The Z.AI GLM Coding Plan, through the Anthropic-compatible endpoint ZCode uses.
	{"zai", "Z.AI Coding Plan (GLM)", "GLM-5.3-Flash", "ZAI_API_KEY", []Model{
		{"GLM-5.3-Flash", "GLM-5.3 Flash"}, {"GLM-5.3", "GLM-5.3"}}},
}

// Lookup finds a provider by ID.
func Lookup(id string) (Provider, bool) {
	for _, p := range Providers {
		if p.ID == id {
			return p, true
		}
	}
	return Provider{}, false
}

// ValidateKey checks a pasted API key.
func ValidateKey(value any) (string, error) {
	s, ok := value.(string)
	if !ok {
		return "", usererr.New("Enter an API key.")
	}
	key := strings.TrimSpace(s)
	if len(key) < 8 || len(key) > 1024 || strings.IndexFunc(key, func(r rune) bool { return r < 33 || r > 126 }) >= 0 {
		return "", usererr.New("Enter a valid API key without spaces or line breaks.")
	}
	switch strings.ToLower(key) {
	case "your-api-key", "your_api_key", "sk-placeholder":
		return "", usererr.New("Enter the actual API key, not a placeholder.")
	}
	if strings.Contains(key, "...") || strings.Contains(key, "***") {
		return "", usererr.New("Enter the actual API key, not a placeholder.")
	}
	return key, nil
}

var modelPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:/-]*$`)

// ValidateModel checks a provider model ID.
func ValidateModel(value any) (string, error) {
	model, err := storage.Text(value, "Model ID", 150, true)
	if err != nil {
		return "", err
	}
	if !modelPattern.MatchString(model) || strings.Contains(model, "..") || strings.Contains(model, "://") {
		return "", usererr.New("Enter the provider model ID, not a URL.")
	}
	return model, nil
}

// savedKey is one key entry in credentials.json.
type savedKey struct {
	ID       string `json:"id"`
	Provider string `json:"provider"`
	Label    string `json:"label"`
	Model    string `json:"model"`
	Key      string `json:"key"`
}

type file struct {
	Keys            []savedKey        `json:"keys"`
	DisabledEnv     []string          `json:"disabled_env"`
	Preferred       *string           `json:"preferred"`
	DismissReminder bool              `json:"dismiss_reminder"`
	EnvModels       map[string]string `json:"env_models,omitempty"`
}

// Store reads and writes one workspace's credentials.
type Store struct {
	root       string
	includeEnv bool
	mu         sync.Mutex
}

// New returns the store for root. With includeEnv, keys from the process
// environment and root/.env are offered too.
func New(root string, includeEnv bool) *Store {
	return &Store{root: root, includeEnv: includeEnv}
}

func (s *Store) path() string {
	return filepath.Join(s.root, ".local", "credentials.json")
}

var errUnreadable = usererr.New("Could not read local API-key settings. Check .local/credentials.json.")

func (s *Store) read() (*file, error) {
	data, err := os.ReadFile(s.path())
	if errors.Is(err, fs.ErrNotExist) {
		return &file{Keys: []savedKey{}, DisabledEnv: []string{}}, nil
	}
	if err != nil {
		return nil, errUnreadable
	}
	var f file
	var shape map[string]json.RawMessage
	if json.Unmarshal(data, &shape) != nil || json.Unmarshal(data, &f) != nil || f.Keys == nil {
		return nil, errUnreadable
	}
	return &f, nil
}

func (s *Store) write(f *file) error {
	return storage.WriteJSON(s.path(), f) // Written 0600.
}

// Routes returns every usable key: saved keys first, then environment keys,
// with the preferred one moved to the front. openaiModel, when set, is the
// model for an OpenAI environment key.
func (s *Store) Routes(openaiModel string) ([]llm.Route, error) {
	saved, err := s.read()
	if err != nil {
		return nil, err
	}
	var routes []llm.Route
	for _, item := range saved.Keys {
		if _, ok := Lookup(item.Provider); !ok {
			continue
		}
		routes = append(routes, llm.Route{ID: item.ID, Provider: item.Provider, Label: item.Label,
			Model: item.Model, Key: item.Key, Source: "saved"})
	}
	if s.includeEnv {
		LoadEnv(filepath.Join(s.root, ".env"))
		for _, p := range Providers {
			id := "env:" + p.ID
			if slices.Contains(saved.DisabledEnv, id) {
				continue
			}
			key, err := ValidateKey(os.Getenv(p.Env))
			if err != nil || slices.ContainsFunc(routes, func(r llm.Route) bool { return r.Provider == p.ID && same(r.Key, key) }) {
				continue
			}
			model := saved.EnvModels[id]
			if model == "" {
				model = p.DefaultModel
				if p.ID == "openai" && openaiModel != "" {
					model = openaiModel
				}
			}
			routes = append(routes, llm.Route{ID: id, Provider: p.ID, Label: p.Name + " environment key",
				Model: model, Key: key, Source: "environment"})
		}
	}
	// A key pasted into a display field must never be shown back.
	for i := range routes {
		p, _ := Lookup(routes[i].Provider)
		for _, other := range routes {
			if strings.Contains(routes[i].Label, other.Key) {
				routes[i].Label = p.Name + " key"
			}
			if strings.Contains(routes[i].Model, other.Key) {
				routes[i].Model = p.DefaultModel
			}
		}
	}
	preferred := ""
	if saved.Preferred != nil {
		preferred = *saved.Preferred
	}
	slices.SortStableFunc(routes, func(a, b llm.Route) int {
		return order(a.ID != preferred, b.ID != preferred)
	})
	return routes, nil
}

func order(a, b bool) int {
	switch {
	case a == b:
		return 0
	case !a:
		return -1
	}
	return 1
}

func same(a, b string) bool {
	return subtle.ConstantTimeCompare([]byte(a), []byte(b)) == 1
}

// PublicKey is a key entry as the page sees it: never the key itself.
type PublicKey struct {
	ID       string `json:"id"`
	Provider string `json:"provider"`
	Label    string `json:"label"`
	Model    string `json:"model"`
	Mask     string `json:"mask"`
	Source   string `json:"source"`
	First    bool   `json:"first"`
}

// Public is what the Settings page shows about keys.
type Public struct {
	Providers         []Provider  `json:"providers"`
	Keys              []PublicKey `json:"keys"`
	ReminderDismissed bool        `json:"reminder_dismissed"`
	ShowReminder      bool        `json:"show_reminder"`
}

// Public describes the keys without revealing them.
func (s *Store) Public(openaiModel string) (*Public, error) {
	saved, err := s.read()
	if err != nil {
		return nil, err
	}
	routes, err := s.Routes(openaiModel)
	if err != nil {
		return nil, err
	}
	out := &Public{Providers: Providers, Keys: []PublicKey{}, ReminderDismissed: saved.DismissReminder,
		ShowReminder: len(routes) == 0 && !saved.DismissReminder}
	for i, r := range routes {
		out.Keys = append(out.Keys, PublicKey{r.ID, r.Provider, r.Label, r.Model, Mask, r.Source, i == 0})
	}
	return out, nil
}

// Add saves a new key from a Settings form body.
func (s *Store) Add(body map[string]any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	provider, ok := Lookup(str(body["provider"]))
	if !ok {
		return usererr.New("Choose a supported AI provider.")
	}
	key, err := ValidateKey(body["key"])
	if err != nil {
		return err
	}
	modelValue := body["model"]
	if str(modelValue) == "" {
		modelValue = provider.DefaultModel
	}
	model, err := ValidateModel(modelValue)
	if err != nil {
		return err
	}
	labelValue := body["label"]
	if str(labelValue) == "" {
		labelValue = provider.Name + " key"
	}
	label, err := storage.Text(labelValue, "Key label", 80, true)
	if err != nil {
		return err
	}
	routes, err := s.Routes("")
	if err != nil {
		return err
	}
	// Even a key accidentally pasted into a display field must never turn
	// public metadata into a way to read the secret back.
	for _, secret := range append(keysOf(routes), key) {
		if strings.Contains(label, secret) || strings.Contains(model, secret) {
			return usererr.New("Keep API keys out of labels and model IDs.")
		}
	}
	saved, err := s.read()
	if err != nil {
		return err
	}
	if len(saved.Keys) >= 20 {
		return usererr.New("You can save up to 20 API keys. Remove an unused key first.")
	}
	if slices.ContainsFunc(routes, func(r llm.Route) bool { return r.Provider == provider.ID && same(r.Key, key) }) {
		return usererr.New("That key is already configured for this provider.")
	}
	// The new secret may have been typed into another entry's display text.
	for i := range saved.Keys {
		p, _ := Lookup(saved.Keys[i].Provider)
		if strings.Contains(saved.Keys[i].Label, key) {
			saved.Keys[i].Label = p.Name + " key"
		}
		if strings.Contains(saved.Keys[i].Model, key) {
			saved.Keys[i].Model = p.DefaultModel
		}
	}
	saved.Keys = append(saved.Keys, savedKey{ID: randomHex(12), Provider: provider.ID, Label: label, Model: model, Key: key})
	return s.write(saved)
}

// Remove deletes a saved key, or hides an environment key.
func (s *Store) Remove(idValue any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	id, err := storage.Text(idValue, "Key ID", 100, true)
	if err != nil {
		return err
	}
	saved, err := s.read()
	if err != nil {
		return err
	}
	if rest, ok := strings.CutPrefix(id, "env:"); ok && isProvider(rest) {
		if !slices.Contains(saved.DisabledEnv, id) {
			saved.DisabledEnv = append(saved.DisabledEnv, id)
		}
	} else {
		saved.Keys = slices.DeleteFunc(saved.Keys, func(k savedKey) bool { return k.ID == id })
	}
	if saved.Preferred != nil && *saved.Preferred == id {
		saved.Preferred = nil
	}
	return s.write(saved)
}

// Prefer makes one key the first one tried.
func (s *Store) Prefer(idValue any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	id, ok := idValue.(string)
	routes, err := s.Routes("")
	if err != nil {
		return err
	}
	if !ok || !slices.ContainsFunc(routes, func(r llm.Route) bool { return r.ID == id }) {
		return usererr.New("Choose an existing API-key entry.")
	}
	saved, err := s.read()
	if err != nil {
		return err
	}
	saved.Preferred = &id
	return s.write(saved)
}

// SetModel changes the model one key uses.
func (s *Store) SetModel(idValue, modelValue any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	id, ok := idValue.(string)
	routes, err := s.Routes("")
	if err != nil {
		return err
	}
	if !ok || !slices.ContainsFunc(routes, func(r llm.Route) bool { return r.ID == id }) {
		return usererr.New("Choose an existing API-key entry.")
	}
	model, err := ValidateModel(modelValue)
	if err != nil {
		return err
	}
	for _, key := range keysOf(routes) {
		if strings.Contains(model, key) {
			return usererr.New("Keep API keys out of model IDs.")
		}
	}
	saved, err := s.read()
	if err != nil {
		return err
	}
	if strings.HasPrefix(id, "env:") {
		if saved.EnvModels == nil {
			saved.EnvModels = map[string]string{}
		}
		saved.EnvModels[id] = model
	} else {
		for i := range saved.Keys {
			if saved.Keys[i].ID == id {
				saved.Keys[i].Model = model
			}
		}
	}
	return s.write(saved)
}

// Reminder turns the "add an API key" reminder on or off.
func (s *Store) Reminder(dismissed any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	value, ok := dismissed.(bool)
	if !ok {
		return usererr.New("Choose whether to show the API-key reminder.")
	}
	saved, err := s.read()
	if err != nil {
		return err
	}
	saved.DismissReminder = value
	return s.write(saved)
}

func keysOf(routes []llm.Route) []string {
	keys := make([]string, 0, len(routes))
	for _, r := range routes {
		keys = append(keys, r.Key)
	}
	return keys
}

func isProvider(id string) bool {
	_, ok := Lookup(id)
	return ok
}

func str(v any) string {
	s, _ := v.(string)
	return s
}

func randomHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

var envOnce sync.Map

// LoadEnv reads KEY=value lines from a .env file into the environment,
// without overwriting variables that are already set: an exported key always
// wins over a stale file. The file is made private, like credentials.json.
func LoadEnv(path string) {
	f, err := os.Open(path)
	if err != nil {
		return
	}
	defer f.Close()
	if _, done := envOnce.LoadOrStore(path, true); !done {
		_ = os.Chmod(path, 0o600)
	}
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") || !strings.Contains(line, "=") {
			continue
		}
		key, value, _ := strings.Cut(line, "=")
		key = strings.TrimSpace(key)
		value = strings.Trim(strings.Trim(strings.TrimSpace(value), `"`), "'")
		if _, set := os.LookupEnv(key); !set {
			os.Setenv(key, value)
		}
	}
}
