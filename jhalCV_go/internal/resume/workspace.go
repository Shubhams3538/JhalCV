package resume

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"jhalcv/internal/storage"
	"jhalcv/internal/usererr"
)

// Draft is one tailored resume awaiting review. Source is the tailored data
// with original wording; Data holds the reviewed (possibly AI) wording.
type Draft struct {
	Created time.Time
	Source  *Data
	Data    *Data
	JD      string
	Report  Report
	Notice  string
	Library *storage.Profile
	Preview *Preview
}

// Preview is the last PDF built for a draft.
type Preview struct {
	Revision string
	Path     string
	Checks   []Check
	Notices  []string
	Exported string
}

// ProjectEdit is the reviewed wording of one project's bullets.
type ProjectEdit struct {
	Bullets []string `json:"bullets"`
}

// EditedProjects applies reviewed wording, checking every bullet against the
// immutable source budget.
func EditedProjects(source *Data, edits any) (*Data, error) {
	list, ok := edits.([]any)
	if !ok || len(list) != len(source.Projects) {
		return nil, usererr.New("Project list changed. Create a new tailored draft.")
	}
	proposed := source.Clone()
	for i, old := range source.Projects {
		edit, _ := list[i].(map[string]any)
		bullets, ok := edit["bullets"].([]any)
		if !ok || len(bullets) != len(old.Bullets) {
			return nil, usererr.New("Keep the original bullet count.")
		}
		revised := make([]string, len(bullets))
		for j, raw := range bullets {
			text, isText := raw.(string)
			if !isText || !Within(text, old.Bullets[j]) {
				return nil, usererr.Errorf("%s, bullet %d: keep within the original character/word budget and do not add numbers.", old.Name, j+1)
			}
			revised[j] = strings.TrimSpace(text)
		}
		proposed.Projects[i].Bullets = revised
	}
	return proposed, nil
}

// fitSkills keeps as many skills as the finished page holds, or nil if even
// the minimum does not fit.
func (r *Renderer) fitSkills(ctx context.Context, candidate *Data, plan *SkillPlan, dir string) ([]SkillRow, error) {
	full, removals := expand(candidate.Skills, plan)
	trial, trialData := filepath.Join(dir, "skills-trial.pdf"), filepath.Join(dir, "skills-trial.data.json")
	defer os.Remove(trial)
	defer os.Remove(trialData)

	fits := func(count int) (bool, error) {
		attempt := candidate.Clone()
		attempt.Skills = without(full, removals[:count])
		if err := r.Compile(ctx, attempt, trial, trialData); err != nil {
			return false, err
		}
		pages, err := PageCount(ctx, trial)
		return pages == 1, err
	}
	ok, err := fits(len(removals))
	if err != nil || !ok {
		return nil, err
	}
	// Each removal only frees space, so binary-search the fewest removals.
	low, high := 0, len(removals)
	for low < high {
		middle := (low + high) / 2
		ok, err := fits(middle)
		if err != nil {
			return nil, err
		}
		if ok {
			high = middle
		} else {
			low = middle + 1
		}
	}
	return without(full, removals[:low]), nil
}

// PreviewResult is what the page shows after a preview build.
type PreviewResult struct {
	Revision string   `json:"revision"`
	URL      string   `json:"url"`
	Checks   []Check  `json:"checks"`
	Notices  []string `json:"notices"`
	Pages    int      `json:"pages"`
}

func randomToken(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// BuildPreview renders the reviewed draft, fitted to one page with the
// original wording's layout, and verifies the PDF.
func (r *Renderer) BuildPreview(ctx context.Context, draft *Draft, edits any, id string) (*PreviewResult, error) {
	source := draft.Source.Clone()
	proposed, err := EditedProjects(source, edits)
	if err != nil {
		return nil, err
	}
	dir := filepath.Join(r.Root, ".cache", "web", id, randomToken(8))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	baseline := filepath.Join(dir, "baseline.pdf")
	result, err := r.Fit(ctx, source, baseline, filepath.Join(dir, "baseline.data.json"), 1)
	if err != nil {
		return nil, err
	}
	if result.Pages != 1 {
		return nil, usererr.New("Your original content cannot fit on one page. Shorten your saved profile, especially the fixed sections, before exporting.")
	}
	notices := []string{}
	if len(result.Dropped) > 0 {
		notices = append(notices, fmt.Sprintf("The baseline fitter omitted %d lower-ranked project bullet(s) to keep one page.", len(result.Dropped)))
	}

	// Use exactly the baseline typography; never squeeze AI wording into a
	// tighter preset.
	candidate := source.Clone()
	for i := range candidate.Projects {
		replacements := map[string]string{}
		for j, m := range draft.Source.Projects[i].BulletMeta {
			replacements[m.ID] = proposed.Projects[i].Bullets[j]
		}
		for j, m := range candidate.Projects[i].BulletMeta {
			candidate.Projects[i].Bullets[j] = replacements[m.ID]
		}
	}
	out, dataPath := filepath.Join(dir, "resume.pdf"), filepath.Join(dir, "resume.data.json")
	if err := r.Compile(ctx, candidate, out, dataPath); err != nil {
		return nil, err
	}
	if pages, err := PageCount(ctx, out); err != nil {
		return nil, err
	} else if pages != 1 {
		candidate = source
		pdf, err := os.ReadFile(baseline)
		if err != nil {
			return nil, err
		}
		if err := os.WriteFile(out, pdf, 0o600); err != nil {
			return nil, err
		}
		if err := storage.WriteJSON(dataPath, source); err != nil {
			return nil, err
		}
		notices = append(notices, "Reviewed wording wrapped beyond one page. Restored original project wording at the same layout; please review this PDF before saving.")
	}

	var swapped *SwapSummary
	if plan, ok := draft.Report["skill_plan"].(*SkillPlan); ok && plan != nil {
		// The layout is settled with the saved skills, so job terms only use
		// room that is left; the saved skills stay as the safe fallback.
		tuned, summary, err := r.tryJobSkills(ctx, candidate, plan, dir, out, dataPath)
		if err != nil {
			if !isRenderError(err) {
				return nil, err
			}
			if err := r.Compile(ctx, candidate, out, dataPath); err != nil {
				return nil, err
			}
			notices = append(notices, "Kept your saved skills because the job skills did not fit or failed the PDF check.")
		} else {
			swapped = summary
			notices = append(notices, swapNotice(summary)...)
			candidate = tuned
		}
	}

	checks, err := Verify(ctx, out, candidate, 1)
	if err != nil {
		return nil, err
	}
	if b := blocking(checks); len(b) > 0 {
		var parts []string
		for _, c := range b {
			parts = append(parts, c.Check+": "+c.Detail)
		}
		return nil, usererr.New("PDF verification failed: " + strings.Join(parts, "; "))
	}
	revision := randomToken(18)
	if err := storage.WriteJSON(filepath.Join(dir, "resume.report.json"), map[string]any{
		"generated": time.Now().UTC().Format("2006-01-02T15:04:05.000000-07:00"), "jd_text": draft.JD,
		"report": draft.Report, "wording_notice": draft.Notice, "fit": result, "layout_notices": notices,
		"verify_failures": checks, "skill_swap": swapped,
	}); err != nil {
		return nil, err
	}
	draft.Preview = &Preview{Revision: revision, Path: out, Checks: checks, Notices: notices}
	return &PreviewResult{Revision: revision, URL: "/api/pdf/" + id + "/" + revision, Checks: checks, Notices: notices, Pages: 1}, nil
}

func (r *Renderer) tryJobSkills(ctx context.Context, candidate *Data, plan *SkillPlan, dir, out, dataPath string) (*Data, *SwapSummary, error) {
	skills, err := r.fitSkills(ctx, candidate, plan, dir)
	if err != nil {
		return nil, nil, err
	}
	if skills == nil {
		return nil, nil, &RenderError{"job skills do not fit"}
	}
	tuned := candidate.Clone()
	tuned.Skills = skills
	if err := r.Compile(ctx, tuned, out, dataPath); err != nil {
		return nil, nil, err
	}
	pages, err := PageCount(ctx, out)
	if err != nil {
		return nil, nil, err
	}
	checks, err := Verify(ctx, out, tuned, 1)
	if err != nil {
		return nil, nil, err
	}
	if pages != 1 || len(blocking(checks)) > 0 {
		return nil, nil, &RenderError{"job skills failed verification"}
	}
	return tuned, summarizeSwap(plan, tuned, candidate.Skills, skills), nil
}

func isRenderError(err error) bool {
	var r *RenderError
	return errors.As(err, &r)
}

// ExportResult is where a saved resume went.
type ExportResult struct {
	Path     string `json:"path"`
	Archived string `json:"archived,omitempty"`
}

// Export saves the reviewed preview as the one current PDF in the settings'
// folder, moving any previous version into Archive/.
func Export(draft *Draft, revision any, settings storage.Settings) (*ExportResult, error) {
	rev, ok := revision.(string)
	if draft.Preview == nil || !ok || rev != draft.Preview.Revision {
		return nil, usererr.New("Build and review the latest PDF preview before saving.")
	}
	dest := settings.OutputDir
	if err := os.MkdirAll(dest, 0o755); err != nil {
		return nil, err
	}
	unlock, err := lockFolder(dest)
	if err != nil {
		return nil, err
	}
	defer unlock()
	return saveCurrent(draft.Preview, dest, settings.Filename)
}

// lockFolder takes an exclusive lock on dest/.jhalcv-save.lock, so two saves
// into one folder cannot interleave. The OS releases it if the process dies.
func lockFolder(dest string) (func(), error) {
	f, err := os.OpenFile(filepath.Join(dest, ".jhalcv-save.lock"), os.O_RDWR|os.O_CREATE|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return nil, usererr.New("Another resume is being saved in this folder. Try again in a moment.")
	}
	return func() {
		_ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
		f.Close()
	}, nil
}

func saveCurrent(preview *Preview, dest, filename string) (*ExportResult, error) {
	target := filepath.Join(dest, filename+".pdf")
	pdf, err := os.ReadFile(preview.Path)
	if err != nil {
		return nil, err
	}
	info, statErr := os.Lstat(target)
	exists := statErr == nil
	if exists && (info.Mode()&fs.ModeSymlink != 0 || !info.Mode().IsRegular()) {
		return nil, usererr.New("The resume destination must be a regular PDF file, not a link or folder.")
	}
	// Re-saving the same preview is a no-op only while that PDF is current.
	if preview.Exported == target && exists {
		if current, err := os.ReadFile(target); err == nil && bytes.Equal(current, pdf) {
			return &ExportResult{Path: target}, nil
		}
	}

	tmp, err := os.CreateTemp(dest, ".jhalcv-*.tmp")
	if err != nil {
		return nil, err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(pdf); err != nil {
		tmp.Close()
		return nil, err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return nil, err
	}
	if err := tmp.Close(); err != nil {
		return nil, err
	}

	archived := ""
	if exists {
		if archived, err = archive(dest, target, filename, info); err != nil {
			return nil, err
		}
	}
	// The complete new PDF becomes current atomically; any failure above
	// leaves the previous PDF in place. Archive names are never reused.
	if err := os.Rename(tmp.Name(), target); err != nil {
		return nil, err
	}
	preview.Exported = target
	return &ExportResult{Path: target, Archived: archived}, nil
}

func archive(dest, target, filename string, info fs.FileInfo) (string, error) {
	dir := filepath.Join(dest, "Archive")
	if fi, err := os.Lstat(dir); err == nil && fi.Mode()&fs.ModeSymlink != 0 {
		return "", usererr.New("Archive must be a regular folder, not a symbolic link.")
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	stamp := time.Now().UTC().Format("2006-01-02_15-04-05_000000")
	for range 10 {
		path := filepath.Join(dir, fmt.Sprintf("%s_%s_%s.pdf", filename, stamp, randomToken(4)))
		backup, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
		if errors.Is(err, fs.ErrExist) {
			continue
		}
		if err != nil {
			return "", err
		}
		if err := copyInto(backup, target); err != nil {
			backup.Close()
			os.Remove(path)
			return "", err
		}
		// Keep the original timestamps on archived versions.
		_ = os.Chtimes(path, info.ModTime(), info.ModTime())
		return path, nil
	}
	return "", usererr.New("Could not reserve an archive filename. Try saving again.")
}

func copyInto(backup *os.File, source string) error {
	in, err := os.Open(source)
	if err != nil {
		return err
	}
	defer in.Close()
	if _, err := io.Copy(backup, in); err != nil {
		return err
	}
	if err := backup.Sync(); err != nil {
		return err
	}
	return backup.Close()
}
