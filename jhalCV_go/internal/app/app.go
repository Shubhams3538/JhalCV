// Package app holds the workspace behind the browser API, like the App class
// in resume/web.py. The web package turns HTTP requests into calls on App.
package app

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"sync"

	"jhalcv/internal/credentials"
	"jhalcv/internal/jobs"
	"jhalcv/internal/llm"
	"jhalcv/internal/resume"
	"jhalcv/internal/storage"
	"jhalcv/internal/timing"
	"jhalcv/internal/usage"
	"jhalcv/internal/usererr"
)

// App is one running workspace.
type App struct {
	Root        string
	Store       *storage.Store
	Credentials *credentials.Store
	Usage       *usage.Store
	Timing      *timing.Tailoring
	Insights    *resume.Insights
	Renderer    *resume.Renderer
	Jobs        *jobs.Service // The saved job collection.
	JobsTest    *jobs.Service // The Test lab's scratch copy.

	drafts    drafts
	setup     setupChecks
	tailoring sync.Mutex // One tailoring run at a time.

	// Mu serializes changes to saved files, like the Python app's lock.
	Mu    sync.Mutex
	token string
}

// New prepares the workspace in the project folder root.
func New(root string) (*App, error) {
	ledger, err := usage.Open(root)
	if err != nil {
		return nil, err
	}
	llm.ProjectRoot = root
	a := &App{
		Root:        root,
		Store:       storage.NewStore(root),
		Credentials: credentials.New(root, true),
		Usage:       ledger,
		Timing:      timing.New(),
		Insights:    resume.NewInsights(root),
		Renderer:    resume.NewRenderer(root),
		token:       rand.Text(),
	}
	if a.Jobs, err = jobs.NewService(root, "jobs.db", ledger, a.newAsker); err != nil {
		ledger.Close()
		return nil, err
	}
	if a.JobsTest, err = jobs.NewService(root, "jobs.test.db", ledger, a.newAsker); err != nil {
		a.Jobs.Store.Close()
		ledger.Close()
		return nil, err
	}
	return a, nil
}

// Close releases the databases and stops any pooled browsers.
func (a *App) Close() error {
	jobs.CloseBrowsers()
	a.Jobs.Store.Close()
	a.JobsTest.Store.Close()
	return a.Usage.Close()
}

// Token is the secret the page must send with every action.
func (a *App) Token() string { return a.token }

// CacheDir is where AI answers for resumes are cached.
func (a *App) CacheDir() string { return filepath.Join(a.Root, ".cache", "llm") }

// State is everything the page needs when it first loads (GET /api/state).
type State struct {
	Profile         *storage.Profile    `json:"profile"`
	Settings        storage.Settings    `json:"settings"`
	Example         bool                `json:"example"`
	AIAvailable     bool                `json:"ai_available"`
	Credentials     *credentials.Public `json:"credentials"`
	ProfileRevision string              `json:"profile_revision"`
	Dependencies    map[string]bool     `json:"dependencies"`
	Setup           *SetupStatus        `json:"setup"`
	Token           string              `json:"token"`
}

// State gathers the page's starting data.
func (a *App) State() (*State, error) {
	// Read the file once, so the profile and its revision always match.
	data, example, err := a.Store.ProfileFile()
	if err != nil {
		return nil, err
	}
	profile, err := storage.ParseProfile(data)
	if err != nil {
		return nil, err
	}
	settings, err := a.Store.Settings()
	if err != nil {
		return nil, err
	}
	public, err := a.Credentials.Public(settings.Model)
	if err != nil {
		return nil, err
	}
	routes, err := a.Credentials.Routes(settings.Model)
	if err != nil {
		return nil, err
	}

	rev := ""
	if !example {
		rev = revision(data)
	}
	setup, err := a.SetupStatus(context.Background())
	if err != nil {
		return nil, err
	}
	return &State{
		Setup:           setup,
		Profile:         profile,
		Settings:        settings,
		Example:         example,
		AIAvailable:     len(routes) > 0 || len(llm.CLIRoutes(context.Background())) > 0,
		Credentials:     public,
		ProfileRevision: rev,
		Dependencies:    Dependencies(),
		Token:           a.token,
	}, nil
}

// ProfileRevision is the fingerprint of the saved profile file ("" if none).
func (a *App) ProfileRevision() string {
	data, err := os.ReadFile(a.Store.ProfilePath())
	if err != nil {
		return ""
	}
	return revision(data)
}

// CheckRevision refuses an edit made from a tab that loaded an older profile.
func (a *App) CheckRevision(rev any) error {
	if s, ok := rev.(string); !ok || s != a.ProfileRevision() {
		return usererr.New("Your profile was changed outside this tab. Reload the page to get the latest version, then redo this edit.")
	}
	return nil
}

// SaveProfile handles POST /api/profile. A tab sends the revision it loaded,
// so an old tab cannot silently overwrite newer edits. A bare profile is still
// accepted for scripts.
func (a *App) SaveProfile(raw json.RawMessage) (map[string]any, error) {
	var wrapped map[string]json.RawMessage
	if err := json.Unmarshal(raw, &wrapped); err != nil {
		return nil, usererr.New("Expected a JSON object.")
	}
	if len(wrapped) == 2 && wrapped["profile"] != nil && wrapped["revision"] != nil {
		var rev any
		_ = json.Unmarshal(wrapped["revision"], &rev)
		if err := a.CheckRevision(rev); err != nil {
			return nil, err
		}
		raw = wrapped["profile"]
	}
	profile, err := storage.ParseProfile(raw)
	if err != nil {
		return nil, err
	}
	if err := a.Store.SaveProfile(profile); err != nil {
		return nil, err
	}
	return map[string]any{"saved": true, "revision": a.ProfileRevision()}, nil
}

// RemoveProject handles POST /api/projects/remove.
func (a *App) RemoveProject(body map[string]any) (map[string]any, error) {
	id, err := storage.Text(body["id"], "Project ID", 500, true)
	if err != nil {
		return nil, err
	}
	id = body["id"].(string) // Match by the exact ID, not the trimmed one.
	if rev, ok := body["revision"]; ok {
		if err := a.CheckRevision(rev); err != nil {
			return nil, err
		}
	}
	if !a.Store.HasProfile() {
		return map[string]any{"removed": false}, nil
	}
	profile, _, err := a.Store.Profile()
	if err != nil {
		return nil, err
	}
	kept := profile.Projects[:0:0]
	for _, p := range profile.Projects {
		if p.ID != id {
			kept = append(kept, p)
		}
	}
	if len(kept) == len(profile.Projects) {
		return map[string]any{"removed": false}, nil
	}
	profile.Projects = kept
	if err := a.Store.SaveProfile(profile); err != nil {
		return nil, err
	}
	return map[string]any{"removed": true, "revision": a.ProfileRevision()}, nil
}

// KeyAction handles POST /api/keys/{add,remove,prefer,model,reminder} and
// returns the updated public key list.
func (a *App) KeyAction(action string, body map[string]any) (*credentials.Public, error) {
	var err error
	switch action {
	case "add":
		err = a.Credentials.Add(body)
	case "remove":
		err = a.Credentials.Remove(body["id"])
	case "prefer":
		err = a.Credentials.Prefer(body["id"])
	case "model":
		err = a.Credentials.SetModel(body["id"], body["model"])
	case "reminder":
		err = a.Credentials.Reminder(body["dismissed"])
	default:
		err = usererr.New("Unknown API-key action.")
	}
	if err != nil {
		return nil, err
	}
	settings, err := a.Store.Settings()
	if err != nil {
		return nil, err
	}
	return a.Credentials.Public(settings.Model)
}

// revision is a short fingerprint of the saved profile file. The page sends it
// back with each edit, so an edit made in another tab is never overwritten.
func revision(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])[:16]
}

// PDFTools are the outside programs that build and check the resume PDF.
var PDFTools = []string{"typst", "pdfinfo", "pdftotext"}

// Dependencies reports which PDF tools are installed.
func Dependencies() map[string]bool {
	found := make(map[string]bool, len(PDFTools))
	for _, name := range PDFTools {
		_, err := exec.LookPath(name)
		found[name] = err == nil
	}
	return found
}

// MissingTools lists PDF tools that are not installed.
func MissingTools() []string {
	var missing []string
	for _, name := range PDFTools {
		if _, err := exec.LookPath(name); err != nil {
			missing = append(missing, name)
		}
	}
	return missing
}
