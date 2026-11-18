package jobs

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"io"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Fetch limits for career sites: public pages only, bounded in size and time.
const (
	MaxBytes      = 8_000_000
	MaxJSONBytes  = 60_000_000 // Whole job boards with descriptions can be large.
	fetchTimeout  = 12 * time.Second
	queueTimeout  = 600 * time.Second // Longest wait for a shared browser or AI slot.
	renderTimeout = 80 * time.Second
	userAgent     = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

// FetchError is a career site that could not be read. Its message is shown
// to the user as the company's last error.
type FetchError struct{ Message string }

func (e *FetchError) Error() string { return e.Message }

func fetchErr(format string, args ...any) error {
	return &FetchError{fmt.Sprintf(format, args...)}
}

// IsFetchError reports whether err is a FetchError.
func IsFetchError(err error) bool {
	var f *FetchError
	return errors.As(err, &f)
}

// ── Time budget: each company gets a deadline; queue waits do not count ──

type budget struct {
	mu sync.Mutex
	at time.Time
}

type budgetKey struct{}

// WithBudget gives ctx a company time budget ending at deadline. A zero
// deadline removes any budget.
func WithBudget(ctx context.Context, deadline time.Time) context.Context {
	if deadline.IsZero() {
		return context.WithValue(ctx, budgetKey{}, (*budget)(nil))
	}
	return context.WithValue(ctx, budgetKey{}, &budget{at: deadline})
}

func budgetOf(ctx context.Context) *budget {
	b, _ := ctx.Value(budgetKey{}).(*budget)
	return b
}

// Deadline returns the company deadline in ctx, or zero.
func Deadline(ctx context.Context) time.Time {
	if b := budgetOf(ctx); b != nil {
		b.mu.Lock()
		defer b.mu.Unlock()
		return b.at
	}
	return time.Time{}
}

// Remaining is the time left for the next step, capped at limit, or an error
// once the company's budget is spent.
func Remaining(ctx context.Context, limit time.Duration) (time.Duration, error) {
	seconds := limit
	if b := budgetOf(ctx); b != nil {
		b.mu.Lock()
		seconds = min(limit, time.Until(b.at))
		b.mu.Unlock()
	}
	if seconds <= 0 {
		return 0, fetchErr("Company time budget reached; remaining work will retry next refresh.")
	}
	return max(10*time.Millisecond, seconds), nil
}

// Slots is a counting semaphore for shared resources (browsers, AI).
type Slots chan struct{}

// NewSlots allows n holders at once.
func NewSlots(n int) Slots { return make(Slots, n) }

// Acquire waits for a slot without charging the wait to the company's budget:
// counting queue time made companies fail before doing any work.
func (s Slots) Acquire(ctx context.Context, busy, stage string) error {
	started := time.Now()
	end := Measure(ctx, stage)
	timer := time.NewTimer(queueTimeout)
	defer timer.Stop()
	var err error
	select {
	case s <- struct{}{}:
	case <-timer.C:
		err = fetchErr("%s", busy)
	case <-ctx.Done():
		err = ctx.Err()
	}
	end(err != nil)
	if b := budgetOf(ctx); b != nil {
		b.mu.Lock()
		b.at = b.at.Add(time.Since(started))
		b.mu.Unlock()
	}
	return err
}

// Release frees a slot.
func (s Slots) Release() { <-s }

// ── Public-URL guard ──

type dnsVerdict struct {
	public bool
	until  time.Time
}

var (
	dnsMu    sync.Mutex
	dnsCache = map[string]dnsVerdict{}
)

// resolvesPublic reports whether every address host answers with is public.
// Cached for five minutes; a DNS failure fails open (the fetch must connect).
func resolvesPublic(ctx context.Context, host string) bool {
	dnsMu.Lock()
	cached, ok := dnsCache[host]
	dnsMu.Unlock()
	if ok && cached.until.After(time.Now()) {
		return cached.public
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	addrs, err := net.DefaultResolver.LookupNetIP(ctx, "ip", host)
	if err != nil {
		return true
	}
	public := true
	for _, a := range addrs {
		if !isGlobal(a) {
			public = false
			break
		}
	}
	dnsMu.Lock()
	dnsCache[host] = dnsVerdict{public, time.Now().Add(5 * time.Minute)}
	dnsMu.Unlock()
	return public
}

// isGlobal matches Python's ip_address(...).is_global closely enough for a
// guard: no loopback, private, link-local, unspecified or shared ranges.
func isGlobal(a netip.Addr) bool {
	a = a.Unmap()
	if a.IsLoopback() || a.IsPrivate() || a.IsLinkLocalUnicast() || a.IsLinkLocalMulticast() ||
		a.IsUnspecified() || a.IsMulticast() || a.IsInterfaceLocalMulticast() {
		return false
	}
	for _, p := range nonGlobal {
		if p.Contains(a) {
			return false
		}
	}
	return true
}

var nonGlobal = func() []netip.Prefix {
	var out []netip.Prefix
	for _, s := range []string{"0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24", "198.18.0.0/15",
		"198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4", "255.255.255.255/32", "64:ff9b:1::/48",
		"100::/64", "2001::/23", "2001:db8::/32", "fc00::/7"} {
		out = append(out, netip.MustParsePrefix(s))
	}
	return out
}()

// CheckURL accepts only public http(s) URLs: never localhost or a private
// address. It returns the normalized URL.
func CheckURL(ctx context.Context, raw string) (string, error) {
	if len(raw) > 3000 {
		return "", fetchErr("Paste a careers page URL.")
	}
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" || u.User != nil {
		return "", fetchErr("Use a full http(s) careers page URL.")
	}
	host := strings.ToLower(u.Hostname())
	if host == "localhost" || strings.HasSuffix(host, ".localhost") || strings.HasSuffix(host, ".local") || strings.HasSuffix(host, ".internal") {
		return "", fetchErr("Only public career sites can be added.")
	}
	if ip, err := netip.ParseAddr(host); err == nil {
		if !isGlobal(ip) {
			return "", fetchErr("Only public career sites can be added.")
		}
	} else if !resolvesPublic(ctx, host) {
		return "", fetchErr("Only public career sites can be added.")
	}
	return u.String(), nil
}

// ── Requests ──

var client = &http.Client{
	// A redirect must stay on the public web, checked before it is followed.
	CheckRedirect: func(req *http.Request, via []*http.Request) error {
		if len(via) >= 10 {
			return errors.New("too many redirects")
		}
		_, err := CheckURL(req.Context(), req.URL.String())
		return err
	},
}

var (
	curlMu    sync.Mutex
	curlHosts = map[string]time.Time{} // Hosts whose bot filter needs curl.
	downPath  = regexp.MustCompile(`(?i)maintenance|outage`)
)

type request struct {
	method  string
	url     string
	headers map[string]string
	body    []byte
}

func open(ctx context.Context, r request, limit int) ([]byte, error) {
	return Timed(ctx, "http", func() ([]byte, error) { return openOnce(ctx, r, limit) })
}

func openOnce(ctx context.Context, r request, limit int) ([]byte, error) {
	if _, err := CheckURL(ctx, r.url); err != nil {
		return nil, err
	}
	u, _ := url.Parse(r.url)
	host := strings.ToLower(u.Host)
	curlMu.Lock()
	preferCurl := curlHosts[host].After(time.Now())
	curlMu.Unlock()
	if preferCurl {
		if data, err := viaCurl(ctx, r, limit); err == nil {
			return data, nil
		}
		// The site may have recovered; try the normal transport again.
		curlMu.Lock()
		delete(curlHosts, host)
		curlMu.Unlock()
	}
	fallback := func() ([]byte, error) {
		if preferCurl {
			return nil, fetchErr("Both HTTP transports failed.")
		}
		data, err := viaCurl(ctx, r, limit)
		if err == nil {
			curlMu.Lock()
			for k, until := range curlHosts {
				if until.Before(time.Now()) {
					delete(curlHosts, k)
				}
			}
			curlHosts[host] = time.Now().Add(5 * time.Minute)
			curlMu.Unlock()
		}
		return data, err
	}

	timeout, err := Remaining(ctx, fetchTimeout)
	if err != nil {
		return nil, err
	}
	reqCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	var body io.Reader
	if r.body != nil {
		body = bytes.NewReader(r.body)
	}
	req, err := http.NewRequestWithContext(reqCtx, r.method, r.url, body)
	if err != nil {
		return nil, fetchErr("Use a full http(s) careers page URL.")
	}
	req.Header.Set("User-Agent", userAgent)
	req.Header.Set("Accept-Language", "en-US,en;q=0.9")
	for k, v := range r.headers {
		req.Header.Set(k, v)
	}
	resp, err := client.Do(req)
	if err != nil {
		var f *FetchError
		if errors.As(err, &f) {
			return nil, f // A redirect to a private address is not retried.
		}
		// Bot filters drop Go's TLS handshake; curl negotiates like a browser.
		if data, err := fallback(); err == nil {
			return data, nil
		}
		return nil, fetchErr("Could not reach the site. Check the URL and your connection.")
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		// Several career sites refuse plain clients but serve curl normally.
		if data, err := fallback(); err == nil {
			return data, nil
		}
		return nil, fetchErr("The site returned HTTP %d.", resp.StatusCode)
	}
	if _, err := CheckURL(ctx, resp.Request.URL.String()); err != nil {
		return nil, err
	}
	if downPath.MatchString(resp.Request.URL.Path) {
		return nil, fetchErr("The site is down for maintenance. It will be retried on the next refresh.")
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, int64(limit)+1))
	if err != nil {
		if data, err := fallback(); err == nil {
			return data, nil
		}
		return nil, fetchErr("Could not reach the site. Check the URL and your connection.")
	}
	if len(data) > limit {
		return nil, fetchErr("The page is too large to read.")
	}
	return data, nil
}

// viaCurl fetches through curl: a browser-grade TLS and HTTP/2 handshake,
// which some career-site bot filters require.
func viaCurl(ctx context.Context, r request, limit int) ([]byte, error) {
	exe, err := exec.LookPath("curl")
	if err != nil {
		return nil, fetchErr("curl unavailable")
	}
	timeout, err := Remaining(ctx, fetchTimeout)
	if err != nil {
		return nil, err
	}
	args := []string{"-sS", "-L", "--proto-redir", "=https", "--http2", "--compressed",
		"-m", strconv.FormatFloat(timeout.Seconds(), 'f', 3, 64), "--max-filesize", strconv.Itoa(limit + 1),
		"-w", "\nJHALCV_META %{http_code} %{url_effective}", "-H", "Accept-Language: en-US,en;q=0.9"}
	for k, v := range r.headers {
		args = append(args, "-H", k+": "+v)
	}
	if r.body != nil {
		args = append(args, "-X", r.method, "--data-binary", "@-")
	}
	ctx, cancel := context.WithTimeout(ctx, timeout+2*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, exe, append(args, r.url)...)
	if r.body != nil {
		cmd.Stdin = bytes.NewReader(r.body)
	}
	out, err := cmd.Output()
	if ctx.Err() != nil {
		return nil, fetchErr("curl timed out")
	}
	code := 0
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		code = exitErr.ExitCode()
	} else if err != nil {
		return nil, fetchErr("curl could not run")
	}
	i := bytes.LastIndex(out, []byte("\nJHALCV_META "))
	if code == 63 || (i >= 0 && i > limit) {
		return nil, fetchErr("The page is too large to read.")
	}
	if code != 0 || i < 0 {
		return nil, fetchErr("curl exit %d", code)
	}
	body, meta := out[:i], string(out[i+len("\nJHALCV_META "):])
	status, final, _ := strings.Cut(meta, " ")
	final = strings.TrimSpace(final)
	if _, err := CheckURL(ctx, final); err != nil {
		return nil, err
	}
	if fu, err := url.Parse(final); err == nil && downPath.MatchString(fu.Path) {
		return nil, fetchErr("The site is down for maintenance. It will be retried on the next refresh.")
	}
	if n, _ := strconv.Atoi(status); n >= 400 {
		return nil, fetchErr("HTTP %d", n)
	}
	return body, nil
}

// GetText reads a page as text.
func GetText(ctx context.Context, link string, headers map[string]string, limit int) (string, error) {
	data, err := open(ctx, request{method: http.MethodGet, url: link, headers: headers}, limit)
	if err != nil {
		return "", err
	}
	return strings.ToValidUTF8(string(data), "�"), nil
}

// GetJSON reads a JSON document.
func GetJSON(ctx context.Context, link string) (any, error) {
	text, err := GetText(ctx, link, map[string]string{"Accept": "application/json"}, MaxJSONBytes)
	if err != nil {
		return nil, err
	}
	var out any
	if json.Unmarshal([]byte(text), &out) != nil {
		return nil, fetchErr("The site did not return readable job data.")
	}
	return out, nil
}

// PostJSON sends body as JSON and reads a JSON answer.
func PostJSON(ctx context.Context, link string, body any) (any, error) {
	data, _ := json.Marshal(body)
	raw, err := open(ctx, request{method: http.MethodPost, url: link, body: data,
		headers: map[string]string{"Accept": "application/json", "Content-Type": "application/json"}}, MaxJSONBytes)
	if err != nil {
		return nil, err
	}
	var out any
	if json.Unmarshal(bytes.ToValidUTF8(raw, []byte("\uFFFD")), &out) != nil {
		return nil, fetchErr("The site did not return readable job data.")
	}
	return out, nil
}

// ── Browser rendering ──

var chromeCandidates = []string{
	"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
	"/Applications/Chromium.app/Contents/MacOS/Chromium",
	"/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
	"google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "msedge",
}

// Browser finds an installed Chrome-family browser, or "".
func Browser() string {
	for _, c := range chromeCandidates {
		if strings.HasPrefix(c, "/") {
			if fi, err := os.Stat(c); err == nil && !fi.IsDir() {
				return c
			}
		} else if path, err := exec.LookPath(c); err == nil {
			return path
		}
	}
	return ""
}

var browserSlots = NewSlots(6)

// RenderPage returns the page as a browser sees it after its JavaScript ran:
// many career sites load their listings with JavaScript.
func RenderPage(ctx context.Context, link string) (string, error) {
	if _, err := CheckURL(ctx, link); err != nil {
		return "", err
	}
	chrome := Browser()
	if chrome == "" {
		return "", fetchErr("This page needs a browser to load its jobs. Install Google Chrome and refresh.")
	}
	if _, err := Remaining(ctx, renderTimeout); err != nil {
		return "", err
	}
	if err := browserSlots.Acquire(ctx, "Browser busy; this company will retry next refresh.", "browser_queue"); err != nil {
		return "", err
	}
	defer browserSlots.Release()
	return Timed(ctx, "browser", func() (string, error) {
		page, err := pool(chrome).render(ctx, link)
		if err != nil {
			return "", err
		}
		if len(page) > MaxBytes {
			page = strings.ToValidUTF8(page[:MaxBytes], "")
		}
		if len(page) < 500 {
			return "", fetchErr("Chrome could not load this page.")
		}
		return page, nil
	})
}

// ── HTML to text ──
//
// Scanning from every candidate start lets a crafted page pin a CPU, so
// blocks, tags and anchors are removed in single forward passes.

var (
	blockOpen  = regexp.MustCompile(`(?i)<(script|style|noscript|svg|template)\b[^>]*>`)
	blockClose = map[string]*regexp.Regexp{}
	anchorOpen = regexp.MustCompile(`(?i)<a\b[^>]*?href\s*=\s*["']([^"']+)["'][^>]*>`)
	anchorEnd  = regexp.MustCompile(`(?i)</a\s*>`)
	breaks     = regexp.MustCompile(`(?i)<\s*(br|/p|/div|/li|/h[1-6]|/tr|li)\b[^>]*>`)
	tag        = regexp.MustCompile(`<[^>]+>`)
)

func init() {
	for _, name := range []string{"script", "style", "noscript", "svg", "template"} {
		blockClose[name] = regexp.MustCompile(`(?i)</` + name + `\s*>`)
	}
}

func stripBlocks(value string) string {
	var out strings.Builder
	pos := 0
	for _, m := range blockOpen.FindAllStringSubmatchIndex(value, -1) {
		if m[0] < pos {
			continue
		}
		loc := blockClose[strings.ToLower(value[m[2]:m[3]])].FindStringIndex(value[m[1]:])
		if loc == nil {
			continue // An element with no closing tag keeps its content.
		}
		out.WriteString(value[pos:m[0]])
		pos = m[1] + loc[1]
	}
	out.WriteString(value[pos:])
	return out.String()
}

func stripTags(value string) string {
	last := strings.LastIndex(value, ">")
	if last < 0 {
		return value
	}
	return tag.ReplaceAllString(value[:last+1], " ") + value[last+1:]
}

// HTMLToText turns HTML into readable plain text, keeping line structure.
func HTMLToText(value string) string {
	if value == "" {
		return ""
	}
	value = stripBlocks(value)
	value = breaks.ReplaceAllString(value, "\n")
	value = html.UnescapeString(stripTags(value))
	var lines []string
	for _, line := range splitLines(value) {
		if line = strings.Join(strings.Fields(line), " "); line != "" {
			lines = append(lines, line)
		}
	}
	return strings.TrimSpace(strings.Join(lines, "\n"))
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

// PageWithLinks is page text in which every link keeps its absolute URL, as
// "[link: URL]", plus the set of those URLs.
func PageWithLinks(value, base string) (string, map[string]bool) {
	links := map[string]bool{}
	baseURL, _ := url.Parse(base)
	value = stripBlocks(value)
	var out strings.Builder
	pos := 0
	for _, m := range anchorOpen.FindAllStringSubmatchIndex(value, -1) {
		if m[0] < pos {
			continue
		}
		loc := anchorEnd.FindStringIndex(value[m[1]:])
		if loc == nil {
			continue
		}
		href := strings.TrimSpace(html.UnescapeString(value[m[2]:m[3]]))
		skip := false
		for _, p := range []string{"#", "javascript:", "mailto:", "tel:", "data:"} {
			if strings.HasPrefix(href, p) {
				skip = true
			}
		}
		if skip {
			continue
		}
		absolute := href
		if ref, err := url.Parse(href); err == nil && baseURL != nil {
			absolute = baseURL.ResolveReference(ref).String()
		}
		links[absolute] = true
		out.WriteString(value[pos:m[0]])
		out.WriteString(value[m[1]:m[1]+loc[0]] + " [link: " + absolute + "] ")
		pos = m[1] + loc[1]
	}
	out.WriteString(value[pos:])
	return HTMLToText(out.String()), links
}
