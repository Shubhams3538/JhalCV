package resume

import "jhalcv/internal/storage"

// Report explains every tailoring decision. Its shape is open-ended (each
// tailorer and later step adds its own keys), so it stays a JSON object.
type Report = map[string]any

// BulletMeta links a printed bullet back to the profile. A nil score marks a
// frozen bullet, which the page fitter must never trim.
type BulletMeta struct {
	ID    string   `json:"id"`
	Score *float64 `json:"score"`
}

// ExperienceEntry is one job as printed.
type ExperienceEntry struct {
	Org        string       `json:"org"`
	Role       string       `json:"role"`
	Location   string       `json:"location"`
	Start      string       `json:"start"`
	End        string       `json:"end"`
	Bullets    []string     `json:"bullets"`
	BulletMeta []BulletMeta `json:"_bullet_meta"`
}

// ProjectEntry is one project as printed.
type ProjectEntry struct {
	ProjectID  string        `json:"_project_id"`
	Name       string        `json:"name"`
	Stack      []string      `json:"stack"`
	Bullets    []string      `json:"bullets"`
	BulletMeta []BulletMeta  `json:"_bullet_meta"`
	Link       *storage.Link `json:"link,omitempty"`
}

// SkillRow is one printed skill group.
type SkillRow struct {
	Label   string   `json:"label"`
	Items   []string `json:"items"`
	Matched []string `json:"_matched,omitzero"` // omitzero keeps an empty list but omits a nil one.
}

// Data is exactly what templates/classic.typ reads.
type Data struct {
	Basics         storage.Basics          `json:"basics"`
	Experience     []ExperienceEntry       `json:"experience"`
	Projects       []ProjectEntry          `json:"projects"`
	CodingProfiles []storage.CodingProfile `json:"coding_profiles"`
	Skills         []SkillRow              `json:"skills"`
	Education      []storage.Education     `json:"education"`
	SectionOrder   []string                `json:"section_order"`
	Config         map[string]any          `json:"config"`
}

// normalized replaces nil lists with empty ones: the template treats a JSON
// null as an error, where Python always wrote [].
func (d *Data) normalized() *Data {
	d.Experience = orEmpty(d.Experience)
	for i := range d.Experience {
		d.Experience[i] = d.Experience[i].normalized()
	}
	d.Projects = orEmpty(d.Projects)
	for i := range d.Projects {
		d.Projects[i] = d.Projects[i].normalized()
	}
	d.CodingProfiles = orEmpty(d.CodingProfiles)
	d.Skills = orEmpty(d.Skills)
	for i := range d.Skills {
		d.Skills[i].Items = orEmpty(d.Skills[i].Items)
	}
	d.Education = orEmpty(d.Education)
	d.SectionOrder = orEmpty(d.SectionOrder)
	if d.Config == nil {
		d.Config = map[string]any{}
	}
	return d
}

func (e ExperienceEntry) normalized() ExperienceEntry {
	e.Bullets, e.BulletMeta = orEmpty(e.Bullets), orEmpty(e.BulletMeta)
	return e
}

func (p ProjectEntry) normalized() ProjectEntry {
	p.Stack, p.Bullets, p.BulletMeta = orEmpty(p.Stack), orEmpty(p.Bullets), orEmpty(p.BulletMeta)
	return p
}

func orEmpty[T any](s []T) []T {
	if s == nil {
		return []T{}
	}
	return s
}

// Clone deep-copies the data.
func (d *Data) Clone() *Data {
	return clone(d).normalized()
}
