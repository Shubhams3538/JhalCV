package storage

import (
	"strings"
	"unicode/utf8"

	"jhalcv/internal/usererr"
)

// text checks one user-entered string and returns it with surrounding spaces
// removed, like text() in resume/storage.py.
func text(value, label string, limit int, required bool) (string, error) {
	if utf8.RuneCountInString(value) > limit || (required && strings.TrimSpace(value) == "") {
		return "", usererr.Errorf("%s: enter text (maximum %d characters).", label, limit)
	}
	for _, r := range value {
		if r < 32 && r != '\n' && r != '\r' && r != '\t' {
			return "", usererr.Errorf("%s: unsupported control character.", label)
		}
	}
	return strings.TrimSpace(value), nil
}
