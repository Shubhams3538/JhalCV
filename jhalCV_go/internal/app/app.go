// Package app holds the workspace behind the browser API, like the App class
// in resume/web.py.
package app

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os/exec"

	"jhalcv/internal/storage"
)

// App is one running workspace: its files and the secret token the page must
// send with every action.
type App struct {
	store *storage.Store
	token string
}

// New prepares the workspace in the project folder root.
func New(root string) *App {
	return &App{
		store: storage.NewStore(root),
		token: rand.Text(),
	}
}

// State is everything the page needs when it first loads (GET /api/state).
type State struct {
	Profile         json.RawMessage  `json:"profile"`
	Settings        storage.Settings `json:"settings"`
	Example         bool             `json:"example"`
	ProfileRevision string           `json:"profile_revision"`
	Dependencies    map[string]bool  `json:"dependencies"`
	Token           string           `json:"token"`
}

// State gathers the page's starting data.
func (a *App) State() (State, error) {
	profile, example, err := a.store.ProfileJSON()
	if err != nil {
		return State{}, err
	}
	settings, err := a.store.Settings()
	if err != nil {
		return State{}, err
	}

	rev := ""
	if !example {
		rev = revision(profile)
	}

	return State{
		Profile:         profile,
		Settings:        settings,
		Example:         example,
		ProfileRevision: rev,
		Dependencies:    dependencies(),
		Token:           a.token,
	}, nil
}

// revision is a short fingerprint of the saved profile file. The page sends it
// back with each edit, so an edit made in another tab is never overwritten.
func revision(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])[:16]
}

// pdfTools are the outside programs that build and check the resume PDF.
var pdfTools = []string{"typst", "pdfinfo", "pdftotext"}

// dependencies reports which PDF tools are installed.
func dependencies() map[string]bool {
	found := make(map[string]bool, len(pdfTools))
	for _, name := range pdfTools {
		_, err := exec.LookPath(name)
		found[name] = err == nil
	}
	return found
}
