package storage

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
)

// Profile is the user's whole resume source: contact details, work history,
// projects, education and skills. It is saved as profile.json.
//
// Field order matches profile.json, and encoding/json writes fields in this
// order, so a saved file keeps its familiar layout.
type Profile struct {
	Locked         Locked          `json:"_locked"`
	Comment        string          `json:"_comment,omitempty"`
	Basics         Basics          `json:"basics"`
	Experience     []Experience    `json:"experience"`
	Projects       []Project       `json:"projects"`
	CodingProfiles []CodingProfile `json:"coding_profiles"`
	Skills         Skills          `json:"skills"`
	Education      []Education     `json:"education"`
}

// Locked holds resume rules that tailoring must respect.
type Locked struct {
	Note           string   `json:"note,omitempty"`
	FrozenSections []string `json:"frozen_sections"`
	// The pointers below are nil when the setting is not saved, which is
	// different from a saved 0 (an invalid value the user should hear about).
	MaxProjectsOnPage *int     `json:"max_projects_on_page,omitempty"`
	PinnedSkills      []string `json:"pinned_skills,omitempty"`
	MinProjectBullets *int     `json:"min_project_bullets,omitempty"`
}

// Basics are the contact details at the top of the resume.
type Basics struct {
	Name  string `json:"name"`
	Phone string `json:"phone"`
	Email string `json:"email"`
	Links []Link `json:"links,omitempty"`
}

// Link is a labelled web address, such as "GitHub" and its URL.
type Link struct {
	Label string `json:"label"`
	URL   string `json:"url"`
}

// Experience is one job.
type Experience struct {
	ID         string   `json:"id"`
	Org        string   `json:"org"`
	Role       string   `json:"role"`
	Location   string   `json:"location"`
	Start      string   `json:"start"`
	End        string   `json:"end"`
	Link       *Link    `json:"link,omitempty"`
	Stack      []string `json:"stack,omitempty"`
	Angles     []string `json:"angles,omitempty"`
	Bullets    []Bullet `json:"bullets"`
	MaxBullets *int     `json:"max_bullets,omitempty"`
}

// Project is one portfolio project.
type Project struct {
	ID         string            `json:"id"`
	Name       string            `json:"name"`
	Stack      []string          `json:"stack,omitempty"`
	Link       *Link             `json:"link,omitempty"`
	Angles     []string          `json:"angles,omitempty"`
	Taglines   map[string]string `json:"taglines,omitempty"`
	Bullets    []Bullet          `json:"bullets"`
	MaxBullets *int              `json:"max_bullets,omitempty"`
	Readme     string            `json:"_readme,omitempty"`
}

// Bullet is one line of achievement under a job or project.
type Bullet struct {
	ID     string   `json:"id"`
	Status string   `json:"status,omitempty"` // "shipped" (the default) or "planned"
	Angles []string `json:"angles,omitempty"`
	Text   string   `json:"text"`
	// NeedsVerification is a private note about a claim the user still has to
	// confirm. It is never printed on the resume.
	NeedsVerification string `json:"needs_verification,omitempty"`
}

// shipped reports whether the bullet describes finished work.
func (b Bullet) shipped() bool {
	return b.Status == "" || b.Status == "shipped"
}

// CodingProfile is a line such as "LeetCode: 500 problems solved".
type CodingProfile struct {
	Text string `json:"text"`
	Link *Link  `json:"link,omitempty"`
	// BoldUntil is the leading part of Text that is printed in bold.
	BoldUntil string `json:"bold_until,omitempty"`
}

// Education is one degree or school.
type Education struct {
	Org    string `json:"org"`
	Degree string `json:"degree"`
	Start  string `json:"start"`
	End    string `json:"end"`
	Detail string `json:"detail"`
	Link   *Link  `json:"link,omitempty"`
}

// Profile returns the saved profile, or the bundled example profile when none
// is saved yet (example is then true). Either way it is checked and tidied by
// Validate, like Store.profile() in resume/storage.py.
func (s *Store) Profile() (profile *Profile, example bool, err error) {
	data, example, err := s.readProfile()
	if err != nil {
		return nil, example, err
	}
	profile, err = ParseProfile(data)
	return profile, example, err
}

// ProfileFile returns the raw bytes of the profile file the app is using.
// Its fingerprint is the profile revision the page sends back with edits.
func (s *Store) ProfileFile() (data []byte, example bool, err error) {
	return s.readProfile()
}

func (s *Store) readProfile() (data []byte, example bool, err error) {
	data, err = os.ReadFile(filepath.Join(s.root, "profile.json"))
	if errors.Is(err, fs.ErrNotExist) {
		data, err = os.ReadFile(filepath.Join(s.root, "profile.example.json"))
		return data, true, err
	}
	return data, false, err
}

// ParseProfile decodes profile JSON and checks it with Validate.
func ParseProfile(data []byte) (*Profile, error) {
	var profile Profile
	if err := json.Unmarshal(data, &profile); err != nil {
		return nil, fmt.Errorf("Profile: %w", err)
	}
	if err := profile.Validate(); err != nil {
		return nil, err
	}
	return &profile, nil
}
