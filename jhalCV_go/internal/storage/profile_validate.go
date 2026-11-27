package storage

import (
	"maps"
	"net/url"
	"slices"
	"strings"

	"jhalcv/internal/usererr"
)

// Angles are the kinds of role a bullet or project can speak to. Tailoring
// uses them to pick what fits a job (resume/tailor.py: ANGLE_KEYWORDS).
var Angles = []string{"backend", "systems", "infra", "frontend", "agentic", "data", "eval", "dsa", "teaching"}

// FrozenSections are always printed exactly as saved, whatever the job says.
var FrozenSections = []string{"basics", "experience", "coding_profiles", "education"}

// focusAliases turn the words people type into the angle names above.
var focusAliases = map[string]string{
	"ai": "agentic", "llm": "agentic", "artificial intelligence": "agentic",
	"ai / agents": "agentic", "ai/agents": "agentic",
	"infrastructure": "infra", "devops": "infra",
	"front-end": "frontend", "front end": "frontend",
	"back-end": "backend", "back end": "backend",
	"ml": "data", "machine learning": "data",
	"evaluation": "eval", "testing": "eval",
}

// Validate checks the whole profile and tidies it in place: focus areas are
// turned into angle names and frozen sections are reset. It returns the first
// problem found, worded for the user, like validate_profile() in Python.
func (p *Profile) Validate() error {
	minimum := 2
	if p.Locked.MinProjectBullets != nil {
		minimum = *p.Locked.MinProjectBullets
		if minimum != 2 && minimum != 3 {
			return usererr.New("Project bullet minimum must be 2 or 3.")
		}
	}

	if err := p.Basics.validate(); err != nil {
		return err
	}

	// Every section must be a list (it may be empty).
	sections := []struct {
		name    string
		count   int
		missing bool
	}{
		{"experience", len(p.Experience), p.Experience == nil},
		{"projects", len(p.Projects), p.Projects == nil},
		{"education", len(p.Education), p.Education == nil},
		{"coding_profiles", len(p.CodingProfiles), p.CodingProfiles == nil},
	}
	for _, s := range sections {
		if s.missing || s.count > 50 {
			return listError(s.name, 50)
		}
	}

	// Bullets point back to their job or project by ID, so IDs must be unique
	// across both sections.
	ids := map[string]bool{}
	for i := range p.Experience {
		if err := p.Experience[i].validate(ids); err != nil {
			return err
		}
	}
	for i := range p.Projects {
		if err := p.Projects[i].validate(ids, minimum); err != nil {
			return err
		}
	}
	for _, e := range p.Education {
		if err := e.validate(); err != nil {
			return err
		}
	}
	for _, c := range p.CodingProfiles {
		if err := c.validate(); err != nil {
			return err
		}
	}

	if err := p.Skills.validate(); err != nil {
		return err
	}
	return p.Locked.validate()
}

func (b Basics) validate() error {
	if err := checkFields("", []field{
		{"name", b.Name, true}, {"email", b.Email, false}, {"phone", b.Phone, false},
	}, 200); err != nil {
		return err
	}
	if len(b.Links) > 10 {
		return listError("Contact links", 10)
	}
	for _, l := range b.Links {
		if err := l.validate(); err != nil {
			return err
		}
	}
	return nil
}

func (l Link) validate() error {
	if _, err := text(l.Label, "Link label", 80, true); err != nil {
		return err
	}
	raw, err := text(l.URL, "Link URL", 2000, true)
	if err != nil {
		return err
	}
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" || u.User != nil {
		return usererr.New("Links must be full http:// or https:// URLs without credentials.")
	}
	return nil
}

func (e *Experience) validate(ids map[string]bool) error {
	err := checkFields("experience", []field{
		{"id", e.ID, true}, {"org", e.Org, true}, {"role", e.Role, true},
		{"location", e.Location, false}, {"start", e.Start, false}, {"end", e.End, false},
	}, 500)
	if err != nil {
		return err
	}
	if e.Angles, err = checkCommon(e.Link, e.Stack, e.Angles); err != nil {
		return err
	}
	if err := claimID(ids, e.ID); err != nil {
		return err
	}
	if err := validateBullets(e.Bullets); err != nil {
		return err
	}
	return checkMaxBullets(e.MaxBullets)
}

func (p *Project) validate(ids map[string]bool, minimum int) error {
	err := checkFields("projects", []field{{"id", p.ID, true}, {"name", p.Name, true}}, 500)
	if err != nil {
		return err
	}
	if p.Angles, err = checkCommon(p.Link, p.Stack, p.Angles); err != nil {
		return err
	}
	if err := claimID(ids, p.ID); err != nil {
		return err
	}
	if err := validateBullets(p.Bullets); err != nil {
		return err
	}

	shipped := 0
	for _, b := range p.Bullets {
		if b.shipped() {
			shipped++
		}
	}
	if shipped < minimum {
		word := "two"
		if minimum == 3 {
			word = "three"
		}
		return usererr.Errorf("Each project needs at least %s confirmed, shipped bullets.", word)
	}

	if err := checkMaxBullets(p.MaxBullets); err != nil {
		return err
	}
	if p.Taglines, err = normalizeTaglines(p.Taglines); err != nil {
		return err
	}
	_, err = text(p.Readme, "README", 30000, false)
	return err
}

func (e Education) validate() error {
	err := checkFields("education", []field{
		{"org", e.Org, true}, {"degree", e.Degree, true},
		{"start", e.Start, false}, {"end", e.End, false}, {"detail", e.Detail, false},
	}, 500)
	if err != nil {
		return err
	}
	_, err = checkCommon(e.Link, nil, nil)
	return err
}

func (c CodingProfile) validate() error {
	if err := checkFields("coding_profiles", []field{{"text", c.Text, true}}, 500); err != nil {
		return err
	}
	_, err := checkCommon(c.Link, nil, nil)
	return err
}

func (s Skills) validate() error {
	if s == nil {
		return usererr.New("Skills: expected an object.")
	}
	for _, group := range s {
		if group.IsNote() {
			continue
		}
		if _, err := text(group.Name, "Skill group", 100, true); err != nil {
			return err
		}
		if group.Items == nil || len(group.Items) > 100 {
			return listError("Skills", 100)
		}
		for _, item := range group.Items {
			if _, err := text(item, "Skill", 100, true); err != nil {
				return err
			}
		}
	}
	return nil
}

func (l *Locked) validate() error {
	for _, section := range l.FrozenSections {
		if !slices.Contains(FrozenSections, section) {
			return usererr.New("Invalid frozen sections.")
		}
	}
	if l.MaxProjectsOnPage != nil && (*l.MaxProjectsOnPage < 1 || *l.MaxProjectsOnPage > 5) {
		return usererr.New("Project limit must be between 1 and 5.")
	}
	if len(l.PinnedSkills) > 100 {
		return listError("Pinned skills", 100)
	}
	for _, skill := range l.PinnedSkills {
		if _, err := text(skill, "Pinned skill", 100, true); err != nil {
			return err
		}
	}
	// The browser workflow always keeps these sections unchanged.
	l.FrozenSections = slices.Clone(FrozenSections)
	return nil
}

// validateBullets checks one job's or project's bullets and normalizes their
// focus areas.
func validateBullets(bullets []Bullet) error {
	if bullets == nil || len(bullets) > 30 {
		return listError("Bullets", 30)
	}
	seen := map[string]bool{}
	for i := range bullets {
		b := &bullets[i] // A pointer, so the normalized angles are saved.
		if _, err := text(b.ID, "Bullet ID", 100, true); err != nil {
			return err
		}
		if seen[b.ID] {
			return usererr.New("Bullet IDs must be unique within each entry.")
		}
		seen[b.ID] = true
		if _, err := text(b.Text, "Bullet", 1200, true); err != nil {
			return err
		}
		if b.Status != "" && b.Status != "shipped" && b.Status != "planned" {
			return usererr.New("Bullet status must be shipped or planned.")
		}
		var err error
		if b.Angles, err = focusList(b.Angles, "Bullet focus areas", 10); err != nil {
			return err
		}
	}
	return nil
}

// field is one text value to check, with the key used in error messages.
type field struct {
	key      string
	value    string
	required bool
}

func checkFields(section string, fields []field, limit int) error {
	for _, f := range fields {
		label := f.key
		if section != "" {
			label = section + ": " + f.key
		}
		if _, err := text(f.value, label, limit, f.required); err != nil {
			return err
		}
	}
	return nil
}

// checkCommon checks the parts any entry may have: a link, technologies and
// focus areas. It returns the focus areas normalized.
func checkCommon(link *Link, stack, angles []string) ([]string, error) {
	if link != nil {
		if err := link.validate(); err != nil {
			return nil, err
		}
	}
	if len(stack) > 50 {
		return nil, listError("Technologies", 50)
	}
	for _, tech := range stack {
		if _, err := text(tech, "Technology", 100, true); err != nil {
			return nil, err
		}
	}
	return focusList(angles, "Focus areas", 50)
}

func claimID(ids map[string]bool, id string) error {
	if ids[id] {
		return usererr.New("Project and experience IDs must be unique.")
	}
	ids[id] = true
	return nil
}

func checkMaxBullets(quota *int) error {
	if quota != nil && (*quota < 1 || *quota > 30) {
		return usererr.New("max_bullets must be between 1 and 30.")
	}
	return nil
}

// normalizeFocus turns one typed focus area ("AI", "DevOps") into an angle.
func normalizeFocus(value string) (string, error) {
	v, err := text(value, "Project focus area", 100, true)
	if err != nil {
		return "", err
	}
	v = strings.ToLower(v)
	if alias, ok := focusAliases[v]; ok {
		v = alias
	}
	if !slices.Contains(Angles, v) {
		return "", usererr.Errorf("Unknown focus area: %s. Choose AI, backend, systems, infrastructure, frontend, data, testing, DSA, or teaching.", v)
	}
	return v, nil
}

// focusList normalizes a list of focus areas and drops repeats, keeping order.
// A missing list (nil) stays missing.
func focusList(values []string, label string, limit int) ([]string, error) {
	if values == nil {
		return nil, nil
	}
	if len(values) > limit {
		return nil, listError(label, limit)
	}
	out := make([]string, 0, len(values))
	for _, value := range values {
		angle, err := normalizeFocus(value)
		if err != nil {
			return nil, err
		}
		if !slices.Contains(out, angle) {
			out = append(out, angle)
		}
	}
	return out, nil
}

// normalizeTaglines renames tagline keys to angle names. Keys are visited in
// sorted order, so if two keys mean the same angle the result is predictable.
func normalizeTaglines(taglines map[string]string) (map[string]string, error) {
	if taglines == nil {
		return nil, nil
	}
	out := make(map[string]string, len(taglines))
	for _, key := range slices.Sorted(maps.Keys(taglines)) {
		angle, err := normalizeFocus(key)
		if err != nil {
			return nil, err
		}
		if out[angle], err = text(taglines[key], "Tagline", 300, true); err != nil {
			return nil, err
		}
	}
	return out, nil
}

func listError(label string, limit int) error {
	return usererr.Errorf("%s: expected a list of at most %d entries.", label, limit)
}
