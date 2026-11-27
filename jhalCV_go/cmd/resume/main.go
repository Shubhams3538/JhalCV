// Command resume is the terminal workflow, like main.py and
// "python3 -m resume":
//
//	resume                        prompt for a job description
//	resume path/to/jd.txt         use a file instead
//	resume make --jd FILE         tailor, fit and verify one resume
//	resume check FILE.pdf         run the ATS checks on any PDF
//	resume lock                   prove frozen sections never vary by job
//	resume compare --jd FILE      rules vs AI job analysis, side by side
//	resume jobs refresh|recheck   refresh or recheck saved jobs
package main

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"slices"
	"strings"
	"time"

	"golang.org/x/term"

	"jhalcv/internal/app"
	"jhalcv/internal/credentials"
	"jhalcv/internal/llm"
	"jhalcv/internal/resume"
	"jhalcv/internal/storage"
	"jhalcv/internal/usage"
)

const (
	green, red, yellow, dim, bold, reset = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
	rule                                 = "────────────────────────────────────────────────────"
)

var root string

func main() {
	root = os.Getenv("JHALCV_ROOT")
	if root == "" {
		root = ".."
	}
	root, _ = filepath.Abs(root)
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	if len(args) > 0 {
		switch args[0] {
		case "make":
			return cmdMake(args[1:])
		case "check":
			return cmdCheck(args[1:])
		case "lock":
			return cmdLock(args[1:])
		case "compare":
			return cmdCompare(args[1:])
		case "jobs":
			return cmdJobs(args[1:])
		case "-h", "--help", "help":
			fmt.Println(strings.TrimSpace(usageText))
			return 0
		}
	}
	return interactive(args)
}

const usageText = `
resume                        prompt for a job description
resume FILE [--label NAME]    use a job description file instead
resume make --jd FILE         tailor, fit and verify one resume
resume check FILE.pdf         run the ATS checks on any PDF
resume lock                   prove frozen sections never vary by job
resume compare --jd FILE      rules vs AI job analysis, side by side
resume jobs refresh|recheck   refresh or recheck saved jobs

The project folder is ".." (or $JHALCV_ROOT).`

// session gives the CLI every configured key and installed CLI, recording
// usage as "resume CLI".
func session(ctx context.Context, model string) (context.Context, *usage.Store, error) {
	ledger, err := usage.Open(root)
	if err != nil {
		return nil, nil, err
	}
	routes, err := credentials.New(root, true).Routes(model)
	if err != nil {
		ledger.Close()
		return nil, nil, err
	}
	llm.ProjectRoot = root
	ctx = usage.WithScope(ctx, ledger, "resume CLI")
	return llm.WithSession(ctx, llm.NewSession(routes, filepath.Join(root, ".cache", "llm"), nil)), ledger, nil
}

var (
	slugChars = regexp.MustCompile(`[^a-z0-9]+`)
	oscSeq    = regexp.MustCompile(`\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)`)
	csiSeq    = regexp.MustCompile(`\x1b\[[0-9;:?]*[ -/]*[@-~]`)
	controls  = regexp.MustCompile(`[\x00-\x1f\x7f]`)
)

func slug(s string) string {
	out := strings.Trim(slugChars.ReplaceAllString(strings.ToLower(s), "-"), "-")
	return out[:min(len(out), 40)]
}

// plain strips terminal control sequences from model-derived text: whole
// escape sequences, since their payloads are printable.
func plain(v any) string {
	s := fmt.Sprint(v)
	if v == nil {
		s = ""
	}
	return controls.ReplaceAllString(csiSeq.ReplaceAllString(oscSeq.ReplaceAllString(s, ""), ""), "")
}

func loadProfile(path string) (*storage.Profile, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return storage.ParseProfile(data)
}

type makeOptions struct {
	jd, profile, template, out, tailorer, model string
	maxPages, projects, jobBullets, projBullets int
	verbose                                     bool
}

func makeFlags(name string) (*flag.FlagSet, *makeOptions) {
	fs := flag.NewFlagSet(name, flag.ExitOnError)
	o := &makeOptions{}
	fs.StringVar(&o.jd, "jd", "", "path to the job description text file")
	fs.StringVar(&o.profile, "profile", filepath.Join(root, "profile.json"), "profile JSON")
	fs.StringVar(&o.template, "template", "classic", "template name in templates/")
	fs.StringVar(&o.out, "out", "", "output basename (default: date_jdname)")
	fs.IntVar(&o.maxPages, "max-pages", 1, "page budget")
	fs.IntVar(&o.projects, "projects", 2, "projects on the page")
	fs.IntVar(&o.jobBullets, "job-bullets", 5, "bullets per job")
	fs.IntVar(&o.projBullets, "project-bullets", 3, "bullets per project")
	fs.StringVar(&o.tailorer, "tailorer", "auto", "auto, llm or rules")
	fs.StringVar(&o.model, "model", "", "override the AI model")
	fs.BoolVar(&o.verbose, "v", false, "verbose fitting")
	return fs, o
}

func cmdMake(args []string) int {
	fs, o := makeFlags("make")
	_ = fs.Parse(args)
	if o.jd == "" {
		fmt.Fprintln(os.Stderr, "make: --jd is required")
		return 2
	}
	return makeResume(o)
}

func makeResume(o *makeOptions) int {
	if !slices.Contains([]string{"auto", "llm", "rules"}, o.tailorer) {
		fmt.Fprintln(os.Stderr, "--tailorer must be auto, llm or rules")
		return 2
	}
	profile, err := loadProfile(o.profile)
	if err != nil {
		fmt.Fprintf(os.Stderr, "%s✗ %v%s\n", red, err, reset)
		return 1
	}
	jdBytes, err := os.ReadFile(o.jd)
	if err != nil {
		fmt.Fprintf(os.Stderr, "%s✗ %v%s\n", red, err, reset)
		return 1
	}
	jd := string(jdBytes)
	ctx, ledger, err := session(context.Background(), o.model)
	if err != nil {
		fmt.Fprintf(os.Stderr, "%s✗ %v%s\n", red, err, reset)
		return 1
	}
	defer ledger.Close()

	fmt.Printf("%s▸ tailoring%s %s\n", bold, reset, filepath.Base(o.jd))
	var data *resume.Data
	var report resume.Report
	if o.tailorer == "rules" {
		data, report = resume.Tailor(profile, jd, o.projects, o.jobBullets, o.projBullets)
		report["tailorer"] = "rules"
	} else {
		// auto and llm share a path: TailorAI always falls back to rules.
		// The difference is only whether a silent fallback is acceptable.
		data, report = resume.TailorAI(ctx, profile, jd, o.model, o.projects, o.jobBullets, o.projBullets)
	}
	info, _ := report["llm"].(map[string]any)
	if o.tailorer == "llm" && report["tailorer"] != "llm" {
		fmt.Printf("  %s✗ llm unavailable: %s%s\n", red, plain(info["reason"]), reset)
		return 1
	}
	if report["tailorer"] == "llm" {
		cached, _ := info["cached"].(map[string]any)
		state := "called"
		if cached["jd"] == true && cached["selection"] == true {
			state = "cached"
		}
		fmt.Printf("  %sllm%s %s%v (%s)%s\n", green, reset, dim, info["model"], state, reset)
		if s, _ := info["summary"].(string); s != "" {
			fmt.Printf("  %sreads as:%s %s\n", dim, reset, plain(s))
		}
	} else if info != nil && info["reason"] != nil {
		fmt.Printf("  %sfell back to rules:%s %s%s%s\n", yellow, reset, dim, plain(info["reason"]), reset)
	}
	if scores, ok := report["angle_scores"].(resume.AngleScores); ok {
		var top []string
		for _, s := range scores[:min(4, len(scores))] {
			if s.Score != 0 {
				top = append(top, fmt.Sprintf("%s %.0f%%", s.Angle, s.Score*100))
			}
		}
		fmt.Printf("  %sangles:%s %s\n", dim, reset, strings.Join(top, ", "))
	}
	decisions, _ := report["decisions"].([]any)
	for _, raw := range decisions {
		d, _ := raw.(map[string]any)
		if d["section"] != "projects" {
			continue
		}
		switch {
		case d["by"] == "llm":
			fmt.Printf("  %sproject:%s %v %s(%v) — %s%s\n", dim, reset, d["name"], dim, d["angle"], plain(d["why"]), reset)
		case d["name"] != nil:
			fmt.Printf("  %sproject:%s %v %s(score %.2f, angle: %v)%s\n", dim, reset, d["name"], dim, d["score"], d["angle"], reset)
		case d["not_selected"] != nil:
			for _, r := range d["not_selected"].([]any) {
				p := r.(map[string]any)
				fmt.Printf("  %s  skipped: %v (score %.2f)%s\n", dim, p["name"], p["score"], reset)
			}
		}
	}
	if repairs, ok := info["repairs"].([]string); ok {
		for _, fix := range repairs {
			fmt.Printf("  %srepaired:%s %s%s%s\n", yellow, reset, dim, plain(fix), reset)
		}
	}

	name := o.out
	if name == "" {
		name = time.Now().Format(time.DateOnly) + "_" + slug(strings.TrimSuffix(filepath.Base(o.jd), filepath.Ext(o.jd)))
	}
	build := filepath.Join(root, "build")
	if err := os.MkdirAll(build, 0o755); err != nil {
		fmt.Fprintf(os.Stderr, "%s✗ %v%s\n", red, err, reset)
		return 1
	}
	outPDF := filepath.Join(build, name+".pdf")
	renderer := &resume.Renderer{Root: root, Template: filepath.Join(root, "templates", o.template+".typ")}
	fmt.Printf("%s▸ fitting%s to %d page(s)\n", bold, reset, o.maxPages)
	result, err := renderer.Fit(ctx, data, outPDF, filepath.Join(build, name+".data.json"), o.maxPages)
	if err != nil {
		fmt.Fprintf(os.Stderr, "%s✗ typst failed%s\n%v\n", red, reset, err)
		return 1
	}
	if result.Warning != "" {
		fmt.Printf("  %s⚠ %s%s\n", yellow, result.Warning, reset)
	}
	for _, d := range result.Dropped {
		text, _ := d["text"].(string)
		fmt.Printf("  %sdropped for space:%s %s...\n", yellow, reset, firstRunes(text, 60))
	}
	fmt.Printf("  %s%d page(s)%s\n", dim, result.Pages, reset)

	fmt.Printf("%s▸ verifying%s ATS text layer\n", bold, reset)
	fails, err := resume.Verify(ctx, outPDF, data, o.maxPages)
	if err != nil {
		fmt.Fprintf(os.Stderr, "%s✗ %v%s\n", red, err, reset)
		return 1
	}
	printChecks(fails, "  ")
	if gaps, _ := report["gap_skills"].([]string); len(gaps) > 0 {
		fmt.Printf("%s▸ gaps%s %s(in JD, not in your profile — not on the PDF)%s\n", bold, reset, dim, reset)
		fmt.Printf("  %s%s%s\n", yellow, strings.Join(gaps, ", "), reset)
	}
	// The sidecar holds the raw job text, so it is written user-only.
	sidecar := filepath.Join(build, name+".report.json")
	tailorer := report["tailorer"]
	if err := storage.WriteJSON(sidecar, map[string]any{"generated": time.Now().Format(time.DateOnly), "jd_file": o.jd,
		"jd_text": jd, "tailorer": tailorer, "report": report, "fit": result, "verify_failures": fails}); err != nil {
		fmt.Fprintf(os.Stderr, "%s✗ %v%s\n", red, err, reset)
		return 1
	}
	fmt.Printf("\n%s→%s %s\n%s  %s · %s.data.json%s\n", green, reset, outPDF, dim, filepath.Base(sidecar), name, reset)
	if len(fails) > 0 {
		return 1
	}
	return 0
}

func printChecks(fails []resume.Check, indent string) {
	for _, f := range fails {
		fmt.Printf("%s%s✗ [%s]%s %s\n", indent, red, f.Check, reset, f.Detail)
	}
	if len(fails) == 0 {
		fmt.Printf("%s%s✓ all checks passed%s\n", indent, green, reset)
	}
}

func cmdCheck(args []string) int {
	fs := flag.NewFlagSet("check", flag.ExitOnError)
	dataFile := fs.String("data", "", "the .data.json that produced it (omit for structural checks only)")
	maxPages := fs.Int("max-pages", 1, "page budget")
	files := parse(fs, args)
	if len(files) != 1 {
		fmt.Fprintln(os.Stderr, "check: give one PDF")
		return 2
	}
	pdf, ctx := files[0], context.Background()
	var fails []resume.Check
	var err error
	if raw, readErr := os.ReadFile(*dataFile); *dataFile != "" && readErr == nil {
		var data resume.Data
		if err = json.Unmarshal(raw, &data); err == nil {
			fails, err = resume.Verify(ctx, pdf, &data, *maxPages)
		}
	} else {
		fails, err = resume.StructuralChecks(ctx, pdf, *maxPages)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "%s✗ %v%s\n", red, err, reset)
		return 1
	}
	fmt.Printf("%s▸ checking%s %s\n", bold, reset, filepath.Base(pdf))
	printChecks(fails, "")
	if len(fails) > 0 {
		return 1
	}
	return 0
}

func cmdLock(args []string) int {
	fs := flag.NewFlagSet("lock", flag.ExitOnError)
	profilePath := fs.String("profile", filepath.Join(root, "profile.json"), "profile JSON")
	jdDir := fs.String("jds", filepath.Join(root, "jds"), "folder of job descriptions")
	tailorer := fs.String("tailorer", "rules", "rules or llm")
	_ = fs.Parse(args)
	profile, err := loadProfile(*profilePath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "%s✗ %v%s\n", red, err, reset)
		return 1
	}
	jds, _ := filepath.Glob(filepath.Join(*jdDir, "*.txt"))
	slices.Sort(jds)
	if len(jds) < 2 {
		fmt.Printf("%sneed at least 2 JDs in %s%s\n", red, *jdDir, reset)
		return 1
	}
	ctx, ledger, err := session(context.Background(), "")
	if err != nil {
		fmt.Fprintf(os.Stderr, "%s✗ %v%s\n", red, err, reset)
		return 1
	}
	defer ledger.Close()
	sections := []string{"basics", "experience", "projects", "coding_profiles", "skills", "education"}
	variants := map[string]map[string]bool{}
	for _, jd := range jds {
		text, _ := os.ReadFile(jd)
		var data *resume.Data
		if *tailorer == "llm" {
			data, _ = resume.TailorAI(ctx, profile, string(text), "", 2, 5, 3)
		} else {
			data, _ = resume.Tailor(profile, string(text), 2, 4, 3)
		}
		encoded, _ := json.Marshal(data)
		var fields map[string]json.RawMessage
		_ = json.Unmarshal(encoded, &fields)
		for _, k := range sections {
			sum := sha256.Sum256(fields[k])
			if variants[k] == nil {
				variants[k] = map[string]bool{}
			}
			variants[k][hex.EncodeToString(sum[:])[:12]] = true
		}
	}
	fmt.Printf("%s▸ lock check%s %sacross %d job descriptions%s\n", bold, reset, dim, len(jds), reset)
	failed := false
	for _, k := range sections {
		mark := fmt.Sprintf("%svaries (%d variant(s))%s", dim, len(variants[k]), reset)
		if slices.Contains(profile.Locked.FrozenSections, k) {
			if len(variants[k]) == 1 {
				mark = green + "✓ locked" + reset
			} else {
				mark, failed = red+"✗ LOCK BROKEN — varies by JD"+reset, true
			}
		}
		fmt.Printf("  %-18s%s\n", k, mark)
	}
	if failed {
		fmt.Printf("\n%sA frozen section changed with the job description.%s\n", red, reset)
		return 1
	}
	return 0
}

func cmdCompare(args []string) int {
	fs := flag.NewFlagSet("compare", flag.ExitOnError)
	jdPath := fs.String("jd", "", "job description file")
	profilePath := fs.String("profile", filepath.Join(root, "profile.json"), "profile JSON")
	model := fs.String("model", resume.JDModel, "AI model for the analysis")
	_ = fs.Parse(args)
	profile, err := loadProfile(*profilePath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "%s✗ %v%s\n", red, err, reset)
		return 1
	}
	jd, err := os.ReadFile(*jdPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "%s✗ %v%s\n", red, err, reset)
		return 1
	}
	ctx, ledger, err := session(context.Background(), *model)
	if err != nil {
		fmt.Fprintf(os.Stderr, "%s✗ %v%s\n", red, err, reset)
		return 1
	}
	defer ledger.Close()
	rules := resume.ParseJD(string(jd), profile)
	if !llm.SessionFrom(ctx).HasRoute() {
		fmt.Printf("%sno AI provider — nothing to compare against%s\n", red, reset)
		return 1
	}
	ai, hit, err := resume.ParseJDAI(ctx, string(jd), profile, *model)
	if err != nil {
		fmt.Printf("%s✗ llm failed: %v%s\n", red, err, reset)
		return 1
	}
	cached := ""
	if hit {
		cached = " · cached"
	}
	fmt.Printf("%s▸ %s%s  %s%s%s%s\n", bold, filepath.Base(*jdPath), reset, dim, *model, cached, reset)
	if ai.Summary != "" {
		fmt.Printf("  %sllm reads it as:%s %s\n  %sseniority:%s %s\n", dim, reset, ai.Summary, dim, reset, ai.Seniority)
	}
	fmt.Printf("\n  %-12s%8s%8s   %s(shown where either is non-zero)%s\n", "angle", "rules", "llm", dim, reset)
	angles := []string{"agentic", "backend", "data", "dsa", "eval", "frontend", "infra", "systems", "teaching"}
	for _, a := range angles {
		r, m := rules.AngleScores.Get(a), ai.AngleScores.Get(a)
		if r == 0 && m == 0 {
			continue
		}
		mark := ""
		if r-m >= 0.15 || m-r >= 0.15 {
			mark = yellow + "  ←" + reset
		}
		fmt.Printf("  %-12s%7.0f%%%7.0f%%%s\n", a, r*100, m*100, mark)
	}
	for _, sec := range []struct {
		label        string
		rules, model []string
	}{{"matched skills", rules.MatchedSkills, ai.MatchedSkills}, {"gaps", rules.GapSkills, ai.GapSkills}} {
		both, onlyR, onlyM := split(sec.rules, sec.model)
		fmt.Printf("\n  %s%s%s\n    %sboth       :%s %s\n    %srules only :%s %s\n    %sllm only   :%s %s\n",
			bold, sec.label, reset, dim, reset, both, dim, reset, onlyR, green, reset, onlyM)
	}
	return 0
}

func split(a, b []string) (both, onlyA, onlyB string) {
	var x, y, z []string
	for _, s := range a {
		if slices.Contains(b, s) {
			x = append(x, s)
		} else {
			y = append(y, s)
		}
	}
	for _, s := range b {
		if !slices.Contains(a, s) {
			z = append(z, s)
		}
	}
	join := func(s []string) string {
		slices.Sort(s)
		if len(s) == 0 {
			return "-"
		}
		return strings.Join(s, ", ")
	}
	return join(x), join(y), join(z)
}

func cmdJobs(args []string) int {
	if len(args) != 1 || (args[0] != "refresh" && args[0] != "recheck") {
		fmt.Fprintln(os.Stderr, "usage: resume jobs refresh|recheck")
		return 2
	}
	a, err := app.New(root)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Job refresh failed: %v\n", err)
		return 1
	}
	defer a.Close()
	profile, _, err := a.Store.Profile()
	if err == nil {
		var summary string
		summary, err = a.Jobs.Refresh(context.Background(), profile, args[0] == "recheck", nil, false)
		if err == nil {
			fmt.Println(summary)
			return 0
		}
	}
	fmt.Fprintf(os.Stderr, "Job refresh failed: %v\n", err)
	return 1
}

// ── Interactive mode (main.py) ──

func interactive(args []string) int {
	fs := flag.NewFlagSet("resume", flag.ExitOnError)
	label := fs.String("label", "", "name for the output file")
	tailorer := fs.String("tailorer", "auto", "auto, llm or rules")
	if files := parse(fs, args); len(files) > 0 {
		text, err := os.ReadFile(files[0])
		if err != nil {
			fmt.Printf("%s✗ no such file: %s%s\n", red, files[0], reset)
			return 1
		}
		name := *label
		if name == "" {
			name = strings.TrimSuffix(filepath.Base(files[0]), filepath.Ext(files[0]))
		}
		return generate(string(text), name, *tailorer)
	}
	name, text := readJD()
	if *label != "" {
		name = *label
	}
	return generate(text, name, *tailorer)
}

// clipboard is whatever is on the system clipboard, or "".
func clipboard() string {
	for _, cmd := range [][]string{{"pbpaste"}, {"xclip", "-selection", "clipboard", "-o"}, {"wl-paste"}} {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		out, err := exec.CommandContext(ctx, cmd[0], cmd[1:]...).Output()
		cancel()
		if err == nil && strings.TrimSpace(string(out)) != "" {
			return string(out)
		}
	}
	return ""
}

func prompt(r *bufio.Reader, text string) (string, error) {
	fmt.Print(text)
	line, err := r.ReadString('\n')
	return strings.TrimSpace(line), err
}

// readJD asks for a label first: reading the job text can end at EOF, which
// closes stdin, so nothing could be asked afterwards.
func readJD() (string, string) {
	fmt.Printf("%sJob description -> resume%s\n%s%s%s\n", bold, reset, dim, rule, reset)
	in := bufio.NewReader(os.Stdin)
	clip := clipboard()
	useClip := false
	if len(strings.TrimSpace(clip)) >= 200 {
		preview := strings.Join(strings.Fields(clip), " ")
		fmt.Printf("%sclipboard:%s %s...\n", dim, reset, firstRunes(preview, 64))
		ans, err := prompt(in, fmt.Sprintf("Use the clipboard? %s[Y/n]%s ", dim, reset))
		ans = strings.ToLower(ans)
		useClip = errors.Is(err, io.EOF) || ans == "" || ans == "y" || ans == "yes"
	}
	label, _ := prompt(in, fmt.Sprintf("%sCompany or role%s %s(for the filename)%s: ", bold, reset, dim, reset))
	if useClip {
		fmt.Printf("%susing %d words from the clipboard%s\n", dim, len(strings.Fields(clip)), reset)
		return label, clip
	}
	fmt.Printf("\n%sPaste the job description%s, then press %sEnter%s:\n%s%s%s\n", bold, reset, bold, reset, dim, rule, reset)
	if !term.IsTerminal(int(os.Stdin.Fd())) {
		data, _ := io.ReadAll(in)
		return label, string(data)
	}
	return label, readBlock(in)
}

// readBlock reads pasted text that ends with Enter rather than Ctrl-D: a
// paste arrives in one burst, so its blank lines always have more data right
// behind them, while the final Enter does not.
func readBlock(in *bufio.Reader) string {
	lines := make(chan string)
	go func() {
		for {
			line, err := in.ReadString('\n')
			if line != "" {
				lines <- line
			}
			if err != nil {
				close(lines)
				return
			}
		}
	}()
	var buf strings.Builder
	for {
		if strings.HasSuffix(buf.String(), "\n") && strings.TrimSpace(buf.String()) != "" {
			select {
			case line, ok := <-lines:
				if !ok {
					return buf.String()
				}
				buf.WriteString(line)
			case <-time.After(600 * time.Millisecond):
				return buf.String()
			}
			continue
		}
		line, ok := <-lines
		if !ok {
			return buf.String()
		}
		buf.WriteString(line)
	}
}

func generate(jd, label, tailorer string) int {
	if len(strings.TrimSpace(jd)) < 40 {
		fmt.Printf("\n%s✗ that job description is too short to tailor against.%s\n", red, reset)
		return 1
	}
	// Keep the job text so the run can be reproduced; jds/*.txt is gitignored.
	s := slug(label)
	if s == "" {
		s = "resume"
	}
	name := time.Now().Format(time.DateOnly) + "_" + s
	jdPath := filepath.Join(root, "jds", s+".txt")
	if err := os.MkdirAll(filepath.Dir(jdPath), 0o755); err != nil || os.WriteFile(jdPath, []byte(jd), 0o600) != nil {
		fmt.Printf("%s✗ could not save the job description%s\n", red, reset)
		return 1
	}
	fmt.Printf("%s%s%s\n", dim, rule, reset)
	_, o := makeFlags("generate")
	o.jd, o.out, o.tailorer, o.jobBullets = jdPath, name, tailorer, 5
	code := makeResume(o)
	if pdf := filepath.Join(root, "build", name+".pdf"); runtime.GOOS == "darwin" {
		if _, err := os.Stat(pdf); err == nil {
			_ = exec.Command("open", pdf).Run()
			fmt.Printf("%sopened in your PDF viewer%s\n", dim, reset)
		}
	}
	return code
}

// parse lets positional arguments and flags come in any order, like
// Python's argparse ("check FILE --data X" as well as "check --data X FILE").
func parse(fs *flag.FlagSet, args []string) []string {
	var positional []string
	for {
		_ = fs.Parse(args)
		args = fs.Args()
		if len(args) == 0 {
			return positional
		}
		positional = append(positional, args[0])
		args = args[1:]
	}
}

func firstRunes(s string, n int) string {
	r := []rune(s)
	return string(r[:min(n, len(r))])
}
