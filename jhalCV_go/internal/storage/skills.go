package storage

import (
	"bytes"
	"encoding/json"
	"strings"

	"jhalcv/internal/usererr"
)

// Skills are the named skill groups ("Languages": ["Go", "Python"], ...) in
// the order the user arranged them.
//
// In JSON this is an object. A Go map would forget the order (and encode keys
// alphabetically), so Skills is a list with its own JSON encoding instead.
type Skills []SkillGroup

// SkillGroup is one named group of skills.
type SkillGroup struct {
	Name  string
	Items []string
	// Note holds the raw value of an entry whose name starts with "_", such
	// as "_comment". Those entries are kept unchanged and never printed.
	Note json.RawMessage
}

// IsNote reports whether the group is a "_"-prefixed note, not real skills.
func (g SkillGroup) IsNote() bool {
	return strings.HasPrefix(g.Name, "_")
}

// UnmarshalJSON reads a JSON object one key at a time, keeping its order.
func (s *Skills) UnmarshalJSON(data []byte) error {
	if string(data) == "null" {
		*s = nil // Validate reports the missing skills.
		return nil
	}

	dec := json.NewDecoder(bytes.NewReader(data))
	if tok, err := dec.Token(); err != nil || tok != json.Delim('{') {
		return usererr.New("Skills: expected an object.")
	}

	groups := Skills{}
	for dec.More() {
		tok, err := dec.Token()
		if err != nil {
			return err
		}
		group := SkillGroup{Name: tok.(string)} // Object keys are always strings.

		var value json.RawMessage
		if err := dec.Decode(&value); err != nil {
			return err
		}
		if group.IsNote() {
			group.Note = value
		} else if err := json.Unmarshal(value, &group.Items); err != nil {
			return usererr.Errorf("Skills: %q must be a list of skills.", group.Name)
		}
		groups = append(groups, group)
	}

	*s = groups
	return nil
}

// MarshalJSON writes the groups back as one JSON object, in order.
func (s Skills) MarshalJSON() ([]byte, error) {
	var buf bytes.Buffer
	buf.WriteByte('{')
	for i, group := range s {
		if i > 0 {
			buf.WriteByte(',')
		}
		key, err := json.Marshal(group.Name)
		if err != nil {
			return nil, err
		}
		value := []byte(group.Note)
		if !group.IsNote() {
			if value, err = json.Marshal(group.Items); err != nil {
				return nil, err
			}
		}
		if len(value) == 0 {
			value = []byte("null")
		}
		buf.Write(key)
		buf.WriteByte(':')
		buf.Write(value)
	}
	buf.WriteByte('}')
	return buf.Bytes(), nil
}
