package jobs

import (
	"html"
	"math/rand/v2"
	"net/url"
	"os"
	"regexp"
	"strings"
	"testing"
)

// The original regular expressions from resume/jobs/fetch.py. The scanners
// in htmltext.go must produce exactly what these produce.
var (
	refBlockOpen  = regexp.MustCompile(`(?i)<(script|style|noscript|svg|template)\b[^>]*>`)
	refAnchorOpen = regexp.MustCompile(`(?i)<a\b[^>]*?href\s*=\s*["']([^"']+)["'][^>]*>`)
	refAnchorEnd  = regexp.MustCompile(`(?i)</a\s*>`)
	refBreaks     = regexp.MustCompile(`(?i)<\s*(br|/p|/div|/li|/h[1-6]|/tr|li)\b[^>]*>`)
	refTag        = regexp.MustCompile(`<[^>]+>`)
)

func refStripBlocks(value string) string {
	var out strings.Builder
	pos := 0
	for _, m := range refBlockOpen.FindAllStringSubmatchIndex(value, -1) {
		if m[0] < pos {
			continue
		}
		closer := regexp.MustCompile(`(?i)</` + strings.ToLower(value[m[2]:m[3]]) + `\s*>`)
		loc := closer.FindStringIndex(value[m[1]:])
		if loc == nil {
			continue
		}
		out.WriteString(value[pos:m[0]])
		pos = m[1] + loc[1]
	}
	out.WriteString(value[pos:])
	return out.String()
}

func refHTMLToText(value string) string {
	if value == "" {
		return ""
	}
	value = refStripBlocks(value)
	value = refBreaks.ReplaceAllString(value, "\n")
	if last := strings.LastIndex(value, ">"); last >= 0 {
		value = refTag.ReplaceAllString(value[:last+1], " ") + value[last+1:]
	}
	value = html.UnescapeString(value)
	var lines []string
	for _, line := range splitLines(value) {
		if line = strings.Join(strings.Fields(line), " "); line != "" {
			lines = append(lines, line)
		}
	}
	return strings.TrimSpace(strings.Join(lines, "\n"))
}

func refPageWithLinks(value, base string) (string, int) {
	links := map[string]bool{}
	baseURL, _ := url.Parse(base)
	value = refStripBlocks(value)
	var out strings.Builder
	pos := 0
	for _, m := range refAnchorOpen.FindAllStringSubmatchIndex(value, -1) {
		loc := refAnchorEnd.FindStringIndex(value[m[1]:])
		if loc == nil {
			continue
		}
		href := strings.TrimSpace(html.UnescapeString(value[m[2]:m[3]]))
		if strings.HasPrefix(href, "#") || strings.HasPrefix(href, "javascript:") || strings.HasPrefix(href, "mailto:") ||
			strings.HasPrefix(href, "tel:") || strings.HasPrefix(href, "data:") {
			continue
		}
		absolute := href
		if ref, err := url.Parse(href); err == nil {
			absolute = baseURL.ResolveReference(ref).String()
		}
		links[absolute] = true
		if m[0] > pos {
			out.WriteString(value[pos:m[0]])
		}
		out.WriteString(value[m[1]:m[1]+loc[0]] + " [link: " + absolute + "] ")
		pos = m[1] + loc[1]
	}
	if pos < len(value) {
		out.WriteString(value[pos:])
	}
	return refHTMLToText(out.String()), len(links)
}

// randomHTML builds tag soup from pieces that exercise every edge: case,
// spacing, missing closers, nested anchors, quotes and stray brackets.
func randomHTML(r *rand.Rand) string {
	pieces := []string{"<script>x</script>", "<SCRIPT type=a>y", "</script >", "<scripts>", "<style>", "</STYLE>",
		"<svg><g/></svg>", "<template x>", "</template\t>", "<noscript>n</noscript>", "<br>", "<BR/>", "< br >",
		"<brx>", "</p>", "</P >", "</div>", "<li>", "</li>", "<LIx>", "</h1>", "</h7>", "</h3 class=x>", "</tr>",
		`<a href="/jobs/1">One</a>`, `<A HREF = '/x'>Two</A >`, `<a class=b href="#top">Top</a>`, `<a href="x>y" id=1>Odd</a>`,
		`<a href="mailto:a@b.c">Mail</a>`, `<ab href="/no">No</ab>`, `<a href="/n1">outer <a href="/n2">inner</a> tail</a>`,
		`<a href=noquote>bare</a>`, `<a href="">empty</a>`, "<>", "< >", "<", ">", "a < b > c", "&amp;", "&lt;tag&gt;",
		"text", " ", "\n", "\r\n", "\t", "word", "é", "日本"}
	var b strings.Builder
	for range 1 + r.IntN(40) {
		b.WriteString(pieces[r.IntN(len(pieces))])
	}
	return b.String()
}

func TestScannersMatchTheOriginalRegexes(t *testing.T) {
	r := rand.New(rand.NewPCG(1, 2))
	for i := range 20000 {
		page := randomHTML(r)
		if got, want := HTMLToText(page), refHTMLToText(page); got != want {
			t.Fatalf("case %d HTMLToText(%q)\n got %q\nwant %q", i, page, got, want)
		}
		got, links := PageWithLinks(page, "https://example.com/careers/")
		want, wantLinks := refPageWithLinks(page, "https://example.com/careers/")
		if got != want || len(links) != wantLinks {
			t.Fatalf("case %d PageWithLinks(%q)\n got %q (%d links)\nwant %q (%d links)", i, page, got, len(links), want, wantLinks)
		}
	}
}

func TestScannersMatchOnTheBenchmarkPage(t *testing.T) {
	dir := os.Getenv("JHALCV_FIXTURES")
	if dir == "" {
		t.Skip("set JHALCV_FIXTURES to also check the large fixture page")
	}
	data, err := os.ReadFile(dir + "/page.html")
	if err != nil {
		t.Fatal(err)
	}
	page := string(data)
	if HTMLToText(page) != refHTMLToText(page) {
		t.Error("HTMLToText differs on the fixture page")
	}
	got, _ := PageWithLinks(page, "https://example.com/careers")
	want, _ := refPageWithLinks(page, "https://example.com/careers")
	if got != want {
		t.Error("PageWithLinks differs on the fixture page")
	}
}
