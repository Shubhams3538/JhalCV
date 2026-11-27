package resume

import (
	"context"
	"encoding/base64"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
	"unicode/utf16"

	"jhalcv/internal/usererr"
)

// ChooseFolder opens the operating system's folder picker, starting at
// initial. It returns "" if the user cancels.
func ChooseFolder(ctx context.Context, initial string) (string, error) {
	initial, _ = filepath.Abs(initial)
	for {
		if fi, err := os.Stat(initial); err == nil && fi.IsDir() {
			break
		}
		parent := filepath.Dir(initial)
		if parent == initial {
			break
		}
		initial = parent
	}

	var cmd []string
	switch {
	case runtime.GOOS == "darwin":
		script := `on run argv
            set selectedFolder to choose folder with prompt "Choose your JhalCV resume folder" default location (POSIX file (item 1 of argv))
            return POSIX path of selectedFolder
        end run`
		cmd = []string{"osascript", "-e", script, initial}
	case runtime.GOOS == "windows":
		// EncodedCommand keeps shell parsing entirely separate from paths.
		script := "Add-Type -AssemblyName System.Windows.Forms\n" +
			"[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding\n" +
			"$dialog = New-Object System.Windows.Forms.FolderBrowserDialog\n" +
			"$dialog.Description = 'Choose your JhalCV resume folder'\n" +
			"try {\n  if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {\n" +
			"    [Console]::WriteLine($dialog.SelectedPath)\n  }\n} finally { $dialog.Dispose() }\n"
		units := utf16.Encode([]rune(script))
		raw := make([]byte, 0, len(units)*2)
		for _, u := range units {
			raw = append(raw, byte(u), byte(u>>8))
		}
		cmd = []string{"powershell.exe", "-NoProfile", "-STA", "-EncodedCommand", base64.StdEncoding.EncodeToString(raw)}
	case lookPath("zenity"):
		cmd = []string{"zenity", "--file-selection", "--directory", "--title=Choose your JhalCV resume folder", "--filename=" + initial + "/"}
	case lookPath("kdialog"):
		cmd = []string{"kdialog", "--getexistingdirectory", initial, "--title", "Choose your JhalCV resume folder"}
	default:
		return "", usererr.New("No native folder picker is available. Enter the destination path manually.")
	}

	ctx, cancel := context.WithTimeout(ctx, 300*time.Second)
	defer cancel()
	c := exec.CommandContext(ctx, cmd[0], cmd[1:]...)
	var stderr strings.Builder
	c.Stderr = &stderr
	out, err := c.Output()
	if ctx.Err() != nil {
		return "", usererr.New("Folder selection timed out. Try again or enter the path manually.")
	}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		if (runtime.GOOS == "darwin" && strings.Contains(stderr.String(), "(-128)")) || (runtime.GOOS != "darwin" && exitErr.ExitCode() == 1) {
			return "", nil // Cancelled.
		}
		return "", usererr.New("Could not open the folder picker. Enter the destination path manually.")
	}
	if err != nil {
		return "", usererr.New("Could not open the folder picker. Enter the destination path manually.")
	}
	selected := strings.TrimSuffix(string(out), "\n")
	if selected == "" {
		return "", nil
	}
	folder, _ := filepath.Abs(selected)
	if real, err := filepath.EvalSymlinks(folder); err == nil {
		folder = real
	}
	if fi, err := os.Stat(folder); err != nil || !fi.IsDir() {
		return "", usererr.New("The selected folder is unavailable. Choose an existing folder.")
	}
	return folder, nil
}

func lookPath(name string) bool {
	_, err := exec.LookPath(name)
	return err == nil
}
