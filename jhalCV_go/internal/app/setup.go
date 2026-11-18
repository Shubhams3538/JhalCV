package app

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"jhalcv/internal/llm"
	"jhalcv/internal/onboarding"
	"jhalcv/internal/resume"
	"jhalcv/internal/storage"
	"jhalcv/internal/usage"
	"jhalcv/internal/usererr"
)

// setupChecks remember recent AI connection tests and the reviewed setup
// preview, so setup can only finish on what the user actually reviewed.
type setupChecks struct {
	mu       sync.Mutex
	aiTested map[string]time.Time
	receipts map[string]setupReceipt
}

type setupReceipt struct {
	signature string
	created   time.Time
	draft     string
}

func (c *setupChecks) testedAI(provider string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.aiTested == nil {
		c.aiTested = map[string]time.Time{}
	}
	c.aiTested[provider] = time.Now()
}

// SetupStatus is state.setup on the page.
type SetupStatus struct {
	Required    bool                 `json:"required"`
	Completed   bool                 `json:"completed"`
	Preferences map[string]any       `json:"preferences"`
	Profile     *storage.Profile     `json:"profile"`
	Experience  float64              `json:"experience"`
	Companies   []onboarding.Company `json:"companies"`
	Agents      []map[string]any     `json:"agents"`
}

// SetupStatus describes first-run setup for the page.
func (a *App) SetupStatus(ctx context.Context) (*SetupStatus, error) {
	config := a.SavedSetup()
	existing := a.Store.HasProfile()
	completed := truthy(config["completed"])
	status := &SetupStatus{Required: !existing && !completed, Completed: completed, Preferences: config,
		Profile: onboarding.EmptyProfile(), Companies: []onboarding.Company{}}
	if existing {
		profile, _, err := a.Store.Profile()
		if err != nil {
			return nil, err
		}
		status.Profile = profile
		exp, err := a.Jobs.ExperienceYears(ctx, profile)
		if err != nil {
			return nil, dbBusy(err, jobsBusy)
		}
		status.Experience = exp.Years
	}
	if existing || completed {
		sources, err := a.Jobs.Store.Sources(ctx)
		if err != nil {
			return nil, dbBusy(err, jobsBusy)
		}
		for _, s := range sources {
			status.Companies = append(status.Companies, onboarding.Company{Name: s.Name, URL: s.URL, Kind: s.Kind})
		}
	} else {
		data, err := os.ReadFile(filepath.Join(a.Root, "data", "companies.json"))
		if err != nil {
			return nil, err
		}
		if err := json.Unmarshal(data, &status.Companies); err != nil {
			return nil, err
		}
	}
	for _, agent := range [][2]string{{"codex", "codex"}, {"claude", "claude"}, {"gemini-cli", "gemini"}, {"antigravity", "agy"}} {
		status.Agents = append(status.Agents, map[string]any{"id": agent[0], "installed": llm.Installed(agent[1]),
			"supported": agent[0] == "codex" || agent[0] == "claude"})
	}
	return status, nil
}

func providerOf(body map[string]any) *string {
	if p, ok := body["provider"].(string); ok {
		return &p
	}
	if body["provider"] == nil {
		return nil
	}
	invalid := "\x00invalid"
	return &invalid
}

func providerKey(body map[string]any) string {
	if p := providerOf(body); p != nil {
		return *p
	}
	return ""
}

// StructureResume handles POST /api/setup/structure: AI sorts the imported
// text into profile fields, only when the user asks.
func (a *App) StructureResume(ctx context.Context, body map[string]any) (map[string]any, error) {
	ctx = usage.WithScope(ctx, a.Usage, "resume")
	ctx, _, err := a.Scope(ctx, providerOf(body))
	if err != nil {
		return nil, err
	}
	profile, err := onboarding.Structure(ctx, body["text"])
	if err != nil {
		return nil, err
	}
	a.setup.testedAI(providerKey(body))
	return map[string]any{"profile": profile, "review": onboarding.ReviewSummary(profile)}, nil
}

// SetupAction handles the other POST /api/setup/<action> requests.
func (a *App) SetupAction(ctx context.Context, action string, body map[string]any, raw []byte) (any, error) {
	switch action {
	case "extract":
		return onboarding.Extract(ctx, body)
	case "test-ai":
		ctx, _, err := a.Scope(usage.WithScope(ctx, a.Usage, "resume"), providerOf(body))
		if err != nil {
			return nil, err
		}
		_, _, err = llm.Ask(ctx, llm.Request{Prompt: "Return ready=true.", NoCache: true,
			Schema: llm.Object(map[string]any{"ready": llm.Schema{"type": "boolean", "enum": []any{true}}})})
		if err != nil {
			return nil, usererr.New("Connection failed. Sign in to the selected CLI or check the API key and quota, then retry. No resume was sent.")
		}
		a.setup.testedAI(providerKey(body))
		return map[string]any{"ready": true}, nil
	case "check", "complete":
		return a.setupCheck(ctx, action, body, raw)
	}
	return nil, usererr.New("Unknown setup action.")
}

func (a *App) setupCheck(ctx context.Context, action string, body map[string]any, raw []byte) (any, error) {
	if err := a.CheckRevision(body["revision"]); err != nil {
		return nil, err
	}
	profile, prefs, settings, companies, err := onboarding.Validate(body, raw, a.Store, func(provider string) error {
		_, _, err := a.Selection(&provider)
		return err
	})
	if err != nil {
		return nil, err
	}
	if a.Jobs.Running() || a.JobsTest.Running() {
		return nil, usererr.New("Wait for the job refresh to finish before changing setup.")
	}
	signature := onboarding.Fingerprint(profile, prefs, settings, companies)

	if action == "check" {
		a.setup.mu.Lock()
		tested, ok := a.setup.aiTested[prefs.Provider]
		a.setup.mu.Unlock()
		if prefs.Provider != "rules" && (!ok || time.Since(tested) > time.Hour) {
			return nil, usererr.New("Test your selected AI connection on step 2 before finishing setup.")
		}
		if missing := MissingTools(); len(missing) > 0 {
			return nil, usererr.New("Install the PDF tools before finishing setup: " + strings.Join(missing, ", "))
		}
		if err := os.MkdirAll(settings.OutputDir, 0o755); err != nil {
			return nil, err
		}
		probe, err := os.CreateTemp(settings.OutputDir, ".jhalcv-write-test-*")
		if err != nil {
			return nil, err
		}
		probe.Close()
		os.Remove(probe.Name())

		data, report := resume.Tailor(profile, prefs.Include, 2, 4, 3)
		result := a.remember(data, data.Clone(), "", report, "Setup preview uses saved wording without AI.", profile)
		draft, id, err := a.drafts.get(result.ID)
		if err != nil {
			return nil, err
		}
		var edits []any
		for _, p := range data.Projects {
			bullets := make([]any, len(p.Bullets))
			for i, b := range p.Bullets {
				bullets[i] = b
			}
			edits = append(edits, map[string]any{"bullets": bullets})
		}
		preview, err := a.Renderer.BuildPreview(ctx, draft, edits, id)
		if err != nil {
			return nil, err
		}
		receipt := urlToken(24)
		a.setup.mu.Lock()
		a.setup.receipts = map[string]setupReceipt{receipt: {signature, time.Now(), result.ID}}
		a.setup.mu.Unlock()
		return map[string]any{"revision": preview.Revision, "url": preview.URL, "checks": preview.Checks,
			"notices": preview.Notices, "pages": preview.Pages, "receipt": receipt}, nil
	}

	receiptID, _ := body["receipt"].(string)
	a.setup.mu.Lock()
	check, ok := a.setup.receipts[receiptID]
	a.setup.mu.Unlock()
	if !ok || check.signature != signature || time.Since(check.created) > time.Hour {
		return nil, usererr.New("Build and review a fresh setup PDF after your latest changes.")
	}
	if _, _, err := a.drafts.get(check.draft); err != nil {
		return nil, err
	}
	if body["confirmed"] != true {
		return nil, usererr.New("Confirm your details and review the PDF before finishing.")
	}
	if err := a.completeSetup(ctx, profile, prefs, settings, companies); err != nil {
		return nil, err
	}
	a.setup.mu.Lock()
	a.setup.receipts = nil
	a.setup.mu.Unlock()
	return a.State()
}

// completeSetup saves everything in one guarded step: the companies, the
// profile, the settings and the setup marker. If any write fails, the files
// are restored and the database transaction rolls back.
func (a *App) completeSetup(ctx context.Context, profile *storage.Profile, prefs *onboarding.Preferences, settings storage.Settings, companies []onboarding.Company) error {
	setupPath := filepath.Join(a.Root, ".local", "setup.json")
	paths := []string{a.Store.ProfilePath(), a.Store.SettingsPath(), setupPath}
	previous := map[string][]byte{}
	for _, p := range paths {
		data, err := os.ReadFile(p)
		if err == nil {
			previous[p] = data
		} else if !errors.Is(err, fs.ErrNotExist) {
			return err
		}
	}
	restore := func() {
		for _, p := range paths {
			if data, ok := previous[p]; ok {
				_ = storage.WriteFile(p, data)
			} else {
				_ = os.Remove(p)
			}
		}
	}
	tx, err := a.Jobs.Store.DB().BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	if err := a.saveSetup(tx, profile, prefs, settings, companies, setupPath); err != nil {
		_ = tx.Rollback()
		restore()
		return err
	}
	if err := tx.Commit(); err != nil {
		restore()
		return err
	}
	return nil
}

func (a *App) saveSetup(tx *sql.Tx, profile *storage.Profile, prefs *onboarding.Preferences, settings storage.Settings, companies []onboarding.Company, setupPath string) error {
	wanted := map[string]bool{}
	for _, c := range companies {
		wanted[c.URL] = true
	}
	rows, err := tx.Query("SELECT id, url FROM sources")
	if err != nil {
		return err
	}
	var remove []int64
	for rows.Next() {
		var id int64
		var link string
		if err := rows.Scan(&id, &link); err != nil {
			rows.Close()
			return err
		}
		if !wanted[link] {
			remove = append(remove, id)
		}
	}
	rows.Close()
	for _, id := range remove {
		if _, err := tx.Exec("DELETE FROM sources WHERE id=?", id); err != nil {
			return err
		}
	}
	for _, c := range companies {
		if _, err := tx.Exec("INSERT INTO sources(name,url,kind,include,exclude,locations,created_at) VALUES(?,?,?,?,?,?,?) "+
			"ON CONFLICT(url) DO UPDATE SET name=excluded.name,include=excluded.include,exclude=excluded.exclude,locations=excluded.locations",
			c.Name, c.URL, c.Kind, prefs.Include, prefs.Exclude, prefs.Locations, time.Now().Unix()); err != nil {
			return err
		}
	}
	if err := a.Store.SaveProfile(profile); err != nil {
		return err
	}
	if _, err := a.Store.SaveSettings(settings); err != nil {
		return err
	}
	years := strconv.FormatFloat(prefs.Years, 'f', -1, 64)
	if !strings.Contains(years, ".") {
		years += ".0" // Python's str(float).
	}
	if _, err := tx.Exec("INSERT OR REPLACE INTO meta(key,value) VALUES('experience_years',?)", years); err != nil {
		return err
	}
	if _, err := tx.Exec("UPDATE jobs SET min_years=NULL, eligible=NULL, reason=NULL, checked_by=NULL"); err != nil {
		return err
	}
	marker := map[string]any{"locations": prefs.Locations, "include": prefs.Include, "exclude": prefs.Exclude,
		"years": prefs.Years, "provider": prefs.Provider, "no_experience": prefs.NoExperience, "completed": true, "version": 1}
	return storage.WriteJSON(setupPath, marker)
}
