package jobs

import (
	"context"
	"fmt"
	"html"
	"net/url"
	"regexp"
	"slices"
	"strings"
)

// Most careers pages are a thin layer over a hiring platform whose public
// feed lists every job with no browser and no AI. A page that reveals its
// platform is upgraded to that feed. Two kinds of evidence are accepted: a
// platform link on the company's own page, or a guessed board name that the
// platform confirms belongs to this company.

var skipTokens = setOf("embed", "v1", "v0", "jobs", "job", "api", "js", "job_board", "careers", "posting-api", "en", "www")

func setOf(items ...string) map[string]bool {
	m := map[string]bool{}
	for _, i := range items {
		m[i] = true
	}
	return m
}

var platformPatterns = []struct {
	kind    string
	pattern *regexp.Regexp
}{
	{"greenhouse", regexp.MustCompile(`(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/embed/job_board(?:/js)?\?(?:[^"'\s<>]*&(?:amp;)?)?for=([A-Za-z0-9_-]+)`)},
	{"greenhouse", regexp.MustCompile(`boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)`)},
	{"greenhouse", regexp.MustCompile(`(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/([A-Za-z0-9_-]+)`)},
	{"lever", regexp.MustCompile(`api(?:\.eu)?\.lever\.co/v0/postings/([A-Za-z0-9_.-]+)`)},
	{"lever", regexp.MustCompile(`jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_.-]+)`)},
	{"ashby", regexp.MustCompile(`api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.%-]+)`)},
	{"ashby", regexp.MustCompile(`jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)`)},
	{"smartrecruiters", regexp.MustCompile(`api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]+)`)},
	{"smartrecruiters", regexp.MustCompile(`(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)`)},
	{"kula", regexp.MustCompile(`careers\.kula\.ai/([A-Za-z0-9_-]+)`)},
	{"keka", regexp.MustCompile(`([a-z0-9-]+)\.keka\.com/careers`)},
	{"pcsx", regexp.MustCompile(`([a-z0-9-]+)\.eightfold\.ai/careers`)},
}

var workdayLink = regexp.MustCompile(`([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)`)

func boardURL(kind, token string) string {
	switch kind {
	case "greenhouse":
		return "https://job-boards.greenhouse.io/" + token
	case "lever":
		return "https://jobs.lever.co/" + token
	case "ashby":
		return "https://jobs.ashbyhq.com/" + token
	case "smartrecruiters":
		return "https://careers.smartrecruiters.com/" + token
	case "kula":
		return "https://careers.kula.ai/" + token
	case "keka":
		return "https://" + token + ".keka.com/careers/"
	}
	return "https://" + token + ".eightfold.ai/careers"
}

// foundInPage lists platform boards referenced anywhere in a page.
func foundInPage(page string) []string {
	unescaped, err := url.PathUnescape(strings.ReplaceAll(page, `\/`, "/"))
	if err != nil {
		unescaped = strings.ReplaceAll(page, `\/`, "/")
	}
	text := html.UnescapeString(unescaped)
	var out []string
	add := func(u string) {
		if !slices.Contains(out, u) {
			out = append(out, u)
		}
	}
	for _, p := range platformPatterns {
		for _, m := range p.pattern.FindAllStringSubmatch(text, -1) {
			token := m[1]
			lower := strings.ToLower(token)
			if !skipTokens[lower] && !strings.HasSuffix(lower, ".js") && !strings.HasSuffix(lower, ".css") && !strings.HasSuffix(lower, ".png") {
				add(boardURL(p.kind, token))
			}
		}
	}
	for _, m := range workdayLink.FindAllStringSubmatch(text, -1) {
		site := strings.ToLower(m[3])
		if !skipTokens[site] && site != "wday" && site != "cxs" {
			add(fmt.Sprintf("https://%s.%s.myworkdayjobs.com/%s", m[1], m[2], m[3]))
		}
	}
	return out
}

var nonAlnum = regexp.MustCompile(`[^a-z0-9]`)

func normName(v string) string { return nonAlnum.ReplaceAllString(strings.ToLower(v), "") }

var words = regexp.MustCompile(`[a-z0-9]+`)

func guesses(name, link string) []string {
	u, _ := url.Parse(link)
	host := ""
	if u != nil {
		host = strings.ToLower(u.Hostname())
	}
	var labels []string
	for _, l := range strings.Split(host, ".") {
		switch l {
		case "www", "careers", "jobs", "career", "com", "in", "io", "ai", "co", "net", "org":
		default:
			labels = append(labels, l)
		}
	}
	ws := words.FindAllString(strings.Split(strings.ToLower(name), "/")[0], -1)
	candidates := []string{strings.Join(ws, "")}
	if len(labels) > 0 {
		candidates = append(candidates, labels[0])
	}
	if len(ws) > 0 {
		candidates = append(candidates, ws[0])
	} else {
		candidates = append(candidates, "")
	}
	candidates = append(candidates, strings.Join(ws, "-"))
	var out []string
	for _, c := range candidates {
		if len(c) >= 3 && !slices.Contains(out, c) {
			out = append(out, c)
		}
	}
	return out
}

const recentDays = 60

func maxDate(dates []string) string {
	best := ""
	for _, d := range dates {
		best = max(best, d)
	}
	return best
}

// board returns a board's job count, its own company name, and its newest
// posting date.
func board(ctx context.Context, kind, token string) (int, string, string) {
	get := func(u string) any {
		v, err := GetJSON(ctx, u)
		if err != nil {
			return nil
		}
		return v
	}
	switch kind {
	case "greenhouse":
		meta := get("https://boards-api.greenhouse.io/v1/boards/" + token)
		jobs := arr(obj(get("https://boards-api.greenhouse.io/v1/boards/" + token + "/jobs"))["jobs"])
		var dates []string
		for _, j := range jobs {
			d := obj(j)["first_published"]
			if text(d) == "" {
				d = obj(j)["updated_at"]
			}
			dates = append(dates, dateOf(d))
		}
		return len(jobs), text(obj(meta)["name"]), maxDate(dates)
	case "lever":
		jobs := arr(get("https://api.lever.co/v0/postings/" + token + "?mode=json"))
		var dates []string
		for _, j := range jobs {
			dates = append(dates, dateOf(obj(j)["createdAt"]))
		}
		return len(jobs), "", maxDate(dates)
	case "ashby":
		jobs := arr(obj(get("https://api.ashbyhq.com/posting-api/job-board/" + token))["jobs"])
		var dates []string
		for _, j := range jobs {
			dates = append(dates, dateOf(obj(j)["publishedAt"]))
		}
		return len(jobs), "", maxDate(dates)
	case "smartrecruiters":
		data := obj(get("https://api.smartrecruiters.com/v1/companies/" + token + "/postings?limit=100"))
		jobs := arr(data["content"])
		names := map[string]bool{}
		var dates []string
		for _, j := range jobs {
			names[text(obj(obj(j)["company"])["name"])] = true
			dates = append(dates, dateOf(obj(j)["releasedDate"]))
		}
		name := ""
		if len(names) == 1 {
			for n := range names {
				name = n
			}
		}
		return intOf(data["totalFound"]), name, maxDate(dates)
	case "kula":
		// Listing pages carry no dates; the link itself is page evidence.
		page, err := GetText(ctx, "https://careers.kula.ai/"+token+"?jobs=true", nil, MaxBytes)
		if err != nil {
			return 0, "", ""
		}
		found := map[string]bool{}
		for _, m := range regexp.MustCompile(`href="[^"]*/`+regexp.QuoteMeta(token)+`/(\d+)-[a-z0-9-]+`).FindAllStringSubmatch(page, -1) {
			found[m[1]] = true
		}
		return len(found), "", ""
	case "keka":
		page, err := GetText(ctx, "https://"+token+".keka.com/careers/", nil, MaxBytes)
		if err != nil {
			return 0, "", ""
		}
		m := KekaFeed.FindStringSubmatch(page)
		if m == nil {
			return 0, "", ""
		}
		rows := arr(get("https://" + token + ".keka.com/careers/api/embedjobs/default/active/" + m[1]))
		var dates []string
		for _, r := range rows {
			dates = append(dates, dateOf(obj(r)["publishedOn"]))
		}
		return len(rows), "", maxDate(dates)
	case "pcsx":
		data := obj(obj(get("https://" + token + ".eightfold.ai/api/pcsx/search?domain=" + token + ".com&query=&location=India&start=0"))["data"])
		var dates []string
		for _, p := range arr(data["positions"]) {
			dates = append(dates, dateOf(obj(p)["postedTs"]))
		}
		return intOf(data["count"]), "", maxDate(dates)
	}
	return 0, "", ""
}

// count is a live board's job count and name. A guessed board needs a
// posting from the last 60 days (test and abandoned boards are common);
// Kula shows no dates, so it is never guessed.
func count(ctx context.Context, kind, token string) (int, string) {
	n, name, newest := board(ctx, kind, token)
	if n == 0 || (kind != "kula" && (newest == "" || newest < today().AddDate(0, 0, -recentDays).Format("2006-01-02"))) {
		return 0, ""
	}
	return n, name
}

// facetValues finds a location facet naming label: a country value wins,
// otherwise every city value naming the country as a word.
func facetValues(facets []any, label string) (string, []string, bool) {
	word := IndiaPlace
	if !strings.EqualFold(label, "india") {
		word = regexp.MustCompile(`(?i)\b` + regexp.QuoteMeta(label) + `\b`)
	}
	for _, f := range facets {
		facet := obj(f)
		param := text(facet["facetParameter"])
		values := arr(facet["values"])
		var exact, cities []string
		for _, v := range values {
			value := obj(v)
			id := text(value["id"])
			if id == "" {
				continue
			}
			if strings.EqualFold(text(value["descriptor"]), label) {
				exact = append(exact, id)
			}
			if word.MatchString(text(value["descriptor"])) {
				cities = append(cities, id)
			}
		}
		if len(exact) > 0 && locationParam.MatchString(param) {
			return param, exact[:1], true
		}
		if len(cities) > 0 && locationParam.MatchString(param) {
			return param, cities, true
		}
		for _, v := range values {
			if arr(obj(v)["values"]) != nil {
				if p, ids, ok := facetValues([]any{v}, label); ok {
					return p, ids, true
				}
			}
		}
	}
	return "", nil, false
}

// workdayWithCountry applies a Workday site's own country filter.
func workdayWithCountry(ctx context.Context, link, country string) (string, int, bool) {
	base, u, err := WorkdayBase(link)
	if err != nil {
		return "", 0, false
	}
	data, err := PostJSON(ctx, base+"/jobs", map[string]any{"appliedFacets": map[string]any{}, "limit": 1, "offset": 0, "searchText": ""})
	if err != nil {
		return "", 0, false
	}
	param, ids, ok := facetValues(arr(obj(data)["facets"]), country)
	if !ok || param == "" {
		return "", 0, false
	}
	filtered, err := PostJSON(ctx, base+"/jobs", map[string]any{"appliedFacets": map[string]any{param: ids}, "limit": 1, "offset": 0, "searchText": ""})
	if err != nil {
		return "", 0, false
	}
	var q []string
	for _, id := range ids {
		q = append(q, param+"="+id)
	}
	return "https://" + u.Hostname() + strings.TrimRight(u.Path, "/") + "?" + strings.Join(q, "&"), intOf(obj(filtered)["total"]), true
}

var (
	oracleHosts = regexp.MustCompile(`([a-z0-9-]+(?:\.[a-z0-9-]+)*\.oraclecloud\.com)`)
	oracleSites = regexp.MustCompile(`\b(CX_\d+)\b`)
	jibeMention = regexp.MustCompile(`(?i)jibe|icims`)
)

func unique(items []string) []string {
	var out []string
	for _, i := range items {
		if !slices.Contains(out, i) {
			out = append(out, i)
		}
	}
	return out
}

func oracleFromPage(ctx context.Context, page, country string) (string, int, bool) {
	var hosts, sites []string
	for _, m := range oracleHosts.FindAllStringSubmatch(page, -1) {
		hosts = append(hosts, m[1])
	}
	for _, m := range oracleSites.FindAllStringSubmatch(page, -1) {
		sites = append(sites, m[1])
	}
	for _, host := range unique(hosts) {
		for _, site := range unique(sites) {
			data, err := GetJSON(ctx, "https://"+host+"/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"+
				"&finder=findReqs;siteNumber="+site+",facetsList=LOCATIONS,limit=1")
			if err != nil {
				continue
			}
			items := arr(obj(data)["items"])
			var block map[string]any
			if len(items) > 0 {
				block = obj(items[0])
			}
			for _, l := range arr(block["locationsFacet"]) {
				place := obj(l)
				if text(place["Name"]) == country && intOf(block["TotalJobsCount"]) > 0 {
					return "https://" + host + "/hcmUI/CandidateExperience/en/sites/" + site + "/jobs?locationId=" + text(place["Id"]),
						intOf(place["TotalCount"]), true
				}
			}
		}
	}
	return "", 0, false
}

func jibeFromPage(ctx context.Context, link, page string) (string, int, bool) {
	if !jibeMention.MatchString(page) {
		return "", 0, false
	}
	u, err := url.Parse(link)
	if err != nil {
		return "", 0, false
	}
	data, err := GetJSON(ctx, "https://"+u.Hostname()+"/api/jobs?limit=1")
	if err != nil {
		return "", 0, false
	}
	d := obj(data)
	if total := intOf(d["totalCount"]); total > 0 && d["jobs"] != nil {
		if _, ok := d["jobs"].([]any); ok {
			return "https://" + u.Hostname() + "/jobs", total, true
		}
	}
	return "", 0, false
}

// Resolved is the hiring-platform feed behind a careers page.
type Resolved struct {
	URL, Kind, Evidence string
	Jobs                int
}

// Resolve finds a company's own board, or nil. country "" skips country
// filtering.
func Resolve(ctx context.Context, name, link string, render, guess bool, country string) *Resolved {
	page, _ := GetText(ctx, link, nil, MaxBytes)
	if page != "" {
		if country != "" {
			if u, n, ok := oracleFromPage(ctx, page, country); ok && n > 0 {
				return &Resolved{u, "oracle", "linked from the careers page", n}
			}
		}
		if u, n, ok := jibeFromPage(ctx, link, page); ok && n > 0 {
			return &Resolved{u, "jibe", "linked from the careers page", n}
		}
	}
	hits := foundInPage(page)
	if len(hits) == 0 && render {
		if rendered, err := RenderPage(ctx, link); err == nil {
			hits = foundInPage(rendered)
		}
	}
	company := normName(strings.Split(name, "/")[0])
	for _, hit := range hits {
		kind := Detect(hit)
		if kind == "workday" {
			if country == "" {
				return &Resolved{hit, kind, "linked from the careers page", 1}
			}
			if u, n, ok := workdayWithCountry(ctx, hit, country); ok {
				return &Resolved{u, kind, "linked from the careers page", n}
			}
			continue
		}
		hu, _ := url.Parse(hit)
		token := strings.Split(strings.Trim(hu.Path, "/"), "/")[0]
		if kind == "keka" || kind == "pcsx" {
			token = strings.Split(hu.Hostname(), ".")[0]
		}
		if n, _ := count(ctx, kind, token); n > 0 {
			return &Resolved{hit, kind, "linked from the careers page", n}
		}
	}
	if !guess {
		return nil
	}
	for _, token := range guesses(name, link) {
		for _, kind := range []string{"greenhouse", "lever", "ashby", "smartrecruiters"} {
			n, boardName := count(ctx, kind, token)
			if n == 0 {
				continue
			}
			// A guessed board must clearly be this company's, not a namesake.
			if boardName != "" {
				if b := normName(boardName); b == company || b == normName(name) {
					return &Resolved{boardURL(kind, token), kind, "confirmed board name", n}
				}
			} else if normName(token) == company {
				return &Resolved{boardURL(kind, token), kind, "confirmed board name", n}
			}
		}
	}
	return nil
}
