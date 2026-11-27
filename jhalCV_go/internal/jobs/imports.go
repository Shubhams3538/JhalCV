package jobs

import (
	"context"
	"encoding/csv"
	"errors"
	"io"
	"net/url"
	"slices"
	"sort"
	"strings"

	"jhalcv/internal/usererr"
)

// Limits for one CSV import of companies.
const (
	MaxImportRows  = 500
	MaxImportBytes = 500_000
)

// NameKey compares company names ignoring case and spacing.
func NameKey(value string) string {
	return strings.ToLower(strings.Join(strings.Fields(value), " "))
}

// URLKey compares careers URLs ignoring case, a trailing slash and query order.
func URLKey(value string) string {
	u, err := url.Parse(value)
	if err != nil {
		return value
	}
	q := u.Query()
	keys := make([]string, 0, len(q))
	for k := range q {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var parts []string
	for _, k := range keys {
		for _, v := range q[k] {
			parts = append(parts, url.QueryEscape(k)+"="+url.QueryEscape(v))
		}
	}
	slices.Sort(parts)
	out := url.URL{Scheme: strings.ToLower(u.Scheme), Host: strings.ToLower(u.Host),
		Path: strings.TrimRight(u.Path, "/"), RawQuery: strings.Join(parts, "&")}
	return out.String()
}

// ValidateEntry checks one company name and careers URL.
func ValidateEntry(ctx context.Context, company, link any) (string, string, error) {
	name, ok1 := company.(string)
	raw, ok2 := link.(string)
	if !ok1 || !ok2 {
		return "", "", usererr.New("Company and URL must be text.")
	}
	name = strings.Join(strings.Fields(name), " ")
	if name == "" || len([]rune(name)) > 100 {
		return "", "", usererr.New("Company name must contain 1–100 characters.")
	}
	checked, err := CheckURL(ctx, raw)
	return name, checked, err
}

// ImportRow is one previewed CSV row.
type ImportRow struct {
	Row     int    `json:"row"`
	Company string `json:"company"`
	URL     string `json:"url"`
	Status  string `json:"status"` // ready, duplicate or invalid
	Message string `json:"message"`
}

// ImportPreview summarizes a CSV before anything is saved.
type ImportPreview struct {
	Rows       []ImportRow `json:"rows"`
	Ready      int         `json:"ready"`
	Duplicates int         `json:"duplicates"`
	Invalid    int         `json:"invalid"`
}

// PreviewCSV validates a two-column company,url CSV without fetching any
// site or using AI.
func PreviewCSV(ctx context.Context, content any, existing []Source) (*ImportPreview, error) {
	text, ok := content.(string)
	if !ok || len(text) > MaxImportBytes {
		return nil, usererr.New("Choose a CSV file under 500 KB.")
	}
	r := csv.NewReader(strings.NewReader(strings.TrimPrefix(text, "\uFEFF")))
	r.FieldsPerRecord = -1
	header, err := r.Read()
	if err != nil && !errors.Is(err, io.EOF) {
		return nil, usererr.New("This CSV has broken quoting. Export it as CSV UTF-8 and try again.")
	}
	aliases := map[string]string{"company": "company", "name": "company", "url": "url", "link": "url"}
	var columns []string
	for _, h := range header {
		columns = append(columns, aliases[strings.ToLower(strings.TrimSpace(h))])
	}
	if len(columns) != 2 || !slices.Contains(columns, "company") || !slices.Contains(columns, "url") {
		return nil, usererr.New("Use two columns named company,url (or name,link).")
	}
	companyCol := slices.Index(columns, "company")
	names, urls := map[string]bool{}, map[string]bool{}
	for _, s := range existing {
		names[NameKey(s.Name)] = true
		urls[URLKey(s.URL)] = true
	}
	out := &ImportPreview{Rows: []ImportRow{}}
	for {
		values, err := r.Read()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, usererr.New("This CSV has broken quoting. Export it as CSV UTF-8 and try again.")
		}
		if !slices.ContainsFunc(values, func(v string) bool { return strings.TrimSpace(v) != "" }) {
			continue
		}
		if len(out.Rows) >= MaxImportRows {
			return nil, usererr.Errorf("Import at most %d companies at a time.", MaxImportRows)
		}
		line, _ := r.FieldPos(0)
		row := ImportRow{Row: line, Status: "invalid"}
		if len(values) != 2 {
			row.Message = "This row must contain exactly two values."
		} else {
			name, link, err := ValidateEntry(ctx, values[companyCol], values[1-companyCol])
			switch {
			case err != nil:
				row.Message = err.Error()
			case names[NameKey(name)] || urls[URLKey(link)]:
				row.Company, row.URL = name, link
				row.Status, row.Message = "duplicate", "Company or careers URL is already in the app or this file."
			default:
				row.Company, row.URL, row.Status = name, link, "ready"
				names[NameKey(name)], urls[URLKey(link)] = true, true
			}
		}
		if row.Company == "" && len(values) > companyCol {
			row.Company = firstRunes(values[companyCol], 100)
		}
		out.Rows = append(out.Rows, row)
	}
	if len(out.Rows) == 0 {
		return nil, usererr.New("The CSV contains no companies.")
	}
	for _, row := range out.Rows {
		switch row.Status {
		case "ready":
			out.Ready++
		case "duplicate":
			out.Duplicates++
		default:
			out.Invalid++
		}
	}
	return out, nil
}

func firstRunes(s string, n int) string {
	r := []rune(s)
	return string(r[:min(n, len(r))])
}
