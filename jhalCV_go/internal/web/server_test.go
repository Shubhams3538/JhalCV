package web

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"jhalcv/internal/app"
)

const port = 8765

func newServer(t *testing.T) (http.Handler, *app.App) {
	t.Helper()
	root := t.TempDir()
	example, err := os.ReadFile("../../../profile.example.json")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "profile.example.json"), example, 0o600); err != nil {
		t.Fatal(err)
	}
	companies, err := os.ReadFile("../../../data/companies.json")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "data"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "data", "companies.json"), companies, 0o600); err != nil {
		t.Fatal(err)
	}
	a, err := app.New(root)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { a.Close() })
	return New(a, "../../../web", port), a
}

func do(h http.Handler, method, path, body string, headers map[string]string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, "http://127.0.0.1:8765"+path, strings.NewReader(body))
	req.Host = "127.0.0.1:8765"
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestStaticFilesAndHostCheck(t *testing.T) {
	h, _ := newServer(t)
	if rec := do(h, "GET", "/style.css", "", nil); rec.Code != 200 || rec.Header().Get("Content-Security-Policy") == "" {
		t.Errorf("style.css: %d %v", rec.Code, rec.Header())
	}
	req := httptest.NewRequest("GET", "/", nil)
	req.Host = "evil.example:8765"
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != 403 {
		t.Errorf("foreign Host got %d, want 403", rec.Code)
	}
}

func TestStateShapeMatchesTheFrontend(t *testing.T) {
	h, _ := newServer(t)
	rec := do(h, "GET", "/api/state", "", nil)
	var state map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &state); err != nil || rec.Code != 200 {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	for _, key := range []string{"profile", "settings", "example", "ai_available", "credentials", "profile_revision", "dependencies", "setup", "token"} {
		if _, ok := state[key]; !ok {
			t.Errorf("missing %q", key)
		}
	}
	providers := state["credentials"].(map[string]any)["providers"].([]any)
	if len(providers) != 5 {
		t.Errorf("providers = %d, want 5", len(providers))
	}
}

func TestPostNeedsTheToken(t *testing.T) {
	h, a := newServer(t)
	body := `{"model": "gpt-5.5"}`
	tests := []struct {
		name    string
		headers map[string]string
		want    int
	}{
		{"no token", map[string]string{"Content-Type": "application/json"}, 403},
		{"wrong origin", map[string]string{"Content-Type": "application/json", "X-App-Token": a.Token(), "Origin": "https://evil.example"}, 403},
		{"not JSON", map[string]string{"Content-Type": "text/plain", "X-App-Token": a.Token()}, 400},
		{"valid", map[string]string{"Content-Type": "application/json", "X-App-Token": a.Token(), "Origin": "http://127.0.0.1:8765"}, 200},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if rec := do(h, "POST", "/api/usage/settings", body, tt.headers); rec.Code != tt.want {
				t.Errorf("got %d (%s), want %d", rec.Code, rec.Body, tt.want)
			}
		})
	}
}

func TestSettingsRoundTrip(t *testing.T) {
	h, a := newServer(t)
	headers := map[string]string{"Content-Type": "application/json", "X-App-Token": a.Token()}
	rec := do(h, "POST", "/api/settings", `{"filename": "../x", "output_dir": "out", "tailorer": "auto", "model": "m"}`, headers)
	if rec.Code != 400 || !strings.Contains(rec.Body.String(), "Filename") {
		t.Errorf("bad filename: %d %s", rec.Code, rec.Body)
	}
	rec = do(h, "POST", "/api/settings", `{"filename": "CV", "output_dir": "out", "tailorer": "rules", "model": "m"}`, headers)
	body, _ := io.ReadAll(rec.Body)
	if rec.Code != 200 || !strings.Contains(string(body), `"tailorer":"rules"`) {
		t.Errorf("save: %d %s", rec.Code, body)
	}
}
