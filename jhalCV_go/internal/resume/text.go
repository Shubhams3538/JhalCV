// Package resume tailors a profile to a job description and renders the
// result as a one-page PDF. It holds the Go versions of resume/tailor.py,
// llm_tailor.py, combined_tailor.py, descriptions.py, skill_swap.py,
// insights.py, render.py, verify.py and workspace.py.
package resume

import (
	"encoding/json"
	"math"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"
)

// termPresent reports whether term appears in blob as a whole word: the
// characters on either side must not be ASCII letters or digits. This keeps
// "c++", "node.js" and "ci/cd" matchable, unlike a regex \b. (Python uses a
// lookbehind for this; Go's regexp has none, so it is checked by hand.)
// blob must already be lowercase.
func termPresent(term, blob string) bool {
	t := strings.ToLower(term)
	for offset := 0; offset <= len(blob); {
		i := strings.Index(blob[offset:], t)
		if i < 0 {
			return false
		}
		start, end := offset+i, offset+i+len(t)
		if (start == 0 || !wordByte(blob[start-1])) && (end >= len(blob) || !wordByte(blob[end])) {
			return true
		}
		offset = start + 1
	}
	return false
}

func wordByte(c byte) bool {
	return c >= 'a' && c <= 'z' || c >= '0' && c <= '9'
}

// round matches Python's round() to n decimal places (ties to even).
func round(x float64, n int) float64 {
	p := math.Pow(10, float64(n))
	return math.RoundToEven(x*p) / p
}

var (
	numbers = regexp.MustCompile(`\d+(?:[.,]\d+)*(?:%|x|ms|s|k|K|M)?`)
	bold    = regexp.MustCompile(`\*\*(.+?)\*\*`)
)

// Budget is the size of an original bullet, which a rewrite may not exceed.
type Budget struct {
	Chars int `json:"chars"`
	Words int `json:"words"`
}

// BudgetOf measures a bullet.
func BudgetOf(value string) Budget {
	return Budget{utf8.RuneCountInString(value), len(strings.Fields(value))}
}

// Within reports whether a revised bullet stays inside the original's
// character and word budget and adds no new numbers.
func Within(value, original string) bool {
	if strings.TrimSpace(value) == "" || strings.ContainsAny(value, "\n\r") {
		return false
	}
	if utf8.RuneCountInString(value) > utf8.RuneCountInString(original) ||
		len(strings.Fields(value)) > len(strings.Fields(original)) {
		return false
	}
	allowed := map[string]bool{}
	for _, n := range numbers.FindAllString(original, -1) {
		allowed[n] = true
	}
	for _, n := range numbers.FindAllString(value, -1) {
		if !allowed[n] {
			return false
		}
	}
	return true
}

// ValidRewrite is Within, plus every **bold** phrase kept unchanged and in
// order: bold text is the user's own emphasis.
func ValidRewrite(value, original string) bool {
	if !Within(value, original) || strings.Count(value, "**") != strings.Count(original, "**") {
		return false
	}
	a, b := bold.FindAllStringSubmatch(value, -1), bold.FindAllStringSubmatch(original, -1)
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i][1] != b[i][1] {
			return false
		}
	}
	return true
}

// clone deep-copies any JSON-shaped value.
func clone[T any](v T) T {
	data, err := json.Marshal(v)
	if err != nil {
		panic(err) // Only JSON-safe types are cloned; this is a bug.
	}
	var out T
	if err := json.Unmarshal(data, &out); err != nil {
		panic(err)
	}
	return out
}

// isWordRune matches Python's \w.
func isWordRune(r rune) bool {
	return r == '_' || unicode.IsLetter(r) || unicode.IsDigit(r)
}
