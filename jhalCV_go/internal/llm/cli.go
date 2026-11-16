package llm

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"jhalcv/internal/usage"
)

// CLITimeout bounds one subscription-CLI answer.
const CLITimeout = 75 * time.Second

// CLIError is a CLI that could not answer (missing, logged out, out of
// quota, timed out, or bad output).
type CLIError struct{ Message string }

func (e *CLIError) Error() string { return e.Message }

// secretEnv are removed from a CLI's environment, so a subscription call can
// never be billed to an API account.
var secretEnv = []string{"OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
	"GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "ZAI_API_KEY",
	"ZAI_BASE_URL", "ZHIPUAI_API_KEY", "ZHIPUAI_BASE_URL"}

// Installed reports whether a program is on PATH. A variable so tests can
// stand in for real CLIs.
var Installed = func(name string) bool {
	_, err := exec.LookPath(name)
	return err == nil
}

// ProjectRoot is the folder the Codex sandbox hides from the model. It is
// set once at startup.
var ProjectRoot string

// RunCLI asks the named CLI ("claude" or "codex") for an answer matching
// schema. It runs in dir, an empty folder, with no tools enabled.
func RunCLI(ctx context.Context, name, prompt string, schema Schema, dir string) (map[string]any, error) {
	switch name {
	case "claude":
		return runClaude(ctx, prompt, schema, dir)
	case "codex":
		return runCodex(ctx, prompt, schema, dir)
	}
	return nil, &CLIError{"could not start (unknown CLI " + name + ")"}
}

func cleanEnv(extra map[string]string) []string {
	var env []string
	for _, kv := range os.Environ() {
		name, _, _ := strings.Cut(kv, "=")
		if _, override := extra[name]; override {
			continue
		}
		secret := false
		for _, s := range secretEnv {
			if name == s {
				secret = true
				break
			}
		}
		if !secret {
			env = append(env, kv)
		}
	}
	for k, v := range extra {
		env = append(env, k+"="+v)
	}
	return env
}

func run(ctx context.Context, agent string, cmd []string, prompt, dir string, extraEnv map[string]string) (string, error) {
	timeout := CLITimeout
	if deadline, ok := ctx.Deadline(); ok {
		timeout = min(timeout, time.Until(deadline))
	}
	if timeout <= 0 {
		return "", &CLIError{"timed out"}
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	c := exec.CommandContext(ctx, cmd[0], cmd[1:]...)
	c.Dir = dir
	c.Env = cleanEnv(extraEnv)
	c.Stdin = strings.NewReader(prompt)
	var stdout, stderr bytes.Buffer
	c.Stdout, c.Stderr = &stdout, &stderr
	err := c.Run()
	usage.RecordAgent(ctx, agent, stdout.String())

	if ctx.Err() != nil {
		return "", &CLIError{"timed out"}
	}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		detail := strings.TrimSpace(stderr.String())
		if detail == "" {
			detail = strings.TrimSpace(stdout.String())
		}
		if lines := strings.Split(detail, "\n"); detail != "" {
			last := lines[len(lines)-1]
			if len(last) > 200 {
				last = last[:200]
			}
			return "", &CLIError{last}
		}
		return "", &CLIError{fmt.Sprintf("exit code %d", exitErr.ExitCode())}
	}
	if err != nil {
		return "", &CLIError{"could not start (" + err.Error() + ")"}
	}
	return stdout.String(), nil
}

func runClaude(ctx context.Context, prompt string, schema Schema, dir string) (map[string]any, error) {
	schemaJSON, _ := json.Marshal(schema)
	out, err := run(ctx, "claude", []string{"claude", "-p", "--output-format", "json", "--json-schema", string(schemaJSON),
		"--no-session-persistence", "--tools", "", "--strict-mcp-config", "--model", "sonnet"}, prompt, dir, nil)
	if err != nil {
		return nil, err
	}
	var data map[string]any
	if json.Unmarshal([]byte(out), &data) != nil {
		return nil, &CLIError{"returned unreadable output"}
	}
	answer, ok := data["structured_output"].(map[string]any)
	if data["is_error"] == true || !ok {
		msg := "returned no structured answer"
		if result, ok := data["result"].(string); ok && result != "" {
			msg = result
		}
		if len(msg) > 200 {
			msg = msg[:200]
		}
		return nil, &CLIError{msg}
	}
	return answer, nil
}

func runCodex(ctx context.Context, prompt string, schema Schema, dir string) (map[string]any, error) {
	schemaPath, outPath := filepath.Join(dir, "schema.json"), filepath.Join(dir, "answer.json")
	schemaJSON, _ := json.Marshal(schema)
	if err := os.WriteFile(schemaPath, schemaJSON, 0o600); err != nil {
		return nil, err
	}
	// Installers often put a symlink in ~/.local/bin, which the sandbox
	// denies. Run its real target instead, without opening up ~/.local.
	executable, err := exec.LookPath("codex")
	if err != nil {
		return nil, &CLIError{"could not start (Codex is not installed)"}
	}
	if real, err := filepath.EvalSymlinks(executable); err == nil {
		executable = real
	}
	home, _ := os.UserHomeDir()
	codexHome := os.Getenv("CODEX_HOME")
	if codexHome == "" {
		codexHome = filepath.Join(home, ".codex")
	}
	cmd := append(seatbelt(), executable, "exec", "--json", "--skip-git-repo-check", "--ephemeral",
		"--ignore-user-config", "--ignore-rules", "--sandbox", "read-only", "-C", dir,
		"--output-schema", schemaPath, "-o", outPath, "-")
	// HOME points at the empty folder so ~-based reads find nothing, while
	// CODEX_HOME stays real so sign-in still works.
	if _, err := run(ctx, "codex", cmd, prompt, dir, map[string]string{"HOME": dir, "CODEX_HOME": codexHome}); err != nil {
		return nil, err
	}
	data, err := os.ReadFile(outPath)
	var answer map[string]any
	if err != nil || json.Unmarshal(data, &answer) != nil {
		return nil, &CLIError{"returned no structured answer"}
	}
	return answer, nil
}

const seatbeltProfile = `(version 1)
(allow default)
(deny file-read*
  (literal "{home}/.env") (literal "{home}/.netrc")
  (subpath "{home}/.ssh") (subpath "{home}/.gnupg") (subpath "{home}/.aws")
  (subpath "{home}/.kube") (subpath "{home}/.config") (subpath "{home}/.local")
  (subpath "{home}/.zcode") (subpath "{home}/Library/Keychains")
  (subpath "{home}/Library/Cookies") (subpath "{home}/Library/Preferences")
  (subpath "{home}/Library/Application Support")
  (subpath "{repo}"))
`

var (
	seatbeltOnce sync.Once
	seatbeltCmd  []string
)

// seatbelt returns a macOS sandbox-exec wrapper for Codex, or nothing where
// it cannot run. Codex keeps a read-only shell tool, so text injected into a
// job page could ask it to read local secrets; this blocks those paths at
// the OS level. Probed once: a wrapper that cannot run must not break Codex.
func seatbelt() []string {
	seatbeltOnce.Do(func() {
		exe, err := exec.LookPath("sandbox-exec")
		if err != nil {
			return
		}
		home, _ := os.UserHomeDir()
		profile := strings.NewReplacer("{home}", home, "{repo}", ProjectRoot).Replace(seatbeltProfile)
		f, err := os.CreateTemp("", "jhalcv-*.sb")
		if err != nil {
			return
		}
		_, werr := f.WriteString(profile)
		f.Close()
		if werr != nil {
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		if exec.CommandContext(ctx, exe, "-f", f.Name(), "/bin/echo", "ok").Run() == nil {
			seatbeltCmd = []string{exe, "-f", f.Name()}
		}
	})
	return seatbeltCmd
}
