package storage

import (
	"encoding/json"
	"os"
	"reflect"
	"slices"
	"strings"
	"testing"
)

// The sample profile shipped with the Python app, shared by both versions.
const exampleProfilePath = "../../../profile.example.json"

// minimalProfile is the smallest profile that passes validation.
const minimalProfile = `{
  "basics": {"name": "Ada Lovelace", "email": "ada@example.com", "phone": ""},
  "experience": [{"id": "job", "org": "Analytical Engines", "role": "Engineer",
    "location": "London", "start": "1842", "end": "1843",
    "bullets": [{"id": "j1", "text": "Wrote the first program."}]}],
  "projects": [{"id": "engine", "name": "Engine",
    "bullets": [{"id": "p1", "text": "Designed it."}, {"id": "p2", "text": "Tested it."}]}],
  "education": [],
  "coding_profiles": [],
  "skills": {"Languages": ["Go"]}
}`

func parseMinimal(t *testing.T) *Profile {
	t.Helper()
	p, err := ParseProfile([]byte(minimalProfile))
	if err != nil {
		t.Fatalf("minimal profile should be valid: %v", err)
	}
	return p
}

func ptr(n int) *int { return &n }

// The example profile must survive decode -> validate -> encode unchanged,
// which proves the Go structs cover every field the profile uses.
func TestExampleProfileRoundTrip(t *testing.T) {
	data, err := os.ReadFile(exampleProfilePath)
	if err != nil {
		t.Fatal(err)
	}
	p, err := ParseProfile(data)
	if err != nil {
		t.Fatalf("example profile should be valid: %v", err)
	}
	encoded, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}

	var before, after any
	if err := json.Unmarshal(data, &before); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(encoded, &after); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(before, after) {
		t.Errorf("round trip changed the profile:\nbefore %v\nafter  %v", before, after)
	}
}

func TestSkillsKeepTheirOrder(t *testing.T) {
	input := `{"_comment": "not printed", "Zig": ["zig"], "Ada": ["ada"], "Go": []}`

	var skills Skills
	if err := json.Unmarshal([]byte(input), &skills); err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(skills)
	if err != nil {
		t.Fatal(err)
	}

	want := `{"_comment":"not printed","Zig":["zig"],"Ada":["ada"],"Go":[]}`
	if string(encoded) != want {
		t.Errorf("got  %s\nwant %s", encoded, want)
	}
}

func TestValidateNormalizesFocusAreas(t *testing.T) {
	p := parseMinimal(t)
	p.Projects[0].Angles = []string{"AI", "llm", "Backend"}
	p.Projects[0].Taglines = map[string]string{"DevOps": "  Runs itself  "}
	p.Locked.FrozenSections = []string{"basics"}

	if err := p.Validate(); err != nil {
		t.Fatal(err)
	}
	if want := []string{"agentic", "backend"}; !slices.Equal(p.Projects[0].Angles, want) {
		t.Errorf("Angles = %q, want %q (aliases mapped, repeats dropped)", p.Projects[0].Angles, want)
	}
	if got := p.Projects[0].Taglines; got["infra"] != "Runs itself" || len(got) != 1 {
		t.Errorf("Taglines = %q, want {infra: Runs itself}", got)
	}
	if !slices.Equal(p.Locked.FrozenSections, FrozenSections) {
		t.Errorf("FrozenSections = %q, want all of %q", p.Locked.FrozenSections, FrozenSections)
	}
}

func TestValidateRejectsBadProfiles(t *testing.T) {
	tests := []struct {
		name    string
		change  func(p *Profile)
		wantErr string
	}{
		{"missing name", func(p *Profile) { p.Basics.Name = " " }, "name: enter text"},
		{"link with a password", func(p *Profile) {
			p.Basics.Links = []Link{{"GitHub", "https://me:secret@github.com"}}
		}, "without credentials"},
		{"link that is not a web address", func(p *Profile) {
			p.Basics.Links = []Link{{"Files", "ftp://example.com"}}
		}, "full http:// or https://"},
		{"missing projects", func(p *Profile) { p.Projects = nil }, "projects: expected a list"},
		{"job and project share an ID", func(p *Profile) { p.Projects[0].ID = "job" }, "IDs must be unique"},
		{"repeated bullet ID", func(p *Profile) { p.Projects[0].Bullets[1].ID = "p1" }, "Bullet IDs must be unique"},
		{"unknown bullet status", func(p *Profile) { p.Projects[0].Bullets[0].Status = "draft" }, "shipped or planned"},
		{"too few shipped bullets", func(p *Profile) { p.Projects[0].Bullets[1].Status = "planned" }, "at least two confirmed"},
		{"minimum raised to three", func(p *Profile) { p.Locked.MinProjectBullets = ptr(3) }, "at least three confirmed"},
		{"impossible minimum", func(p *Profile) { p.Locked.MinProjectBullets = ptr(4) }, "minimum must be 2 or 3"},
		{"unknown focus area", func(p *Profile) { p.Experience[0].Angles = []string{"cooking"} }, "Unknown focus area: cooking"},
		{"zero max bullets", func(p *Profile) { p.Experience[0].MaxBullets = ptr(0) }, "max_bullets must be between 1 and 30"},
		{"too many projects on page", func(p *Profile) { p.Locked.MaxProjectsOnPage = ptr(6) }, "between 1 and 5"},
		{"unknown frozen section", func(p *Profile) { p.Locked.FrozenSections = []string{"projects"} }, "Invalid frozen sections"},
		{"blank skill", func(p *Profile) { p.Skills[0].Items = []string{""} }, "Skill: enter text"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			p := parseMinimal(t)
			tt.change(p)
			err := p.Validate()
			if err == nil || !strings.Contains(err.Error(), tt.wantErr) {
				t.Errorf("error = %v, want one containing %q", err, tt.wantErr)
			}
		})
	}
}

func TestParseProfileRejectsBadJSON(t *testing.T) {
	tests := []struct {
		name    string
		json    string
		wantErr string
	}{
		{"skill group that is not a list",
			strings.Replace(minimalProfile, `["Go"]`, `"Go"`, 1), `"Languages" must be a list`},
		{"skills that are not an object",
			strings.Replace(minimalProfile, `{"Languages": ["Go"]}`, `["Go"]`, 1), "Skills: expected an object"},
		{"missing skills",
			strings.Replace(minimalProfile, `"skills": {"Languages": ["Go"]}`, `"skills": null`, 1), "Skills: expected an object"},
		{"wrong type", strings.Replace(minimalProfile, `"Ada Lovelace"`, `42`, 1), "Profile:"},
		{"not JSON at all", "{", "Profile:"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := ParseProfile([]byte(tt.json))
			if err == nil || !strings.Contains(err.Error(), tt.wantErr) {
				t.Errorf("error = %v, want one containing %q", err, tt.wantErr)
			}
		})
	}
}
