package storage

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
)

// ProfileJSON returns the saved profile.json, or the bundled example profile
// when none is saved yet (example is then true).
//
// For now the JSON is passed through unchanged. A later module turns it into
// checked Go structs, like validate_profile() in resume/storage.py.
func (s *Store) ProfileJSON() (data []byte, example bool, err error) {
	data, err = os.ReadFile(filepath.Join(s.root, "profile.json"))
	if errors.Is(err, fs.ErrNotExist) {
		data, err = os.ReadFile(filepath.Join(s.root, "profile.example.json"))
		return data, true, err
	}
	return data, false, err
}
