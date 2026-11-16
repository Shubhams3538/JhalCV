package app

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"jhalcv/internal/usererr"
)

// The sample profile shipped with the Python app, shared by both versions.
const exampleProfilePath = "../../../profile.example.json"

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}

func exampleProfile(t *testing.T) string {
	t.Helper()
	data, err := os.ReadFile(exampleProfilePath)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestStateFallsBackToExampleProfile(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "profile.example.json"), exampleProfile(t))

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
	if state.Profile.Basics.Name != "Ada Lovelace" {
		t.Errorf("Basics.Name = %q, want the example profile's name", state.Profile.Basics.Name)
	}
}

func TestStateRevisionFollowsProfileChanges(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "profile.json")
	a := New(root)

	writeFile(t, path, exampleProfile(t))
	first, err := a.State()
	if err != nil {
		t.Fatal(err)
	}
	writeFile(t, path, strings.Replace(exampleProfile(t), "Ada Lovelace", "Ada Byron", 1))
	second, err := a.State()
	if err != nil {
		t.Fatal(err)
	}

	if first.Example || len(first.ProfileRevision) != 16 {
		t.Errorf("revision %q (example %v), want 16 characters for a saved profile", first.ProfileRevision, first.Example)
	}
	if first.ProfileRevision == second.ProfileRevision {
		t.Error("revision did not change after the profile changed")
	}
}

func TestStateReportsAnInvalidProfileToTheUser(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "profile.json"), `{"basics": {"name": ""}}`)

	_, err := New(root).State()
	if msg, ok := usererr.Message(err); !ok || !strings.Contains(msg, "name: enter text") {
		t.Errorf("error = %v, want a user-facing message about the missing name", err)
	}
}

func TestEachAppGetsItsOwnToken(t *testing.T) {
	root := t.TempDir()
	one, two := New(root).token, New(root).token
	if one == "" || one == two {
		t.Errorf("tokens %q and %q, want two different non-empty secrets", one, two)
	}
}
