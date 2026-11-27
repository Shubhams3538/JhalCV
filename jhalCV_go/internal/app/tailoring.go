package app

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"time"

	"jhalcv/internal/credentials"
	"jhalcv/internal/llm"
	"jhalcv/internal/resume"
	"jhalcv/internal/storage"
	"jhalcv/internal/timing"
	"jhalcv/internal/usage"
	"jhalcv/internal/usererr"
)

const (
	draftLifetime = 2 * time.Hour
	maxDrafts     = 10
)

// providerNames are shown in notices about which AI answered.
var providerNames = map[string]string{
	"codex": "Codex", "claude": "Claude Code", "openai": "OpenAI", "gemini": "Gemini",
	"anthropic": "Anthropic", "openrouter": "OpenRouter",
}

// ProviderName is the display name of a provider or CLI.
func ProviderName(id string) string {
	if name, ok := providerNames[id]; ok {
		return name
	}
	if p, ok := credentials.Lookup(id); ok {
		return p.Name
	}
	return "another AI provider"
}

// drafts holds tailored drafts in creation order, for two hours at most.
type drafts struct {
	mu    sync.Mutex
	order []string
	byID  map[string]*resume.Draft
}

func (d *drafts) get(id any) (*resume.Draft, string, error) {
	key, _ := id.(string)
	d.mu.Lock()
	defer d.mu.Unlock()
	draft := d.byID[key]
	if draft == nil || time.Since(draft.Created) > draftLifetime {
		return nil, "", usererr.New("This draft expired. Create a new tailored draft.")
	}
	return draft, key, nil
}

func urlToken(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return base64.RawURLEncoding.EncodeToString(b)
}

// DraftProject is one project of a draft as the review page shows it.
type DraftProject struct {
	ID      string          `json:"id"`
	Name    string          `json:"name"`
	Bullets []string        `json:"bullets"`
	Budgets []resume.Budget `json:"budgets"`
}

// DraftResult is what the page gets back for a new draft.
type DraftResult struct {
	ID       string              `json:"id"`
	Notice   string              `json:"notice"`
	Gaps     any                 `json:"gaps"`
	Library  []map[string]string `json:"library"`
	Projects []DraftProject      `json:"projects"`
	Timings  *timing.Snapshot    `json:"timings,omitempty"`
}

func (a *App) remember(source, rewritten *resume.Data, jd string, report resume.Report, notice string, library *storage.Profile) *DraftResult {
	d := &a.drafts
	d.mu.Lock()
	if d.byID == nil {
		d.byID = map[string]*resume.Draft{}
	}
	d.order = slices.DeleteFunc(d.order, func(id string) bool {
		if time.Since(d.byID[id].Created) >= draftLifetime {
			delete(d.byID, id)
			return true
		}
		return false
	})
	for len(d.order) >= maxDrafts {
		delete(d.byID, d.order[0])
		d.order = d.order[1:]
	}
	id := urlToken(18)
	d.byID[id] = &resume.Draft{Created: time.Now(), Source: source.Clone(), Data: rewritten, JD: jd,
		Report: report, Notice: notice, Library: library}
	d.order = append(d.order, id)
	d.mu.Unlock()

	gaps := report["gap_skills"]
	if gaps == nil {
		gaps = []string{}
	}
	result := &DraftResult{ID: id, Notice: notice, Gaps: gaps, Library: []map[string]string{}, Projects: []DraftProject{}}
	for _, p := range resume.Renderable(library).Projects {
		result.Library = append(result.Library, map[string]string{"id": p.ID, "name": p.Name})
	}
	for i, p := range rewritten.Projects {
		dp := DraftProject{ID: p.ProjectID, Name: p.Name, Bullets: p.Bullets, Budgets: []resume.Budget{}}
		for _, b := range source.Projects[i].Bullets {
			dp.Budgets = append(dp.Budgets, resume.BudgetOf(b))
		}
		result.Projects = append(result.Projects, dp)
	}
	return result
}

// Selection returns the API routes and CLIs the user's chosen provider
// allows (".local/setup.json"; "auto" if not chosen). A nil CLI list allows
// every installed CLI; an empty one allows none.
func (a *App) Selection(provider *string) ([]llm.Route, []string, error) {
	choice := "auto"
	if provider != nil {
		choice = *provider
	} else if saved := a.SavedSetup(); saved["provider"] != nil {
		choice, _ = saved["provider"].(string)
	}
	settings, err := a.Store.Settings()
	if err != nil {
		return nil, nil, err
	}
	routes, err := a.Credentials.Routes(settings.Model)
	if err != nil {
		return nil, nil, err
	}
	switch choice {
	case "auto":
		return routes, nil, nil
	case "codex", "claude":
		return nil, []string{choice}, nil
	case "rules":
		return nil, []string{}, nil
	}
	if _, ok := credentials.Lookup(choice); ok && choice != "zai" {
		var chosen []llm.Route
		for _, r := range routes {
			if r.Provider == choice {
				chosen = append(chosen, r)
			}
		}
		return chosen, []string{}, nil
	}
	return nil, nil, usererr.New("Choose Codex, Claude Code, a supported API provider, or rules mode.")
}

// Scope attaches an AI session for the chosen provider to ctx.
func (a *App) Scope(ctx context.Context, provider *string) (context.Context, *llm.Session, error) {
	routes, cli, err := a.Selection(provider)
	if err != nil {
		return ctx, nil, err
	}
	session := llm.NewSession(routes, a.CacheDir(), cli)
	return llm.WithSession(ctx, session), session, nil
}

// SavedSetup is the onboarding choice in .local/setup.json, or empty.
func (a *App) SavedSetup() map[string]any {
	data, err := os.ReadFile(filepath.Join(a.Root, ".local", "setup.json"))
	if err != nil {
		return map[string]any{}
	}
	var out map[string]any
	if json.Unmarshal(data, &out) != nil || out == nil {
		return map[string]any{}
	}
	return out
}

// inferenceNotice tells the user when an AI provider failed during a run.
func inferenceNotice(s *llm.Session) string {
	if s == nil || s.Events() == 0 {
		return ""
	}
	if used := s.UsedProviders(); len(used) > 0 && !s.LastCallFailed() {
		return " An AI provider failed; automatically continued with " + ProviderName(used[len(used)-1].Provider) + "."
	}
	return " AI providers were unavailable; original wording and rules were used."
}

// Tailor handles POST /api/tailor: one tailored draft at a time.
func (a *App) Tailor(ctx context.Context, body map[string]any) (*DraftResult, error) {
	if !a.tailoring.TryLock() {
		return nil, usererr.New("A resume is already being tailored. Wait for it to finish.")
	}
	defer a.tailoring.Unlock()

	ctx, done := a.Timing.Run(ctx)
	ctx = usage.WithScope(ctx, a.Usage, "resume")
	end := timing.Begin(ctx, "profile", "Reading saved profile")
	a.Mu.Lock()
	profile, _, err := a.Store.Profile()
	var settings storage.Settings
	if err == nil {
		settings, err = a.Store.Settings()
	}
	var session *llm.Session
	if err == nil {
		ctx, session, err = a.Scope(ctx, nil)
	}
	a.Mu.Unlock()
	end(err == nil)
	if err != nil {
		done()
		return nil, err
	}

	// Inference works on a snapshot and never holds the main lock, so
	// progress polling and other tabs stay responsive.
	result, err := a.createDraft(ctx, body, profile, settings)
	if err != nil {
		done()
		return nil, err
	}
	suffix := inferenceNotice(session)
	draft, _, _ := a.drafts.get(result.ID)
	result.Notice += suffix
	draft.Notice += suffix
	usedList, _ := draft.Report["providers_used"].([]llm.Used)
	for _, u := range session.UsedProviders() {
		if !slices.Contains(usedList, u) {
			usedList = append(usedList, u)
		}
	}
	draft.Report["providers_used"] = orEmptyUsed(usedList)
	failovers, _ := draft.Report["key_failover_count"].(int)
	draft.Report["key_failover_count"] = failovers + session.Events()
	done()
	snapshot := a.Timing.Snapshot()
	result.Timings = &snapshot
	draft.Report["timings"] = snapshot
	return result, nil
}

func orEmptyUsed(u []llm.Used) []llm.Used {
	if u == nil {
		return []llm.Used{}
	}
	return u
}

func (a *App) createDraft(ctx context.Context, body map[string]any, profile *storage.Profile, settings storage.Settings) (*DraftResult, error) {
	jd, err := storage.Text(body["jd"], "Job description", 30000, true)
	if err != nil {
		return nil, err
	}
	if len([]rune(jd)) < 40 {
		return nil, usererr.New("Paste a job description of at least 40 characters.")
	}
	if !a.Store.HasProfile() {
		return nil, usererr.New("Save your own profile before creating a resume.")
	}
	library := resumeClone(profile)
	working := resumeClone(profile)
	// The browser uses the whole library; an explicit subset still works
	// for scripts.
	if chosen, ok := body["projects"]; ok {
		list, isList := chosen.([]any)
		valid := isList && len(list) > 0
		ids := map[string]bool{}
		for _, item := range list {
			id, isText := item.(string)
			if !isText || !slices.ContainsFunc(working.Projects, func(p storage.Project) bool { return p.ID == id }) {
				valid = false
			}
			ids[id] = true
		}
		if !valid {
			return nil, usererr.New("Choose at least one project from your library.")
		}
		working.Projects = slices.DeleteFunc(working.Projects, func(p storage.Project) bool { return !ids[p.ID] })
	}
	if len(resume.Renderable(working).Projects) == 0 {
		return nil, usererr.New("Add a project with at least two confirmed, shipped bullets before creating a draft.")
	}

	var source, rewritten *resume.Data
	var report resume.Report
	var notice string
	if settings.Tailorer == "rules" {
		end := timing.Begin(ctx, "ranking", "Ranking saved content")
		source, report = resume.Tailor(working, jd, 2, 4, 3)
		end(true)
		report["tailorer"] = "rules"
		rewritten = source.Clone()
		notice = "Using your original project wording. Projects and skills are ranked for the job."
	} else {
		source, report, rewritten, notice = resume.TailorOnce(ctx, working, jd, settings.Model, 3)
	}
	if report["tailorer"] == "rules" && settings.Tailorer == "auto" {
		notice += " Project selection used the rules fallback."
	}

	end := timing.Begin(ctx, "save", "Saving the tailored draft")
	defer end(true)
	a.Mu.Lock()
	defer a.Mu.Unlock()
	var asked []string
	asked = append(asked, stringsOf(report["matched_skills"])...)
	asked = append(asked, stringsOf(report["gap_skills"])...)
	if err := a.Insights.Record(jd, asked); err != nil {
		notice += " Skill insights could not be saved for this job."
	}
	return a.remember(source, rewritten, jd, report, notice, library), nil
}

func stringsOf(v any) []string {
	switch s := v.(type) {
	case []string:
		return s
	case []any:
		var out []string
		for _, item := range s {
			if str, ok := item.(string); ok {
				out = append(out, str)
			}
		}
		return out
	}
	return nil
}

func resumeClone(p *storage.Profile) *storage.Profile {
	data, _ := json.Marshal(p)
	out, err := storage.ParseProfile(data)
	if err != nil {
		var copy storage.Profile
		_ = json.Unmarshal(data, &copy)
		return &copy
	}
	return out
}

// SwapProject handles POST /api/drafts/swap: a saved project replaces one in
// the draft, in the same slot, with no AI and no reranking.
func (a *App) SwapProject(body map[string]any) (*DraftResult, error) {
	current, _, err := a.drafts.get(body["id"])
	if err != nil {
		return nil, err
	}
	source := current.Source
	projectID, _ := body["project"].(string)
	replacementID, isText := body["replacement"].(string)
	index := slices.IndexFunc(source.Projects, func(p resume.ProjectEntry) bool { return p.ProjectID == projectID })
	if index < 0 {
		return nil, usererr.New("Choose a project in this draft to replace.")
	}
	if !isText || slices.ContainsFunc(source.Projects, func(p resume.ProjectEntry) bool { return p.ProjectID == replacementID }) {
		return nil, usererr.New("Choose a replacement that is not already in this draft.")
	}
	var replacement *storage.Project
	for _, p := range resume.Renderable(current.Library).Projects {
		if p.ID == replacementID {
			replacement = &p
			break
		}
	}
	if replacement == nil {
		return nil, usererr.New("Choose a resume-ready project from the draft’s project library.")
	}
	reviewed, err := resume.EditedProjects(source, body["projects"])
	if err != nil {
		return nil, err
	}
	// Keep the slot's bullet quota and the library's bullet order.
	bullets := replacement.Bullets[:min(len(replacement.Bullets), len(source.Projects[index].Bullets))]
	entry := resume.ProjectEntry{ProjectID: replacementID, Name: replacement.Name, Stack: slices.Clone(replacement.Stack),
		Link: replacement.Link, Bullets: []string{}, BulletMeta: []resume.BulletMeta{}}
	if entry.Stack == nil {
		entry.Stack = []string{}
	}
	for _, b := range bullets {
		entry.Bullets = append(entry.Bullets, b.Text)
		entry.BulletMeta = append(entry.BulletMeta, resume.BulletMeta{ID: b.ID})
	}
	newSource := source.Clone()
	newSource.Projects[index] = entry
	reviewed.Projects[index] = entry
	report := cloneReport(current.Report)
	swaps, _ := report["project_swaps"].([]any)
	report["project_swaps"] = append(swaps, map[string]any{"removed": projectID, "added": replacementID})
	notice := "Swapped in saved project wording. Build a new PDF preview to review the layout."
	return a.remember(newSource, reviewed, current.JD, report, notice, current.Library), nil
}

// cloneReport copies a report, keeping typed values such as the skill plan.
func cloneReport(r resume.Report) resume.Report {
	out := resume.Report{}
	for k, v := range r {
		out[k] = v
	}
	return out
}

// Preview handles POST /api/preview.
func (a *App) Preview(ctx context.Context, body map[string]any) (*resume.PreviewResult, error) {
	draft, id, err := a.drafts.get(body["id"])
	if err != nil {
		return nil, err
	}
	if missing := MissingTools(); len(missing) > 0 {
		return nil, usererr.New("Install the PDF tools first: " + strings.Join(missing, ", ") + ". See README setup.")
	}
	result, err := a.Renderer.BuildPreview(ctx, draft, body["projects"], id)
	var renderErr *resume.RenderError
	if errors.As(err, &renderErr) {
		return nil, usererr.New("Could not compile the resume: " + renderErr.Message)
	}
	return result, err
}

// Export handles POST /api/export.
func (a *App) Export(body map[string]any) (*resume.ExportResult, error) {
	draft, _, err := a.drafts.get(body["id"])
	if err != nil {
		return nil, err
	}
	settings, err := a.Store.Settings()
	if err != nil {
		return nil, err
	}
	return resume.Export(draft, body["revision"], settings)
}

// PreviewPDF returns the bytes of a draft's current preview PDF.
func (a *App) PreviewPDF(id, revision string) ([]byte, error) {
	draft, _, err := a.drafts.get(id)
	if err != nil {
		return nil, err
	}
	if draft.Preview == nil || draft.Preview.Revision != revision {
		return nil, usererr.New("This preview is out of date. Build a new preview.")
	}
	return os.ReadFile(draft.Preview.Path)
}

// ImportProject handles POST /api/import: a GitHub README becomes an
// unconfirmed project draft.
func (a *App) ImportProject(ctx context.Context, body map[string]any) (*resume.ImportedProject, error) {
	jdValue, ok := body["jd"]
	if !ok {
		jdValue = ""
	}
	jd, err := storage.Text(jdValue, "Job description", 30000, false)
	if err != nil {
		return nil, err
	}
	evidence, err := resume.FetchRepository(ctx, a.Root, body["url"])
	if err != nil {
		return nil, err
	}
	settings, err := a.Store.Settings()
	if err != nil {
		return nil, err
	}
	ctx, session, err := a.Scope(usage.WithScope(ctx, a.Usage, "resume"), nil)
	if err != nil {
		return nil, err
	}
	result, err := resume.ImportProject(ctx, evidence, jd, settings)
	if err != nil {
		return nil, err
	}
	result.Notice += inferenceNotice(session)
	return result, nil
}

// ChooseFolder handles POST /api/folder.
func (a *App) ChooseFolder(ctx context.Context, body map[string]any) (map[string]any, error) {
	initial, ok := body["initial"]
	if !ok {
		settings, err := a.Store.Settings()
		if err != nil {
			return nil, err
		}
		initial = settings.OutputDir
	}
	path, err := storage.Text(initial, "Destination folder", 2000, true)
	if err != nil {
		return nil, err
	}
	if strings.HasPrefix(path, "~") {
		home, _ := os.UserHomeDir()
		path = filepath.Join(home, strings.TrimPrefix(path, "~"))
	}
	if !filepath.IsAbs(path) {
		path = filepath.Join(a.Root, path)
	}
	chosen, err := resume.ChooseFolder(ctx, path)
	if err != nil {
		return nil, err
	}
	if chosen == "" {
		return map[string]any{"path": nil}, nil
	}
	return map[string]any{"path": chosen}, nil
}

// InsightsSummary handles GET /api/insights.
func (a *App) InsightsSummary() (*resume.InsightSummary, error) {
	profile, _, err := a.Store.Profile()
	if err != nil {
		return nil, err
	}
	return a.Insights.Summary(profile)
}

// InsightAction handles POST /api/insights/{add,hide,unhide,reset}.
func (a *App) InsightAction(action string, body map[string]any) (map[string]any, error) {
	switch action {
	case "add":
		if !a.Store.HasProfile() {
			return nil, usererr.New("Save your own profile before adding skills.")
		}
		profile, _, err := a.Store.Profile()
		if err != nil {
			return nil, err
		}
		if err := resume.AddSkill(profile, body["skill"], body["group"]); err != nil {
			return nil, err
		}
		if err := a.Store.SaveProfile(profile); err != nil {
			return nil, err
		}
	case "hide":
		skill, err := storage.Text(body["skill"], "Skill", 100, true)
		if err != nil {
			return nil, err
		}
		if err := a.Insights.Hide(skill); err != nil {
			return nil, err
		}
	case "unhide":
		if err := a.Insights.UnhideAll(); err != nil {
			return nil, err
		}
	case "reset":
		if err := a.Insights.Reset(); err != nil {
			return nil, err
		}
	default:
		return nil, usererr.New("Unknown insights action.")
	}
	profile, _, err := a.Store.Profile()
	if err != nil {
		return nil, err
	}
	summary, err := a.Insights.Summary(profile)
	if err != nil {
		return nil, err
	}
	return map[string]any{"profile": profile, "insights": summary, "revision": a.ProfileRevision()}, nil
}
