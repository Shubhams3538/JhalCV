package storage

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
)

// WriteJSON saves value as indented JSON, atomically: it writes a temporary
// file next to path and renames it into place, so a crash or a full disk can
// never leave a half-written file. The file is readable only by the user.
// This matches atomic_json() in resume/storage.py.
func WriteJSON(path string, value any) error {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false) // Keep "<", ">" and "&" readable, like Python.
	enc.SetIndent("", "  ")
	if err := enc.Encode(value); err != nil { // Encode ends with a newline.
		return err
	}
	return WriteFile(path, buf.Bytes())
}

// WriteFile atomically replaces path with data, creating its folder if needed.
func WriteFile(path string, data []byte) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(path)+"*.tmp") // Mode 0600.
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name()) // No-op after a successful rename.

	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), path)
}

// Root is the project folder this store reads from.
func (s *Store) Root() string {
	return s.root
}

// ProfilePath is where the user's own profile is saved.
func (s *Store) ProfilePath() string {
	return filepath.Join(s.root, "profile.json")
}

// SettingsPath is where the settings are saved.
func (s *Store) SettingsPath() string {
	return s.settingsPath()
}

// HasProfile reports whether the user has saved their own profile.
func (s *Store) HasProfile() bool {
	_, err := os.Stat(s.ProfilePath())
	return err == nil
}

// SaveProfile checks the profile and saves it as profile.json.
func (s *Store) SaveProfile(p *Profile) error {
	if err := p.Validate(); err != nil {
		return err
	}
	return WriteJSON(s.ProfilePath(), p)
}

// SaveSettings checks the settings and saves them.
func (s *Store) SaveSettings(v Settings) (Settings, error) {
	settings, err := s.ValidateSettings(v)
	if err != nil {
		return Settings{}, err
	}
	return settings, WriteJSON(s.settingsPath(), settings)
}

// ValidateSettings checks settings without saving them.
func (s *Store) ValidateSettings(v Settings) (Settings, error) {
	return s.validateSettings(v)
}

// Text checks any decoded JSON value as user text, like text() in Python:
// it must be a string within limit characters, and non-blank if required.
func Text(value any, label string, limit int, required bool) (string, error) {
	str, ok := value.(string)
	if !ok {
		return "", textError(label, limit)
	}
	return text(str, label, limit, required)
}
