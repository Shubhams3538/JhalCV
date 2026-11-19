package jobs

import (
	"html"
	"net/url"
	"strings"
)

// HTML to text. The Python app uses regular expressions here; Go's regexp
// engine guarantees linear time but is slower than Python's C engine on
// these patterns, so each one is a small hand-written scanner that matches
// exactly what its regex matches (htmltext_test.go checks them against the
// original regexes). Every scan is a single forward pass, so a crafted page
// cannot pin a CPU.

var blockNames = []string{"script", "style", "noscript", "svg", "template"}

func isSpace(c byte) bool {
	return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v'
}

func isWordChar(c byte) bool {
	return c == '_' || c >= '0' && c <= '9' || c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z'
}

// hasFoldPrefix reports whether s[i:] starts with lower (ASCII, any case).
func hasFoldPrefix(s string, i int, lower string) bool {
	if len(s)-i < len(lower) {
		return false
	}
	for j := 0; j < len(lower); j++ {
		c := s[i+j]
		if c >= 'A' && c <= 'Z' {
			c += 'a' - 'A'
		}
		if c != lower[j] {
			return false
		}
	}
	return true
}

// boundaryAt is regexp \b after a word character: the next byte is not one.
func boundaryAt(s string, i int) bool {
	return i >= len(s) || !isWordChar(s[i])
}

// tagEnd returns the index just past the '>' that ends a tag whose name
// ended at i ([^>]*>), or -1.
func tagEnd(s string, i int) int {
	j := strings.IndexByte(s[i:], '>')
	if j < 0 {
		return -1
	}
	return i + j + 1
}

// openBlock matches (?i)<(script|style|noscript|svg|template)\b[^>]*> at i.
func openBlock(s string, i int) (name string, end int) {
	for _, n := range blockNames {
		if hasFoldPrefix(s, i+1, n) && boundaryAt(s, i+1+len(n)) {
			if end := tagEnd(s, i+1+len(n)); end >= 0 {
				return n, end
			}
			return "", -1
		}
	}
	return "", -1
}

// closeTag finds (?i)</name\s*> from i: its start and end, or -1.
func closeTag(s string, i int, name string) (int, int) {
	for {
		j := strings.Index(s[i:], "</")
		if j < 0 {
			return -1, -1
		}
		start := i + j
		k := start + 2
		if hasFoldPrefix(s, k, name) {
			k += len(name)
			for k < len(s) && isSpace(s[k]) {
				k++
			}
			if k < len(s) && s[k] == '>' {
				return start, k + 1
			}
		}
		i = start + 1
	}
}

// stripBlocks drops script, style, noscript, svg and template elements. An
// element with no closing tag keeps its content.
func stripBlocks(s string) string {
	var out strings.Builder
	pos := 0
	for i := 0; i < len(s); {
		j := strings.IndexByte(s[i:], '<')
		if j < 0 {
			break
		}
		at := i + j
		name, end := openBlock(s, at)
		if end < 0 {
			i = at + 1
			continue
		}
		if _, closeEnd := closeTag(s, end, name); closeEnd >= 0 {
			out.WriteString(s[pos:at])
			pos = closeEnd
			i = closeEnd
		} else {
			i = end
		}
	}
	if pos == 0 {
		return s
	}
	out.WriteString(s[pos:])
	return out.String()
}

var breakNames = []string{"br", "/p", "/div", "/li", "/h", "/tr", "li"}

// lineBreak matches (?i)<\s*(br|/p|/div|/li|/h[1-6]|/tr|li)\b[^>]*> at i.
func lineBreak(s string, i int) int {
	k := i + 1
	for k < len(s) && isSpace(s[k]) {
		k++
	}
	for _, n := range breakNames {
		if !hasFoldPrefix(s, k, n) {
			continue
		}
		e := k + len(n)
		if n == "/h" {
			if e >= len(s) || s[e] < '1' || s[e] > '6' {
				continue
			}
			e++
		}
		if boundaryAt(s, e) {
			return tagEnd(s, e) // [^>]*> cannot fail differently for another name.
		}
	}
	return -1
}

func replaceBreaks(s string) string {
	var out strings.Builder
	out.Grow(len(s))
	pos := 0
	for i := 0; i < len(s); {
		j := strings.IndexByte(s[i:], '<')
		if j < 0 {
			break
		}
		at := i + j
		if end := lineBreak(s, at); end >= 0 {
			out.WriteString(s[pos:at])
			out.WriteByte('\n')
			pos, i = end, end
		} else {
			i = at + 1
		}
	}
	out.WriteString(s[pos:])
	return out.String()
}

// stripTags replaces every <...> with a space. No tag can cross the last
// '>' in the text, like the Python version.
func stripTags(s string) string {
	last := strings.LastIndexByte(s, '>')
	if last < 0 {
		return s
	}
	var out strings.Builder
	out.Grow(len(s))
	pos := 0
	for i := 0; i <= last; {
		j := strings.IndexByte(s[i:last+1], '<')
		if j < 0 {
			break
		}
		at := i + j
		k := strings.IndexByte(s[at+1:last+1], '>')
		if k <= 0 { // "<>" is not a tag ([^>]+ needs one character).
			i = at + 1
			continue
		}
		out.WriteString(s[pos:at])
		out.WriteByte(' ')
		pos = at + 1 + k + 1
		i = pos
	}
	out.WriteString(s[pos:])
	return out.String()
}

// HTMLToText turns HTML into readable plain text, keeping line structure.
func HTMLToText(value string) string {
	if value == "" {
		return ""
	}
	value = html.UnescapeString(stripTags(replaceBreaks(stripBlocks(value))))
	var b strings.Builder
	for _, line := range splitLines(value) {
		fields := strings.Fields(line)
		if len(fields) == 0 {
			continue
		}
		if b.Len() > 0 {
			b.WriteByte('\n')
		}
		b.WriteString(strings.Join(fields, " "))
	}
	return strings.TrimSpace(b.String())
}

// splitLines splits like Python's str.splitlines.
func splitLines(s string) []string {
	return strings.FieldsFunc(strings.ReplaceAll(s, "\r\n", "\n"), func(r rune) bool {
		switch r {
		case '\n', '\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', ' ', ' ':
			return true
		}
		return false
	})
}

// anchor matches (?i)<a\b[^>]*?href\s*=\s*["']([^"']+)["'][^>]*> at i and
// returns the href value's bounds and the tag's end.
func anchor(s string, i int) (hrefStart, hrefEnd, end int) {
	if !hasFoldPrefix(s, i+1, "a") || !boundaryAt(s, i+2) {
		return -1, -1, -1
	}
	for k := i + 2; k < len(s) && s[k] != '>'; k++ {
		if !hasFoldPrefix(s, k, "href") {
			continue
		}
		p := k + 4
		for p < len(s) && isSpace(s[p]) {
			p++
		}
		if p >= len(s) || s[p] != '=' {
			continue
		}
		p++
		for p < len(s) && isSpace(s[p]) {
			p++
		}
		if p >= len(s) || (s[p] != '"' && s[p] != '\'') {
			continue
		}
		valueStart := p + 1
		q := strings.IndexAny(s[valueStart:], `"'`)
		if q <= 0 {
			continue
		}
		valueEnd := valueStart + q
		if e := tagEnd(s, valueEnd+1); e >= 0 {
			return valueStart, valueEnd, e
		}
		return -1, -1, -1
	}
	return -1, -1, -1
}

// PageWithLinks is page text in which every link keeps its absolute URL, as
// "[link: URL]", plus the set of those URLs.
func PageWithLinks(value, base string) (string, map[string]bool) {
	links := map[string]bool{}
	baseURL, _ := url.Parse(base)
	value = stripBlocks(value)
	var out strings.Builder
	pos := 0
	for i := 0; i < len(value); {
		j := strings.IndexByte(value[i:], '<')
		if j < 0 {
			break
		}
		at := i + j
		hs, he, end := anchor(value, at)
		if end < 0 {
			i = at + 1
			continue
		}
		closeStart, closeEnd := closeTag(value, end, "a")
		if closeStart < 0 {
			i = end
			continue
		}
		href := strings.TrimSpace(html.UnescapeString(value[hs:he]))
		skip := false
		for _, p := range []string{"#", "javascript:", "mailto:", "tel:", "data:"} {
			if strings.HasPrefix(href, p) {
				skip = true
			}
		}
		if skip {
			i = end
			continue
		}
		absolute := href
		if ref, err := url.Parse(href); err == nil && baseURL != nil {
			absolute = baseURL.ResolveReference(ref).String()
		}
		links[absolute] = true
		// Like Python's finditer, the next anchor is searched from this
		// tag's end, so an anchor nested in the link text is read too.
		if at > pos {
			out.WriteString(value[pos:at])
		}
		out.WriteString(value[end:closeStart] + " [link: " + absolute + "] ")
		pos, i = closeEnd, end
	}
	if pos < len(value) {
		out.WriteString(value[pos:])
	}
	return HTMLToText(out.String()), links
}
