package storage

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// writeSettings saves content as .local/settings.json under root.
func writeSettings(t *testing.T, root, content string) {
	t.Helper()
	dir := filepath.Join(root, ".local")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "settings.json"), []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestSettingsDefaults(t *testing.T) {
	root := t.TempDir()

	got, err := NewStore(root).Settings()
	if err != nil {
		t.Fatal(err)
	}

	want := Settings{
		Filename:  "shubham_Resume",
		OutputDir: filepath.Join(root, "build"),
		Tailorer:  "auto",
		Model:     "gpt-5.5",
	}
	if got != want {
		t.Errorf("got %+v\nwant %+v", got, want)
	}
}

func TestSettingsKeepsDefaultsForMissingFields(t *testing.T) {
	root := t.TempDir()
	writeSettings(t, root, `{"filename": "  My CV  ", "output_dir": "out"}`)

	got, err := NewStore(root).Settings()
	if err != nil {
		t.Fatal(err)
	}

	if got.Filename != "My CV" {
		t.Errorf("Filename = %q, want spaces trimmed", got.Filename)
	}
	if got.OutputDir != filepath.Join(root, "out") {
		t.Errorf("OutputDir = %q, want it inside the project", got.OutputDir)
	}
	if got.Tailorer != "auto" || got.Model != "gpt-5.5" {
		t.Errorf("defaults lost: %+v", got)
	}
}


func TestSettingsRejectsBadValues(t *testing.T) {
	tests := []struct {
		name    string
		file    string
		wantErr string
	}{
		{"filename with extension", `{"filename": "resume.pdf"}`, "Filename: use letters"},
		{"filename escaping the folder", `{"filename": "../evil"}`, "Filename: use letters"},
		{"unknown tailorer", `{"tailorer": "magic"}`, "Choose auto or rules"},
		{"blank model", `{"model": "   "}`, "Model: enter text"},
		{"control character", `{"model": "gpt\u0007"}`, "unsupported control character"},
		{"broken JSON", `{"filename":`, "settings.json"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			root := t.TempDir()
			writeSettings(t, root, tt.file)

			_, err := NewStore(root).Settings()
			if err == nil || !strings.Contains(err.Error(), tt.wantErr) {
				t.Errorf("error = %v, want one containing %q", err, tt.wantErr)
			}
		})
	}
}