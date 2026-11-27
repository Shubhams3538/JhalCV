package resume

import (
	"context"
	"fmt"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	"jhalcv/internal/storage"
)

// RenderError is a failure to compile or measure the PDF.
type RenderError struct{ Message string }

func (e *RenderError) Error() string { return e.Message }

// presets are typography settings, loosest first. The fitter tightens the
// layout before it drops any content: losing a little leading is always
// better than losing an achievement.
var presets = []map[string]string{
	{},
	{"leading": "0.62em", "bullet_gap": "0.72em", "entry_gap": "10pt", "section_gap": "12pt", "rule_gap": "7pt", "head_gap": "5.5pt"},
	{"leading": "0.58em", "bullet_gap": "0.68em", "entry_gap": "9pt", "section_gap": "10pt",
		"body_size": "9.8pt", "margin_y": "0.45in", "head_gap": "5pt"},
	{"leading": "0.55em", "bullet_gap": "0.64em", "entry_gap": "8pt", "section_gap": "9pt",
		"body_size": "9.6pt", "margin_y": "0.42in", "margin_x": "0.5in", "name_size": "18pt",
		"head_gap": "4.5pt"},
	{"leading": "0.52em", "bullet_gap": "0.6em", "entry_gap": "7pt", "section_gap": "8pt",
		"body_size": "9.4pt", "margin_y": "0.38in", "margin_x": "0.45in", "name_size": "17.5pt",
		"section_size": "10pt", "contact_size": "9.2pt", "rule_gap": "5pt", "head_gap": "4pt"},
}

var length = regexp.MustCompile(`^([\d.]+)(pt|in|mm|em)$`)

// typstLength turns "9.6pt" into {__len, __unit}: Typst cannot read a length
// from JSON text, so the template rebuilds it from these two parts.
func typstLength(v string) any {
	m := length.FindStringSubmatch(v)
	if m == nil {
		return v
	}
	n, err := strconv.ParseFloat(m[1], 64)
	if err != nil {
		return v
	}
	return map[string]any{"__len": n, "__unit": m[2]}
}

// Renderer compiles resume data with Typst.
type Renderer struct {
	Root     string // Project folder: Typst's --root, holding templates/ and fonts/.
	Template string
}

// NewRenderer uses root/templates/classic.typ.
func NewRenderer(root string) *Renderer {
	return &Renderer{Root: root, Template: filepath.Join(root, "templates", "classic.typ")}
}

// Compile writes data to dataPath and compiles it into outPDF.
func (r *Renderer) Compile(ctx context.Context, data *Data, outPDF, dataPath string) error {
	if err := storage.WriteJSON(dataPath, data.normalized()); err != nil { // Personal details: 0600.
		return err
	}
	rel, err := filepath.Rel(r.Root, dataPath)
	if err != nil || strings.HasPrefix(rel, "..") {
		return &RenderError{"the data file must be inside the project folder"}
	}
	cmd := exec.CommandContext(ctx, "typst", "compile", "--root", r.Root,
		"--font-path", filepath.Join(r.Root, "fonts"),
		"--input", "data=/"+filepath.ToSlash(rel), r.Template, outPDF)
	out, err := cmd.CombinedOutput()
	if err != nil {
		msg := strings.TrimSpace(string(out))
		if msg == "" {
			msg = err.Error()
		}
		return &RenderError{msg}
	}
	return nil
}

var pagesLine = regexp.MustCompile(`(?m)^Pages:\s+(\d+)`)

// PageCount asks pdfinfo how many pages a PDF has.
func PageCount(ctx context.Context, pdf string) (int, error) {
	out, err := exec.CommandContext(ctx, "pdfinfo", pdf).Output()
	if err != nil {
		return 0, &RenderError{fmt.Sprintf("pdfinfo failed: %v", err)}
	}
	m := pagesLine.FindSubmatch(out)
	if m == nil {
		return 0, &RenderError{"could not read page count"}
	}
	return strconv.Atoi(string(m[1]))
}

// FitResult describes how the page was made to fit.
type FitResult struct {
	Pages   int              `json:"pages"`
	Log     []map[string]any `json:"log"`
	Dropped []map[string]any `json:"dropped"`
	Warning string           `json:"warning,omitempty"`
}

// Fit compiles data and, while it overflows maxPages, tightens the
// typography, then drops the lowest-scoring bullets one at a time.
func (r *Renderer) Fit(ctx context.Context, data *Data, outPDF, dataPath string, maxPages int) (*FitResult, error) {
	result := &FitResult{Log: []map[string]any{}, Dropped: []map[string]any{}}
	for i, preset := range presets {
		config := map[string]any{}
		for k, v := range preset {
			config[k] = typstLength(v)
		}
		data.Config = config
		if err := r.Compile(ctx, data, outPDF, dataPath); err != nil {
			return nil, err
		}
		pages, err := PageCount(ctx, outPDF)
		if err != nil {
			return nil, err
		}
		result.Log = append(result.Log, map[string]any{"step": fmt.Sprintf("preset %d", i), "pages": pages, "config": preset})
		if pages <= maxPages {
			result.Pages = pages
			return result, nil
		}
	}

	// Still overflowing at the tightest typography: drop the globally
	// least-relevant bullet, one at a time.
	for range 20 {
		entry, index, score, ok := weakestBullet(data)
		if !ok {
			break
		}
		meta := (*entry.meta)[index]
		text := (*entry.bullets)[index]
		*entry.bullets = append((*entry.bullets)[:index:index], (*entry.bullets)[index+1:]...)
		*entry.meta = append((*entry.meta)[:index:index], (*entry.meta)[index+1:]...)
		result.Dropped = append(result.Dropped, map[string]any{"id": meta.ID, "score": score, "text": text})
		if err := r.Compile(ctx, data, outPDF, dataPath); err != nil {
			return nil, err
		}
		pages, err := PageCount(ctx, outPDF)
		if err != nil {
			return nil, err
		}
		result.Log = append(result.Log, map[string]any{"step": "drop " + meta.ID, "pages": pages})
		if pages <= maxPages {
			result.Pages = pages
			return result, nil
		}
	}
	pages, err := PageCount(ctx, outPDF)
	if err != nil {
		return nil, err
	}
	result.Pages = pages
	result.Warning = fmt.Sprintf("could not fit into %d page(s)", maxPages)
	return result, nil
}

type bulletList struct {
	bullets *[]string
	meta    *[]BulletMeta
}

// weakestBullet finds the lowest-scoring droppable bullet. Entries with a
// frozen bullet (nil score) are never trimmed, and no entry goes below two.
func weakestBullet(data *Data) (bulletList, int, float64, bool) {
	var lists []bulletList
	for i := range data.Experience {
		lists = append(lists, bulletList{&data.Experience[i].Bullets, &data.Experience[i].BulletMeta})
	}
	for i := range data.Projects {
		lists = append(lists, bulletList{&data.Projects[i].Bullets, &data.Projects[i].BulletMeta})
	}
	var best bulletList
	bestIndex, bestScore, found := 0, 0.0, false
	for _, l := range lists {
		frozen := false
		for _, m := range *l.meta {
			if m.Score == nil {
				frozen = true
			}
		}
		if frozen || len(*l.bullets) <= 2 {
			continue
		}
		for i, m := range *l.meta {
			if !found || *m.Score < bestScore {
				best, bestIndex, bestScore, found = l, i, *m.Score, true
			}
		}
	}
	return best, bestIndex, bestScore, found
}
