// Package storage reads and checks the local profile and settings files,
// like resume/storage.py.
package storage

import (
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"jhalcv/internal/usererr"
)

// Settings are the user's resume-output preferences, saved in .local/settings.json.
type Settings struct {
	Filename  string `json:"filename"`
	OutputDir string `json:"output_dir"`
	Tailorer  string `json:"tailorer"`
	Model     string `json:"model"`
}

// Store finds the workspace files under one root folder.
type Store struct {
	root string
}

// NewStore returns a Store for the project folder root.
func NewStore(root string) *Store {
	return &Store{root: root}
}

func (s *Store) settingsPath() string {
	return filepath.Join(s.root, ".local", "settings.json")
}

// Settings returns the saved settings, using defaults for anything not saved.
func (s *Store) Settings() (Settings, error) {
	settings := Settings{
		Filename:  "shubham_Resume",
		OutputDir: filepath.Join(s.root, "build"),
		Tailorer:  "auto",
		Model:     "gpt-5.5",
	}

	data, err := os.ReadFile(s.settingsPath())
	switch {
	case errors.Is(err, fs.ErrNotExist):
		// Nothing saved yet: keep the defaults.
	case err != nil:
		return Settings{}, err
	default:
		if err := json.Unmarshal(data, &settings); err != nil {
			return Settings{}, usererr.Errorf("Settings file %s is not valid JSON: %v", s.settingsPath(), err)
		}
	}

	return s.validateSettings(settings)
}

// A filename may use only letters, digits, underscores, spaces and hyphens,
// so it can never point into another folder ("../") or carry an extension.
var filenamePattern = regexp.MustCompile(`^[\w -]+$`)

func (s *Store) validateSettings(v Settings) (Settings, error) {
	filename, err := text(v.Filename, "Filename", 100, true)
	if err != nil {
		return Settings{}, err
	}
	if !filenamePattern.MatchString(filename) {
		return Settings{}, usererr.New("Filename: use letters, numbers, spaces, hyphens or underscores; omit .pdf.")
	}

	folder, err := text(v.OutputDir, "Output folder", 2000, true)
	if err != nil {
		return Settings{}, err
	}
	folder, err = expandHome(folder)
	if err != nil {
		return Settings{}, err
	}
	if !filepath.IsAbs(folder) {
		folder = filepath.Join(s.root, folder)
	}

	if v.Tailorer != "auto" && v.Tailorer != "rules" {
		return Settings{}, usererr.New("Choose auto or rules tailoring.")
	}

	model, err := text(v.Model, "Model", 100, true)
	if err != nil {
		return Settings{}, err
	}

	return Settings{
		Filename:  filename,
		OutputDir: filepath.Clean(folder),
		Tailorer:  v.Tailorer,
		Model:     model,
	}, nil
}

// expandHome turns a leading "~" into the user's home folder.
func expandHome(path string) (string, error) {
	if path != "~" && !strings.HasPrefix(path, "~/") {
		return path, nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, strings.TrimPrefix(path, "~")), nil
}
