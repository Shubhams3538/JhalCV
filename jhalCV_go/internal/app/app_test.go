package app

import (
	"os"
	"path/filepath"
	"testing"
)

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestStateFallsBackToExampleProfile(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "profile.example.json"), `{"basics": {"name": "Ada"}}`)

	state, err := New(root).State()
	if err != nil {
		t.Fatal(err)
	}
	if !state.Example {
		t.Error("Example = false, want true when profile.json is missing")
	}
	if state.ProfileRevision != "" {
		t.Errorf("ProfileRevision = %q, want empty for the example profile", state.ProfileRevision)
	}
	if string(state.Profile) != `{"basics": {"name": "Ada"}}` {
		t.Errorf("Profile = %s, want the example file unchanged", state.Profile)
	}
}

func TestStateRevisionFollowsProfileChanges(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "profile.json")
	a := New(root)

	writeFile(t, path, `{"version": 1}`)
	first, err := a.State()
	if err != nil {
		t.Fatal(err)
	}
	writeFile(t, path, `{"version": 2}`)
	second, err := a.State()
	if err != nil {
		t.Fatal(err)
	}

	if len(first.ProfileRevision) != 16 {
		t.Errorf("revision %q, want 16 characters", first.ProfileRevision)
	}
	if first.ProfileRevision == second.ProfileRevision {
		t.Error("revision did not change after the profile changed")
	}
}

func TestEachAppGetsItsOwnToken(t *testing.T) {
	root := t.TempDir()
	one, two := New(root).token, New(root).token
	if one == "" || one == two {
		t.Errorf("tokens %q and %q, want two different non-empty secrets", one, two)
	}
}
