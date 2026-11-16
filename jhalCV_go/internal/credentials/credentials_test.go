package credentials

import (
	"encoding/json"
	"strings"
	"testing"
)

const secret = "sk-test-1234567890abcdef"

func TestAddedKeyIsNeverShown(t *testing.T) {
	s := New(t.TempDir(), false)
	if err := s.Add(map[string]any{"provider": "anthropic", "key": secret}); err != nil {
		t.Fatal(err)
	}
	public, err := s.Public("")
	if err != nil {
		t.Fatal(err)
	}
	encoded, _ := json.Marshal(public)
	if strings.Contains(string(encoded), secret) || strings.Contains(string(encoded), secret[:8]) {
		t.Fatalf("public metadata leaks the key: %s", encoded)
	}
	if len(public.Keys) != 1 || public.Keys[0].Model != "claude-sonnet-5-5" || !public.Keys[0].First || public.ShowReminder {
		t.Errorf("unexpected public view: %+v", public)
	}

	routes, _ := s.Routes("")
	if len(routes) != 1 || routes[0].Key != secret {
		t.Errorf("routes = %+v, want the saved key", routes)
	}
}

func TestAddRejectsBadInput(t *testing.T) {
	s := New(t.TempDir(), false)
	tests := []struct {
		name    string
		body    map[string]any
		wantErr string
	}{
		{"unknown provider", map[string]any{"provider": "acme", "key": secret}, "supported AI provider"},
		{"placeholder", map[string]any{"provider": "openai", "key": "your-api-key"}, "not a placeholder"},
		{"spaces", map[string]any{"provider": "openai", "key": "sk test key 123"}, "without spaces"},
		{"key in label", map[string]any{"provider": "openai", "key": secret, "label": "mine " + secret}, "out of labels"},
		{"url as model", map[string]any{"provider": "openai", "key": secret, "model": "https://x"}, "not a URL"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if err := s.Add(tt.body); err == nil || !strings.Contains(err.Error(), tt.wantErr) {
				t.Errorf("err = %v, want %q", err, tt.wantErr)
			}
		})
	}
	if err := s.Add(map[string]any{"provider": "openai", "key": secret}); err != nil {
		t.Fatal(err)
	}
	if err := s.Add(map[string]any{"provider": "openai", "key": secret}); err == nil {
		t.Error("duplicate key accepted")
	}
}

func TestEnvironmentKeysAndPreference(t *testing.T) {
	t.Setenv("GEMINI_API_KEY", "gm-0123456789")
	s := New(t.TempDir(), true)
	if err := s.Add(map[string]any{"provider": "openai", "key": secret}); err != nil {
		t.Fatal(err)
	}
	routes, _ := s.Routes("")
	if len(routes) != 2 || routes[1].ID != "env:gemini" {
		t.Fatalf("routes = %+v", routes)
	}
	if err := s.Prefer("env:gemini"); err != nil {
		t.Fatal(err)
	}
	if routes, _ = s.Routes(""); routes[0].ID != "env:gemini" {
		t.Errorf("preferred route not first: %+v", routes)
	}
	if err := s.Remove("env:gemini"); err != nil {
		t.Fatal(err)
	}
	if routes, _ = s.Routes(""); len(routes) != 1 {
		t.Errorf("hidden env key still offered: %+v", routes)
	}
}
