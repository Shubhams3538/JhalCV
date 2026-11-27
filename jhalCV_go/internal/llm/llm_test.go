package llm

import (
	"context"
	"os"
	"path/filepath"
	"slices"
	"testing"
)

var readySchema = Object(map[string]any{"ready": Boolean, "items": Strings})

func TestStrictifyRequiresEveryProperty(t *testing.T) {
	s := Strictify(readySchema)
	if s["additionalProperties"] != false {
		t.Error("additionalProperties not false")
	}
	if got := s["required"].([]string); !slices.Equal(got, []string{"items", "ready"}) {
		t.Errorf("required = %v", got)
	}
	if _, touched := readySchema["required"]; touched {
		t.Error("Strictify changed its input")
	}
}

func TestValidateOutput(t *testing.T) {
	strict := Strictify(readySchema)
	tests := []struct {
		name  string
		value any
		ok    bool
	}{
		{"valid", map[string]any{"ready": true, "items": []any{"a"}}, true},
		{"missing field", map[string]any{"ready": true}, false},
		{"extra field", map[string]any{"ready": true, "items": []any{}, "x": 1.0}, false},
		{"wrong type", map[string]any{"ready": "yes", "items": []any{}}, false},
		{"wrong item type", map[string]any{"ready": true, "items": []any{1.0}}, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if err := ValidateOutput(tt.value, strict); (err == nil) != tt.ok {
				t.Errorf("err = %v, want ok=%v", err, tt.ok)
			}
		})
	}
	if ValidateOutput(2.0, Integer) != nil || ValidateOutput(2.5, Integer) == nil {
		t.Error("integer check wrong")
	}
	if ValidateOutput("b", Enum("a")) == nil {
		t.Error("enum not enforced")
	}
}

func TestJSONFromTextToleratesFencesAndPreamble(t *testing.T) {
	for _, text := range []string{"```json\n{\"a\": 1}\n```", "Sure! {\"a\": 1} done", `{"a": 1}`} {
		v, err := jsonFromText(text)
		if err != nil || v.(map[string]any)["a"] != 1.0 {
			t.Errorf("%q: got %v, %v", text, v, err)
		}
	}
}

// fakeCLI puts a "claude" script on PATH that answers with a fixed result
// and counts how often it ran.
func fakeCLI(t *testing.T, answer string) string {
	t.Helper()
	bin := t.TempDir()
	counter := filepath.Join(bin, "calls")
	script := "#!/bin/sh\ncat > /dev/null\necho x >> " + counter + "\necho '" + answer + "'\n"
	if err := os.WriteFile(filepath.Join(bin, "claude"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin+":/usr/bin:/bin")
	return counter
}

func calls(t *testing.T, counter string) int {
	data, _ := os.ReadFile(counter)
	return len(data) / 2
}

func TestAskUsesClaudeCLIAndCaches(t *testing.T) {
	counter := fakeCLI(t, `{"structured_output": {"ready": true, "items": ["go"]}}`)
	session := NewSession(nil, t.TempDir(), []string{"claude"})
	ctx := WithSession(context.Background(), session)
	req := Request{Prompt: "Ready?", Schema: readySchema}

	answer, cached, err := Ask(ctx, req)
	if err != nil || cached || answer["ready"] != true {
		t.Fatalf("first ask: %v, cached=%v, err=%v", answer, cached, err)
	}
	if used := session.UsedProviders(); len(used) != 1 || used[0].Provider != "claude" {
		t.Errorf("used = %v", used)
	}

	_, cached, err = Ask(ctx, req)
	if err != nil || !cached {
		t.Errorf("second ask should come from the cache: cached=%v err=%v", cached, err)
	}
	if n := calls(t, counter); n != 1 {
		t.Errorf("CLI ran %d times, want 1", n)
	}
}

func TestAskRejectsAnswersThatBreakTheSchema(t *testing.T) {
	fakeCLI(t, `{"structured_output": {"ready": "maybe"}}`)
	ctx := WithSession(context.Background(), NewSession(nil, "", []string{"claude"}))
	if _, _, err := Ask(ctx, Request{Prompt: "Ready?", Schema: readySchema, NoCache: true}); !IsError(err) {
		t.Errorf("err = %v, want an AI failure", err)
	}
}

func TestAskWithNothingConfigured(t *testing.T) {
	ctx := WithSession(context.Background(), NewSession(nil, "", []string{}))
	if _, _, err := Ask(ctx, Request{Prompt: "x", Schema: readySchema}); err == nil || !err.(*Error).Auth {
		t.Errorf("err = %v, want a missing-key error", err)
	}
}
