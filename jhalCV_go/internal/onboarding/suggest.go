package onboarding

import (
	"fmt"
	"net/url"
	"regexp"
	"slices"
	"strings"
	"unicode"

	"jhalcv/internal/jobs"
	"jhalcv/internal/storage"
)

// EmptyProfile is the starting point for a new user's profile.
func EmptyProfile() *storage.Profile {
	two := 2
	return &storage.Profile{
		Basics:         storage.Basics{Links: []storage.Link{}},
		Experience:     []storage.Experience{},
		Education:      []storage.Education{},
		CodingProfiles: []storage.CodingProfile{},
		Projects:       []storage.Project{},
		Skills:         storage.Skills{},
		Locked:         storage.Locked{MaxProjectsOnPage: &two},
	}
}

var headings = map[string]string{
	"experience": "experience", "work experience": "experience", "professional experience": "experience",
	"employment": "experience", "internships": "experience", "education": "education",
	"academic qualifications": "education", "projects": "projects", "personal projects": "projects",
	"technical projects": "projects", "skills": "skills", "technical skills": "skills",
	"coding profiles": "coding_profiles", "coding profiles and achievements": "coding_profiles",
	"achievements": "coding_profiles", "certifications": "ignore", "summary": "ignore",
	"profile": "ignore", "objective": "ignore", "interests": "ignore",
}

const datePattern = `(?:(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)[.,]?\s+)?(?:19|20)\d{2}`

var (
	urlPattern = regexp.MustCompile(`(?i)(?:https?://|www\.)[^\s<>"\]]+|(?:github\.com|linkedin\.com)/[^\s<>"\]]+`)
	// Python's "(?<=\d)-(?=\d)" separator is the second branch here: a bare
	// hyphen straight after the start year, followed by a year.
	rangePattern = regexp.MustCompile(`(?i)(?P<start>` + datePattern + `)(?:\s*(?:–|—|\s-\s|\bto\b)\s*(?P<end>` + datePattern + `|Present|Current|Now)|-(?P<end2>(?:19|20)\d{2}))`)
	bulletMark   = regexp.MustCompile(`^\s*[•●▪◦*-]\s+`)
	rolePattern  = regexp.MustCompile(`(?i)\b(engineer|developer|intern|analyst|manager|consultant|scientist|designer|architect|sde|swe|researcher|instructor|assistant|trainee)\b`)
	columnSplit  = regexp.MustCompile(`\s{2,}|\s+\|\s+`)
	emailPattern = regexp.MustCompile(`[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}`)
	phonePattern = regexp.MustCompile(`(^|[^\w])(\+?\d[\d ()-]{6,}\d)`)
	nonDigits    = regexp.MustCompile(`\D`)
	schoolWord   = regexp.MustCompile(`(?i)\b(university|college|institute|school|academy)\b`)
	workplace    = regexp.MustCompile(`(?i)^(?:remote|hybrid|on[- ]site|wfh)$`)
	titleSplit   = regexp.MustCompile(`\s{2,}|\s*\|\s*`)
	linkLabel    = regexp.MustCompile(`(?i)\s*(?:GitHub|Repository|Source|Demo)?\s*\[https?://[^\]]*\]`)
	skillSplit   = regexp.MustCompile(`[,;]`)
)

func rangeOf(s string) (start, end string, ok bool) {
	m := rangePattern.FindStringSubmatch(s)
	if m == nil {
		return "", "", false
	}
	end = m[rangePattern.SubexpIndex("end")]
	if end == "" {
		end = m[rangePattern.SubexpIndex("end2")]
	}
	return m[rangePattern.SubexpIndex("start")], end, true
}

func stripRange(s string) string {
	return strings.Trim(rangePattern.ReplaceAllString(s, ""), " |,–—-")
}

func columns(value string) []string {
	var out []string
	for _, part := range columnSplit.Split(value, -1) {
		if p := strings.Trim(part, " |"); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// WebLinks lists the web addresses in content plus extra, at most 100.
func WebLinks(content string, extra []string) []string {
	var out []string
	for _, value := range append(slices.Clone(extra), urlPattern.FindAllString(content, -1)...) {
		value = strings.TrimRight(strings.TrimSpace(value), ".,;|)")
		lower := strings.ToLower(value)
		if !strings.HasPrefix(lower, "http://") && !strings.HasPrefix(lower, "https://") {
			value = "https://" + value
		}
		u, err := url.Parse(value)
		if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" || u.User != nil || slices.Contains(out, value) {
			continue
		}
		out = append(out, value)
	}
	return out[:min(len(out), 100)]
}

func lines(content string) []string {
	var out []string
	for _, line := range strings.Split(strings.ReplaceAll(content, "\r\n", "\n"), "\n") {
		if strings.TrimSpace(line) != "" {
			out = append(out, strings.TrimRightFunc(line, unicode.IsSpace))
		}
	}
	return out
}

func hostOf(link string) (string, string) {
	u, err := url.Parse(link)
	if err != nil {
		return "", ""
	}
	return strings.TrimPrefix(strings.ToLower(u.Hostname()), "www."), strings.Trim(u.Path, "/")
}

// SuggestedProfile is a conservative local reading of resume text: uncertain
// and missing facts stay empty and editable.
func SuggestedProfile(content string, extraLinks []string) *storage.Profile {
	p := EmptyProfile()
	all := lines(content)
	if len(all) > 0 {
		p.Basics.Name = firstRunes(strings.TrimSpace(all[0]), 200)
	}
	p.Basics.Email = emailPattern.FindString(content)
	for _, m := range phonePattern.FindAllStringSubmatch(content, -1) {
		digits := len(nonDigits.ReplaceAllString(m[2], ""))
		if _, _, isRange := rangeOf(m[2]); digits >= 8 && digits <= 15 && !isRange {
			p.Basics.Phone = strings.TrimSpace(m[2])
			break
		}
	}
	for _, link := range WebLinks(content, extraLinks) {
		host, path := hostOf(link)
		if host == "github.com" && path != "" && !strings.Contains(path, "/") {
			p.Basics.Links = append(p.Basics.Links, storage.Link{Label: "GitHub", URL: link})
		} else if host == "linkedin.com" && strings.HasPrefix(path, "in/") {
			p.Basics.Links = append(p.Basics.Links, storage.Link{Label: "LinkedIn", URL: link})
		}
	}

	sections := map[string][]string{}
	current := ""
	for _, line := range all {
		heading := strings.ToLower(spacesRe.ReplaceAllString(strings.TrimRight(strings.TrimSpace(line), ":"), " "))
		if section, ok := headings[heading]; ok {
			current = section
		} else if current != "" && current != "ignore" {
			sections[current] = append(sections[current], line)
		}
	}

	for _, section := range []string{"experience", "education", "projects"} {
		type block struct{ headers, bullets []string }
		var blocks []block
		var headers, bullets []string
		bulletColumn := 0
		sectionLines := sections[section]
		for i, line := range sectionLines {
			if loc := bulletMark.FindStringIndex(line); loc != nil {
				bullets = append(bullets, strings.TrimSpace(line[loc[1]:]))
				bulletColumn = loc[1]
			} else if len(bullets) > 0 {
				following := ""
				if i+1 < len(sectionLines) {
					following = sectionLines[i+1]
				}
				indent := len(line) - len(strings.TrimLeftFunc(line, unicode.IsSpace))
				_, _, lineRange := rangeOf(line)
				_, _, nextRange := rangeOf(following)
				if indent >= bulletColumn && !lineRange && !nextRange {
					bullets[len(bullets)-1] += " " + strings.TrimSpace(line)
				} else {
					blocks = append(blocks, block{headers, bullets})
					headers, bullets = []string{strings.TrimSpace(line)}, nil
				}
			} else {
				// A new dated school after a completed degree is a new entry.
				previousDates := slices.ContainsFunc(headers, func(h string) bool { _, _, ok := rangeOf(h); return ok })
				_, _, dated := rangeOf(line)
				datedTitle := dated && stripRange(line) != ""
				if section == "education" && previousDates && (schoolWord.MatchString(line) || datedTitle) {
					blocks = append(blocks, block{headers, bullets})
					headers, bullets = nil, nil
				}
				headers = append(headers, strings.TrimSpace(line))
			}
		}
		if len(headers) > 0 || len(bullets) > 0 {
			blocks = append(blocks, block{headers, bullets})
		}
		for index, b := range blocks {
			if len(b.headers) == 0 {
				continue
			}
			start, end := "", ""
			for _, h := range b.headers {
				if s, e, ok := rangeOf(h); ok {
					start, end = s, e
					break
				}
			}
			var clean []string
			for _, h := range b.headers {
				if c := stripRange(h); c != "" {
					clean = append(clean, c)
				}
			}
			id := fmt.Sprintf("%s-%d", section, index)
			at := func(i int) string {
				if i < len(clean) {
					return clean[i]
				}
				return ""
			}
			switch section {
			case "education":
				detail := ""
				if len(clean) > 2 {
					detail = strings.Join(clean[2:], " | ")
				}
				p.Education = append(p.Education, storage.Education{Org: at(0), Degree: at(1), Detail: detail, Start: start, End: end})
			case "experience":
				p.Experience = append(p.Experience, experienceEntry(id, clean, b.bullets, start, end))
			default:
				title := ""
				if len(clean) > 0 {
					title = titleSplit.Split(clean[0], -1)[0]
				}
				title = strings.TrimSpace(linkLabel.ReplaceAllString(title, ""))
				project := storage.Project{ID: id, Name: title, Stack: []string{}, Angles: []string{}, Bullets: draftBullets(id, b.bullets)}
				if links := WebLinks(strings.Join(b.headers, "\n"), nil); len(links) > 0 {
					project.Link = &storage.Link{Label: "Project", URL: links[0]}
				}
				p.Projects = append(p.Projects, project)
			}
		}
	}
	for _, line := range sections["skills"] {
		group, values, found := strings.Cut(strings.TrimSpace(line), ":")
		if found && group != "" && !strings.HasPrefix(group, "_") {
			var items []string
			for _, s := range skillSplit.Split(values, -1) {
				if s = strings.TrimSpace(s); s != "" {
					items = append(items, s)
				}
			}
			p.Skills = setSkill(p.Skills, group, orEmpty(items))
		}
	}
	for _, line := range sections["coding_profiles"] {
		p.CodingProfiles = append(p.CodingProfiles, storage.CodingProfile{Text: strings.TrimSpace(bulletMark.ReplaceAllString(line, ""))})
	}
	return p
}

var spacesRe = regexp.MustCompile(`\s+`)

func setSkill(skills storage.Skills, group string, items []string) storage.Skills {
	for i, g := range skills {
		if g.Name == group {
			skills[i].Items = items
			return skills
		}
	}
	return append(skills, storage.SkillGroup{Name: group, Items: items})
}

func draftBullets(id string, texts []string) []storage.Bullet {
	out := []storage.Bullet{}
	for i, t := range texts {
		out = append(out, storage.Bullet{ID: fmt.Sprintf("%s-%d", id, i), Text: t, Status: "shipped", Angles: []string{}})
	}
	return out
}

func experienceEntry(id string, clean, bullets []string, start, end string) storage.Experience {
	var cells []string
	for _, h := range clean {
		cells = append(cells, columns(h)...)
	}
	roleIndex := slices.IndexFunc(cells, func(c string) bool { return rolePattern.MatchString(c) })
	role := ""
	var others []string
	if roleIndex >= 0 {
		role = cells[roleIndex]
		for i, c := range cells {
			if i != roleIndex {
				others = append(others, c)
			}
		}
	} else {
		if len(cells) > 1 {
			role = cells[1]
		}
		if len(cells) > 0 {
			others = append(others, cells[0])
		}
		if len(cells) > 2 {
			others = append(others, cells[2:]...)
		}
	}
	employer := ""
	for _, h := range clean {
		row := columns(h)
		if role == "" || slices.Contains(row, role) {
			continue
		}
		if len(row) > 0 && !workplace.MatchString(row[0]) {
			employer = row[0]
			break
		}
	}
	if employer == "" && len(others) > 0 {
		employer = others[0]
	}
	locations := slices.Clone(others)
	if i := slices.Index(locations, employer); i >= 0 {
		locations = slices.Delete(locations, i, i+1)
	}
	three := 3
	return storage.Experience{ID: id, Org: employer, Role: role, Location: strings.Join(locations, " | "), Stack: []string{},
		MaxBullets: &three, Start: start, End: end, Bullets: draftBullets(id, bullets)}
}

// Review is the checklist of details the user still has to confirm.
type Review struct {
	Missing         []string `json:"missing"`
	ExperienceYears float64  `json:"experience_years"`
}

// ReviewSummary lists missing details as suggestions; nothing is invented.
func ReviewSummary(p *storage.Profile) Review {
	missing := []string{}
	for _, f := range [][2]string{{"name", p.Basics.Name}, {"email", p.Basics.Email}, {"phone", p.Basics.Phone}} {
		if strings.TrimSpace(f[1]) == "" {
			missing = append(missing, "Contact "+f[0]+": enter it if applicable.")
		}
	}
	var urls []string
	for _, l := range p.Basics.Links {
		urls = append(urls, l.URL)
	}
	links := WebLinks("", urls)
	for _, s := range [][2]string{{"GitHub", "github.com"}, {"LinkedIn", "linkedin.com"}} {
		if !slices.ContainsFunc(links, func(u string) bool { h, _ := hostOf(u); return h == s[1] }) {
			missing = append(missing, s[0]+": add your profile URL if you have one; otherwise leave it blank.")
		}
	}
	if len(p.Experience) == 0 {
		missing = append(missing, "Experience: add employment/internships, or confirm you are a fresher.")
	}
	if len(p.Education) == 0 {
		missing = append(missing, "Education: add your institution, degree and dates.")
	}
	if len(p.Projects) == 0 {
		missing = append(missing, "Projects: add your projects with at least three confirmed bullets each.")
	}
	if len(p.Skills) == 0 {
		missing = append(missing, "Skills: add your confirmed technologies.")
	}
	confirm := func(section string, index int, fields [][2]string) {
		var absent []string
		for _, f := range fields {
			if strings.TrimSpace(f[1]) == "" {
				absent = append(absent, f[0])
			}
		}
		if len(absent) > 0 {
			missing = append(missing, fmt.Sprintf("%s %d: confirm %s.", section, index+1, strings.Join(absent, ", ")))
		}
	}
	for i, e := range p.Experience {
		confirm("Experience", i, [][2]string{{"org", e.Org}, {"role", e.Role}, {"start", e.Start}, {"end", e.End}})
	}
	for i, e := range p.Education {
		confirm("Education", i, [][2]string{{"org", e.Org}, {"degree", e.Degree}, {"start", e.Start}, {"end", e.End}})
	}
	for _, pr := range p.Projects {
		shipped := 0
		for _, b := range pr.Bullets {
			if (b.Status == "" || b.Status == "shipped") && strings.TrimSpace(b.Text) != "" {
				shipped++
			}
		}
		if shipped < 3 {
			name := pr.Name
			if name == "" {
				name = "Project"
			}
			missing = append(missing, name+": supply at least three confirmed bullets; missing claims are never generated.")
		}
	}
	return Review{Missing: missing, ExperienceYears: jobs.ProfileYears(p)}
}

func orEmpty(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

func firstRunes(s string, n int) string {
	r := []rune(s)
	return string(r[:min(n, len(r))])
}
