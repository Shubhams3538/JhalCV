// Package jobs collects job postings from company career sites, stores them
// in SQLite, and checks each one against the user's experience. It is the Go
// version of resume/jobs/.
package jobs

import (
	"cmp"
	"slices"
	"time"
)

// MaxAgeDays is how old a posting may be and still count as recent.
const MaxAgeDays = 7

// PostingDate parses the YYYY-MM-DD at the start of value.
func PostingDate(value string) (time.Time, bool) {
	if len(value) < 10 {
		return time.Time{}, false
	}
	t, err := time.ParseInLocation(time.DateOnly, value[:10], time.Local)
	return t, err == nil
}

func today() time.Time {
	y, m, d := time.Now().Date()
	return time.Date(y, m, d, 0, 0, 0, 0, time.Local)
}

// Freshness is "recent", "expired", or "unknown" (no date, or a future one).
// It never uses discovery or update time as posting time.
func Freshness(value string) string {
	posted, ok := PostingDate(value)
	now := today()
	if !ok || posted.After(now) {
		return "unknown"
	}
	if !posted.Before(now.AddDate(0, 0, -MaxAgeDays)) {
		return "recent"
	}
	return "expired"
}

// NewestFirst sorts recent postings first, then by posting date, newest
// first; equal items keep their order.
func NewestFirst[T any](items []T, posted func(T) string) []T {
	out := slices.Clone(items)
	key := func(item T) (int, time.Time) {
		p := posted(item)
		recent := 0
		if Freshness(p) == "recent" {
			recent = 1
		}
		d, _ := PostingDate(p)
		return recent, d
	}
	slices.SortStableFunc(out, func(a, b T) int {
		ra, da := key(a)
		rb, db := key(b)
		return cmp.Or(cmp.Compare(rb, ra), db.Compare(da))
	})
	return out
}

// Category is the one place a job is listed. Applied always wins over age
// and AI results.
func Category(status, posted string, manual, relevant, eligible *string) string {
	if status == "applied" {
		return "applied"
	}
	if Freshness(posted) == "expired" {
		return "old"
	}
	if is(manual, "no") {
		return "no"
	}
	if status == "hidden" {
		return "review"
	}
	if is(relevant, "no") {
		return "notrel" // Its own section, kept for audit instead of dropped.
	}
	if is(eligible, "no") {
		return "no"
	}
	if is(eligible, "yes") && is(relevant, "yes") {
		return "eligible"
	}
	return "review"
}

func is(p *string, value string) bool { return p != nil && *p == value }
