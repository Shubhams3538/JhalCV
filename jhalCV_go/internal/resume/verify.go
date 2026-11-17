package resume

import (
	"context"
	"fmt"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
)

// Check is one failed verification of the finished PDF.
type Check struct {
	Check  string `json:"check"`
	Detail string `json:"detail"`
}

// extractText reads a PDF's text layer with pdftotext.
func extractText(ctx context.Context, pdf string, layout bool) (string, error) {
	args := []string{pdf, "-"}
	if layout {
		args = append([]string{"-layout"}, args...)
	}
	out, err := exec.CommandContext(ctx, "pdftotext", args...).Output()
	if err != nil {
		return "", fmt.Errorf("pdftotext failed: %w", err)
	}
	return string(out), nil
}

var whitespace = regexp.MustCompile(`\s+`)

// norm collapses whitespace so line wrapping causes no false misses.
func norm(s string) string {
	return strings.ToLower(strings.TrimSpace(whitespace.ReplaceAllString(s, " ")))
}

var (
	// An ALL-CAPS run broken by a lowercase ligature: "CODING PROfiLES".
	ligature = regexp.MustCompile(`\b[A-Z]{2,}(?:fi|fl|ff|ffi)[A-Z]*\b`)
	// Lowercase right before uppercase inside a word: "FundamentalsData".
	glued      = regexp.MustCompile(`\b[a-zA-Z]*[a-z][A-Z][a-zA-Z]*\b`)
	wideGap    = regexp.MustCompile(`\S {6,}\S`)
	nonDigit   = regexp.MustCompile(`\D`)
	gluedAllow = setOf(strings.Fields(`leetcode codeforces nxtwave github linkedin javascript
		postgresql mysql mongodb nodejs node.js dbms cgpa nextwave dsa oop cdn aws api apis
		openai typescript pgvector hnsw drizzle gpt fastapi mcp graphql postgres nextjs
		websockets websocket aisarthi zod sdks`))
)

// StructuralChecks need no knowledge of what the PDF should say, so they
// work on any resume PDF: they catch what an applicant tracking system
// would choke on.
func StructuralChecks(ctx context.Context, pdf string, maxPages int) ([]Check, error) {
	raw, err := extractText(ctx, pdf, false)
	if err != nil {
		return nil, err
	}
	flat := norm(raw)
	fails := []Check{}
	if n := len([]rune(flat)); n <= 200 {
		fails = append(fails, Check{"text-layer", fmt.Sprintf("extracted only %d chars — PDF may be image-only", n)})
	}
	for _, m := range ligature.FindAllString(raw, -1) {
		fails = append(fails, Check{"ligature", fmt.Sprintf("'%s' — ligature glyph lacks a Unicode mapping", m)})
	}
	for _, w := range glued.FindAllString(raw, -1) {
		if !gluedAllow[strings.ToLower(w)] {
			fails = append(fails, Check{"glued-words", fmt.Sprintf("'%s' — two words may have run together", w)})
		}
	}

	layout, err := extractText(ctx, pdf, true)
	if err != nil {
		return nil, err
	}
	suspicious, body := 0, 0
	for _, line := range strings.Split(layout, "\n") {
		if wideGap.MatchString(line) {
			suspicious++
		}
		if strings.TrimSpace(line) != "" {
			body++
		}
	}
	if body > 0 && float64(suspicious)/float64(body) > 0.55 {
		fails = append(fails, Check{"columns", fmt.Sprintf("%d/%d lines have wide interior gaps — possible multi-column layout", suspicious, body)})
	}

	pages := -1
	if out, err := exec.CommandContext(ctx, "pdfinfo", pdf).Output(); err == nil {
		if m := pagesLine.FindSubmatch(out); m != nil {
			pages, _ = strconv.Atoi(string(m[1]))
		}
	}
	if pages <= 0 || pages > maxPages {
		fails = append(fails, Check{"page-count", fmt.Sprintf("%d pages, budget is %d", pages, maxPages)})
	}
	return fails, nil
}

// Verify runs the structural checks plus content checks: every name, contact
// detail, bullet and skill must survive text extraction intact.
func Verify(ctx context.Context, pdf string, data *Data, maxPages int) ([]Check, error) {
	raw, err := extractText(ctx, pdf, false)
	if err != nil {
		return nil, err
	}
	flat := norm(raw)
	fails, err := StructuralChecks(ctx, pdf, maxPages)
	if err != nil {
		return nil, err
	}
	check := func(ok bool, name, detail string) {
		if !ok {
			fails = append(fails, Check{name, detail})
		}
	}
	b := data.Basics
	check(strings.Contains(flat, norm(b.Name)), "name", fmt.Sprintf("'%s' not found in text layer", b.Name))
	check(strings.Contains(flat, norm(b.Email)), "email", fmt.Sprintf("'%s' not found", b.Email))
	check(strings.Contains(nonDigit.ReplaceAllString(raw, ""), nonDigit.ReplaceAllString(b.Phone, "")), "phone", "phone digits not found")
	for _, l := range b.Links {
		check(strings.Contains(flat, norm(l.Label)), "link-url", fmt.Sprintf("link text '%s' missing — ATS gets no URL", l.Label))
	}
	for _, e := range data.Experience {
		check(strings.Contains(flat, norm(e.Org)), "employer", fmt.Sprintf("'%s' not found", e.Org))
	}
	for _, p := range data.Projects {
		check(strings.Contains(flat, norm(p.Name)), "project", fmt.Sprintf("'%s' not found", p.Name))
	}
	// The first 45 characters prove a bullet was not dropped or mangled.
	var bullets []string
	for _, e := range data.Experience {
		bullets = append(bullets, e.Bullets...)
	}
	for _, p := range data.Projects {
		bullets = append(bullets, p.Bullets...)
	}
	for _, bullet := range bullets {
		probe := []rune(norm(strings.ReplaceAll(bullet, "**", "")))
		probe = probe[:min(45, len(probe))]
		short := []rune(bullet)
		check(strings.Contains(flat, string(probe)), "bullet-intact", fmt.Sprintf("missing/garbled: '%s...'", string(short[:min(50, len(short))])))
	}
	for _, g := range data.Skills {
		check(strings.Contains(flat, norm(g.Label+":")), "skill-label", fmt.Sprintf("'%s:' not intact — label may be glued to its values", g.Label))
		for _, item := range g.Items {
			check(strings.Contains(flat, norm(item)), "skill-item", fmt.Sprintf("skill '%s' missing", item))
		}
	}
	return fails, nil
}

// blocking are the failures that stop an export; glued words and columns are
// warnings only.
func blocking(checks []Check) []Check {
	var out []Check
	for _, c := range checks {
		if c.Check != "glued-words" && c.Check != "columns" {
			out = append(out, c)
		}
	}
	return out
}
