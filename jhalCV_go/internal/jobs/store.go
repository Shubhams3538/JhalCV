package jobs

import (
	"context"
	"database/sql"
	"errors"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	_ "modernc.org/sqlite" // Registers the "sqlite" driver.

	"jhalcv/internal/usererr"
)

const schema = `
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    include TEXT NOT NULL DEFAULT '',
    exclude TEXT NOT NULL DEFAULT '',
    locations TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    last_run_at INTEGER,
    last_error TEXT,
    last_count INTEGER
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    posted TEXT NOT NULL DEFAULT '',
    requirements TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    first_seen_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'new',
    min_years REAL,
    eligible TEXT,
    reason TEXT,
    checked_by TEXT,
    UNIQUE (source_id, external_id)
);
CREATE TABLE IF NOT EXISTS dismissed_jobs (
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    PRIMARY KEY (source_id, external_id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);`

// Statuses a user can give a job.
var Statuses = []string{"new", "seen", "applied", "hidden"}

var workdayLocale = regexp.MustCompile(`^/[a-z]{2}-[A-Z]{2}/`)

// jobURLKey compares job links, ignoring Workday's language prefix.
func jobURLKey(link string) string {
	if link == "" {
		return ""
	}
	u, err := url.Parse(link)
	if err == nil && strings.HasSuffix(u.Hostname(), ".myworkdayjobs.com") {
		path := strings.TrimRight(workdayLocale.ReplaceAllString(u.Path, "/"), "/")
		return strings.ToLower(u.Scheme) + "://" + strings.ToLower(u.Host) + path
	}
	return URLKey(link)
}

// LocationMatches reports whether a job location matches any of the
// comma-separated wanted locations (all locations when none are wanted).
func LocationMatches(location, locations string) bool {
	var terms []string
	for _, part := range strings.Split(locations, ",") {
		if t := strings.ToLower(strings.TrimSpace(part)); t != "" {
			terms = append(terms, t)
		}
	}
	if slices.Contains(terms, "india") {
		terms = append(terms, "ind") // ISO code used by several official boards.
	}
	if len(terms) == 0 {
		return true
	}
	value := strings.ToLower(location)
	for _, t := range terms {
		if wholeWord(value, t) {
			return true
		}
	}
	return false
}

// wholeWord finds term in value with no word character on either side
// (Python's (?<!\w)term(?!\w)).
func wholeWord(value, term string) bool {
	for offset := 0; offset <= len(value); {
		i := strings.Index(value[offset:], term)
		if i < 0 {
			return false
		}
		start, end := offset+i, offset+i+len(term)
		before, after := lastRune(value[:start]), firstRune(value[end:])
		if !isWord(before) && !isWord(after) {
			return true
		}
		offset = start + 1
	}
	return false
}

func lastRune(s string) rune {
	r := []rune(s)
	if len(r) == 0 {
		return 0
	}
	return r[len(r)-1]
}

func firstRune(s string) rune {
	for _, r := range s {
		return r
	}
	return 0
}

// matchingLocationLabel keeps only the matching parts of "A; B; C".
func matchingLocationLabel(location, locations string) string {
	if locations == "" {
		return location
	}
	var matches []string
	for _, part := range strings.Split(location, ";") {
		if LocationMatches(part, locations) {
			matches = append(matches, strings.TrimSpace(part))
		}
	}
	if len(matches) == 0 {
		return location
	}
	return strings.Join(matches, "; ")
}

// Source is one saved company careers page.
type Source struct {
	ID          int64   `json:"id"`
	Name        string  `json:"name"`
	URL         string  `json:"url"`
	Kind        string  `json:"kind"`
	Include     string  `json:"include"`
	Exclude     string  `json:"exclude"`
	Locations   string  `json:"locations"`
	CreatedAt   int64   `json:"created_at"`
	LastRunAt   *int64  `json:"last_run_at"`
	LastError   *string `json:"last_error"`
	LastCount   *int64  `json:"last_count"`
	OriginalURL string  `json:"original_url"`
	ResolvedAt  int64   `json:"resolved_at"`
	Label       string  `json:"label,omitempty"`
}

const sourceColumns = "id, name, url, kind, include, exclude, locations, created_at, last_run_at, last_error, last_count, original_url, resolved_at"

func scanSource(row interface{ Scan(...any) error }) (Source, error) {
	var s Source
	err := row.Scan(&s.ID, &s.Name, &s.URL, &s.Kind, &s.Include, &s.Exclude, &s.Locations, &s.CreatedAt,
		&s.LastRunAt, &s.LastError, &s.LastCount, &s.OriginalURL, &s.ResolvedAt)
	return s, err
}

// Store is the SQLite collection of sources and jobs.
type Store struct {
	path string
	db   *sql.DB
}

// OpenStore opens (and migrates) root/.local/<filename>.
func OpenStore(root, filename string) (*Store, error) {
	path := filepath.Join(root, ".local", filename)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	// Reads during a refresh must not wait for a source saving new jobs, so
	// the database uses write-ahead logging. Transactions take the write lock
	// up front (BEGIN IMMEDIATE) so two writers cannot deadlock.
	db, err := sql.Open("sqlite", "file:"+path+"?_pragma=foreign_keys(1)&_pragma=busy_timeout(30000)&_pragma=journal_mode(WAL)&_txlock=immediate")
	if err != nil {
		return nil, err
	}
	s := &Store{path: path, db: db}
	if err := s.migrate(); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

// Close closes the database.
func (s *Store) Close() error { return s.db.Close() }

// DB exposes the database for setup's transaction across sources and jobs.
func (s *Store) DB() *sql.DB { return s.db }

func (s *Store) migrate() error {
	if _, err := s.db.Exec(schema); err != nil {
		return err
	}
	return s.tx(context.Background(), func(tx *sql.Tx) error {
		columns := func(table string) (map[string]bool, error) {
			rows, err := tx.Query("SELECT name FROM pragma_table_info('" + table + "')")
			if err != nil {
				return nil, err
			}
			defer rows.Close()
			out := map[string]bool{}
			for rows.Next() {
				var name string
				if err := rows.Scan(&name); err != nil {
					return nil, err
				}
				out[name] = true
			}
			return out, rows.Err()
		}
		jobCols, err := columns("jobs")
		if err != nil {
			return err
		}
		sourceCols, err := columns("sources")
		if err != nil {
			return err
		}
		var stmts []string
		if !sourceCols["locations"] {
			stmts = append(stmts, "ALTER TABLE sources ADD COLUMN locations TEXT NOT NULL DEFAULT ''")
		}
		if !sourceCols["original_url"] {
			stmts = append(stmts, "ALTER TABLE sources ADD COLUMN original_url TEXT NOT NULL DEFAULT ''")
		}
		if !sourceCols["resolved_at"] {
			stmts = append(stmts, "ALTER TABLE sources ADD COLUMN resolved_at INTEGER NOT NULL DEFAULT 0")
		}
		legacyDates := !jobCols["relevant"]
		for _, name := range []string{"relevant", "relevance_reason", "manual_eligible"} {
			if !jobCols[name] {
				stmts = append(stmts, "ALTER TABLE jobs ADD COLUMN "+name+" TEXT")
			}
		}
		if legacyDates {
			// Older Greenhouse readers used updated_at as the posting time;
			// quarantine those dates until a refresh supplies first_published.
			stmts = append(stmts, "UPDATE jobs SET posted='' WHERE source_id IN (SELECT id FROM sources WHERE kind='greenhouse')")
		}
		if !jobCols["detail_attempted_at"] {
			stmts = append(stmts, "ALTER TABLE jobs ADD COLUMN detail_attempted_at INTEGER NOT NULL DEFAULT 0")
		}
		for _, stmt := range stmts {
			if _, err := tx.Exec(stmt); err != nil {
				return err
			}
		}
		return seedSequence(tx)
	})
}

func (s *Store) tx(ctx context.Context, fn func(*sql.Tx) error) error {
	end := Measure(ctx, "database")
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		end(true)
		return err
	}
	if err := fn(tx); err != nil {
		_ = tx.Rollback()
		end(true)
		return err
	}
	err = tx.Commit()
	end(err != nil)
	return err
}

// seedSequence makes sure a deleted job ID is never reused: another open tab
// may still refer to it.
func seedSequence(tx *sql.Tx) error {
	_, err := tx.Exec("INSERT OR IGNORE INTO meta (key, value) SELECT 'job_id_sequence', COALESCE(MAX(id),0) FROM jobs")
	return err
}

// ── sources ──

// Sources lists every saved company, by name.
func (s *Store) Sources(ctx context.Context) ([]Source, error) {
	end := Measure(ctx, "database")
	rows, err := s.db.QueryContext(ctx, "SELECT "+sourceColumns+" FROM sources ORDER BY name COLLATE NOCASE")
	if err != nil {
		end(true)
		return nil, err
	}
	defer rows.Close()
	out := []Source{}
	for rows.Next() {
		src, err := scanSource(rows)
		if err != nil {
			end(true)
			return nil, err
		}
		out = append(out, src)
	}
	end(rows.Err() != nil)
	return out, rows.Err()
}

// AddSource saves a new company careers page.
func (s *Store) AddSource(ctx context.Context, name, link, kind, include, exclude, locations string) (*Source, error) {
	var src Source
	err := s.tx(ctx, func(tx *sql.Tx) error {
		res, err := tx.Exec("INSERT INTO sources (name, url, kind, include, exclude, locations, created_at) VALUES (?,?,?,?,?,?,?)",
			name, link, kind, include, exclude, locations, time.Now().Unix())
		if err != nil {
			if isConstraint(err) {
				return usererr.New("This careers URL is already added.")
			}
			return err
		}
		id, _ := res.LastInsertId()
		src, err = scanSource(tx.QueryRow("SELECT "+sourceColumns+" FROM sources WHERE id=?", id))
		return err
	})
	return &src, err
}

func isConstraint(err error) bool {
	return err != nil && strings.Contains(err.Error(), "UNIQUE constraint failed")
}

// SetSourceLocations changes the wanted locations of one company.
func (s *Store) SetSourceLocations(ctx context.Context, id int64, locations string) error {
	res, err := s.db.ExecContext(ctx, "UPDATE sources SET locations=? WHERE id=?", locations, id)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return usererr.New("This careers page is no longer saved.")
	}
	return nil
}

// NoteResolveAttempt records when a page was last examined for a feed.
func (s *Store) NoteResolveAttempt(ctx context.Context, id int64) error {
	_, err := s.db.ExecContext(ctx, "UPDATE sources SET resolved_at=? WHERE id=?", time.Now().Unix(), id)
	return err
}

// UpgradeSource points a careers page at the hiring-platform feed behind
// it, keeping the original link and every saved job. It returns nil when that
// feed is already saved as its own company.
func (s *Store) UpgradeSource(ctx context.Context, id int64, link, kind string) (*Source, error) {
	var out *Source
	err := s.tx(ctx, func(tx *sql.Tx) error {
		var one int
		if tx.QueryRow("SELECT 1 FROM sources WHERE url=? AND id!=?", link, id).Scan(&one) == nil {
			return nil
		}
		if _, err := tx.Exec("UPDATE sources SET original_url=CASE WHEN original_url='' THEN url ELSE original_url END, "+
			"url=?, kind=?, resolved_at=?, last_error=NULL WHERE id=?", link, kind, time.Now().Unix(), id); err != nil {
			return err
		}
		src, err := scanSource(tx.QueryRow("SELECT "+sourceColumns+" FROM sources WHERE id=?", id))
		if err == nil {
			out = &src
		} else if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		return err
	})
	return out, err
}

// EditSourceURL changes a company's careers link.
func (s *Store) EditSourceURL(ctx context.Context, id int64, link, kind string) error {
	return s.tx(ctx, func(tx *sql.Tx) error {
		var current string
		if err := tx.QueryRow("SELECT url FROM sources WHERE id=?", id).Scan(&current); err != nil {
			return usererr.New("This careers page is no longer saved.")
		}
		_, err := tx.Exec("UPDATE sources SET original_url=CASE WHEN original_url='' THEN url ELSE original_url END, "+
			"url=?,kind=?,resolved_at=0,last_error=NULL WHERE id=?", link, kind, id)
		if isConstraint(err) {
			return usererr.New("This career URL is already saved for another company.")
		}
		return err
	})
}

// RemoveSource deletes a company and its jobs.
func (s *Store) RemoveSource(ctx context.Context, id int64) error {
	return s.tx(ctx, func(tx *sql.Tx) error {
		if err := seedSequence(tx); err != nil {
			return err
		}
		_, err := tx.Exec("DELETE FROM sources WHERE id = ?", id)
		return err
	})
}

// ImportRowEntry is one validated company to import.
type ImportRowEntry struct {
	Company, URL, Kind string
}

// ImportSources inserts one reviewed batch atomically, rechecking duplicates
// at commit time.
func (s *Store) ImportSources(ctx context.Context, rows []ImportRowEntry, locations string) (map[string]int, error) {
	imported, skipped := 0, 0
	err := s.tx(ctx, func(tx *sql.Tx) error {
		names, urls := map[string]bool{}, map[string]bool{}
		existing, err := tx.Query("SELECT name, url FROM sources")
		if err != nil {
			return err
		}
		for existing.Next() {
			var name, link string
			if err := existing.Scan(&name, &link); err != nil {
				existing.Close()
				return err
			}
			names[NameKey(name)], urls[URLKey(link)] = true, true
		}
		existing.Close()
		for _, row := range rows {
			if names[NameKey(row.Company)] || urls[URLKey(row.URL)] {
				skipped++
				continue
			}
			if _, err := tx.Exec("INSERT INTO sources (name,url,kind,locations,created_at) VALUES (?,?,?,?,?)",
				row.Company, row.URL, row.Kind, locations, time.Now().Unix()); err != nil {
				return err
			}
			names[NameKey(row.Company)], urls[URLKey(row.URL)] = true, true
			imported++
		}
		return nil
	})
	return map[string]int{"imported": imported, "skipped": skipped}, err
}

// SourceResult records the outcome of reading one company.
func (s *Store) SourceResult(ctx context.Context, id int64, count *int, failure *string) error {
	_, err := s.db.ExecContext(ctx, "UPDATE sources SET last_run_at = ?, last_count = COALESCE(?, last_count), last_error = ? WHERE id = ?",
		time.Now().Unix(), count, failure, id)
	return err
}

// ── jobs ──

// Listing is one job as a career site lists it.
type Listing struct {
	ExternalID   string `json:"external_id"`
	Title        string `json:"title"`
	Location     string `json:"location"`
	URL          string `json:"url"`
	Posted       string `json:"posted"`
	Requirements string `json:"requirements"`
	Description  string `json:"description"`
	// NeedsDetail is the URL to fetch for the full posting, if any.
	NeedsDetail string `json:"needs_detail,omitempty"`
	SavedID     int64  `json:"-"`
}

// Detail is what a posting's detail page adds.
type Detail struct {
	Title, Location, Requirements, Description, Posted string
}

// DismissExternal makes sure a job is never saved again.
func (s *Store) DismissExternal(ctx context.Context, sourceID int64, externalID string) error {
	_, err := s.db.ExecContext(ctx, "INSERT OR IGNORE INTO dismissed_jobs (source_id, external_id) VALUES (?, ?)", sourceID, externalID)
	return err
}

// IDsByExternal maps a source's external IDs to saved job IDs.
func (s *Store) IDsByExternal(ctx context.Context, sourceID int64, externalIDs []string) (map[string]int64, error) {
	out := map[string]int64{}
	if len(externalIDs) == 0 {
		return out, nil
	}
	args := []any{sourceID}
	for _, id := range externalIDs {
		args = append(args, id)
	}
	rows, err := s.db.QueryContext(ctx, "SELECT id, external_id FROM jobs WHERE source_id=? AND external_id IN ("+marks(len(externalIDs))+")", args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var id int64
		var ext string
		if err := rows.Scan(&id, &ext); err != nil {
			return nil, err
		}
		out[ext] = id
	}
	return out, rows.Err()
}

func marks(n int) string {
	return strings.TrimSuffix(strings.Repeat("?,", n), ",")
}

// KnownIDs are a source's saved and dismissed external IDs.
func (s *Store) KnownIDs(ctx context.Context, sourceID int64) (map[string]bool, error) {
	rows, err := s.db.QueryContext(ctx, "SELECT external_id FROM jobs WHERE source_id=? UNION SELECT external_id FROM dismissed_jobs WHERE source_id=?", sourceID, sourceID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]bool{}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out[id] = true
	}
	return out, rows.Err()
}

// SaveListing upserts one source's current listing and returns the IDs of
// jobs not seen before. Jobs missing from the listing keep their status;
// active describes the latest listing only.
func (s *Store) SaveListing(ctx context.Context, sourceID int64, items []Listing) ([]int64, error) {
	now := time.Now().Unix()
	var fresh []int64
	err := s.tx(ctx, func(tx *sql.Tx) error {
		dismissed := map[string]bool{}
		deletedURLs := map[string]bool{}
		rows, err := tx.Query("SELECT external_id FROM dismissed_jobs WHERE source_id=?", sourceID)
		if err != nil {
			return err
		}
		for rows.Next() {
			var v string
			if err := rows.Scan(&v); err != nil {
				rows.Close()
				return err
			}
			dismissed[v] = true
			if strings.HasPrefix(v, "https://") || strings.HasPrefix(v, "http://") {
				deletedURLs[jobURLKey(v)] = true
			}
		}
		rows.Close()
		savedURLs := map[string]int64{}
		rows, err = tx.Query("SELECT id, url FROM jobs WHERE source_id=?", sourceID)
		if err != nil {
			return err
		}
		for rows.Next() {
			var id int64
			var link string
			if err := rows.Scan(&id, &link); err != nil {
				rows.Close()
				return err
			}
			if link != "" {
				savedURLs[jobURLKey(link)] = id
			}
		}
		rows.Close()

		var seen []any
		for _, j := range items {
			canonical := jobURLKey(j.URL)
			if dismissed[j.ExternalID] || (canonical != "" && deletedURLs[canonical]) {
				continue
			}
			var id int64
			found := tx.QueryRow("SELECT id FROM jobs WHERE source_id = ? AND external_id = ?", sourceID, j.ExternalID).Scan(&id) == nil
			if !found && j.URL != "" {
				if saved, ok := savedURLs[canonical]; ok {
					id, found = saved, true
					if _, err := tx.Exec("UPDATE jobs SET external_id=? WHERE id=?", j.ExternalID, id); err != nil {
						return err
					}
				}
			}
			if found {
				var title, reqs, desc string
				if err := tx.QueryRow("SELECT title, requirements, description FROM jobs WHERE id=?", id).Scan(&title, &reqs, &desc); err != nil {
					return err
				}
				if title != j.Title || (j.Requirements != "" && reqs != j.Requirements) || (j.Description != "" && desc != j.Description) {
					if _, err := tx.Exec("UPDATE jobs SET min_years=NULL, reason=NULL, checked_by=NULL, eligible=NULL, relevant=NULL, relevance_reason=NULL WHERE id=?", id); err != nil {
						return err
					}
				}
				if _, err := tx.Exec("UPDATE jobs SET title=?, location=COALESCE(NULLIF(?, ''), location), url=?, posted=COALESCE(NULLIF(?, ''), posted), "+
					"last_seen_at=?, active=1 WHERE id=?", j.Title, j.Location, j.URL, j.Posted, now, id); err != nil {
					return err
				}
				if _, err := tx.Exec("UPDATE jobs SET requirements=COALESCE(NULLIF(?, ''), requirements), description=COALESCE(NULLIF(?, ''), description) WHERE id=?",
					j.Requirements, j.Description, id); err != nil {
					return err
				}
				seen = append(seen, id)
				savedURLs[canonical] = id
				continue
			}
			if err := seedSequence(tx); err != nil {
				return err
			}
			if _, err := tx.Exec("UPDATE meta SET value=MAX(CAST(value AS INTEGER), (SELECT COALESCE(MAX(id),0) FROM jobs))+1 WHERE key='job_id_sequence'"); err != nil {
				return err
			}
			var next string
			if err := tx.QueryRow("SELECT value FROM meta WHERE key='job_id_sequence'").Scan(&next); err != nil {
				return err
			}
			newID, _ := strconv.ParseInt(next, 10, 64)
			if _, err := tx.Exec("INSERT INTO jobs (id, source_id, external_id, title, location, url, posted, requirements, "+
				"description, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
				newID, sourceID, j.ExternalID, j.Title, j.Location, j.URL, j.Posted, j.Requirements, j.Description, now, now); err != nil {
				return err
			}
			seen = append(seen, newID)
			fresh = append(fresh, newID)
			if canonical != "" {
				savedURLs[canonical] = newID
			}
		}
		query := "UPDATE jobs SET active = 0 WHERE source_id = ?"
		if len(seen) > 0 {
			query += " AND id NOT IN (" + marks(len(seen)) + ")"
		}
		_, err = tx.Exec(query, append([]any{sourceID}, seen...)...)
		return err
	})
	return fresh, err
}

// SetDetail stores a posting's detail page. Changed text clears earlier
// verdicts so the job is checked again.
func (s *Store) SetDetail(ctx context.Context, id int64, d Detail) error {
	return s.tx(ctx, func(tx *sql.Tx) error {
		var title, location, reqs, desc string
		err := tx.QueryRow("SELECT title, location, requirements, description FROM jobs WHERE id=?", id).Scan(&title, &location, &reqs, &desc)
		if errors.Is(err, sql.ErrNoRows) {
			return nil // Deleted while its page was being fetched.
		}
		if err != nil {
			return err
		}
		newReqs, newDesc, newTitle, newLoc := or(d.Requirements, reqs), or(d.Description, desc), or(d.Title, title), or(d.Location, location)
		if newTitle != title || newReqs != reqs || newDesc != desc {
			if _, err := tx.Exec("UPDATE jobs SET min_years=NULL, eligible=NULL, reason=NULL, checked_by=NULL, relevant=NULL, relevance_reason=NULL WHERE id=?", id); err != nil {
				return err
			}
		}
		_, err = tx.Exec("UPDATE jobs SET title=?, location=?, requirements=?, description=?, posted=COALESCE(NULLIF(?, ''), posted) WHERE id=?",
			newTitle, newLoc, newReqs, newDesc, d.Posted, id)
		return err
	})
}

func or(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

// savedJob is a stored job row, as the refresh logic reads it.
type savedJob struct {
	ID, SourceID       int64
	ExternalID, Title  string
	Location, URL      string
	Posted, Status     string
	Requirements, Desc string
	Active             bool
	Manual, Relevant   *string
	Eligible           *string
	DetailAttempted    int64
	Locations          string
}

// DetailCandidates are saved jobs of a source whose detail page is still
// worth fetching, least recently attempted first.
func (s *Store) DetailCandidates(ctx context.Context, sourceID int64, missingLocations map[string]bool) ([]savedJob, error) {
	rows, err := s.db.QueryContext(ctx, "SELECT id, external_id, title, location, url, posted, requirements, active, manual_eligible, relevant, detail_attempted_at FROM jobs WHERE source_id=?", sourceID)
	if err != nil {
		return nil, err
	}
	var all []savedJob
	for rows.Next() {
		var j savedJob
		if err := rows.Scan(&j.ID, &j.ExternalID, &j.Title, &j.Location, &j.URL, &j.Posted, &j.Requirements, &j.Active,
			&j.Manual, &j.Relevant, &j.DetailAttempted); err != nil {
			rows.Close()
			return nil, err
		}
		all = append(all, j)
	}
	rows.Close()
	var locations string
	_ = s.db.QueryRowContext(ctx, "SELECT locations FROM sources WHERE id=?", sourceID).Scan(&locations)
	var out []savedJob
	for _, r := range all {
		missing := missingLocations[r.ExternalID]
		if is(r.Manual, "no") || is(r.Relevant, "no") {
			continue
		}
		if !(missing || r.Location == "" || LocationMatches(r.Location, locations)) {
			continue
		}
		age := Freshness(r.Posted)
		if (r.Active && missing) || age == "unknown" || (r.Active && r.Requirements == "" && age != "expired") {
			out = append(out, r)
		}
	}
	slices.SortStableFunc(out, func(a, b savedJob) int { return int(a.DetailAttempted - b.DetailAttempted) })
	return out, nil
}

// NoteDetailAttempt records that a posting's detail page was tried.
func (s *Store) NoteDetailAttempt(ctx context.Context, sourceID int64, externalID string) error {
	_, err := s.db.ExecContext(ctx, "UPDATE jobs SET detail_attempted_at=? WHERE source_id=? AND external_id=?", time.Now().UnixNano(), sourceID, externalID)
	return err
}

// ReviewJob is a job waiting for its experience and relevance check.
type ReviewJob struct {
	ID           int64  `json:"id"`
	SourceID     int64  `json:"source_id"`
	Title        string `json:"title"`
	Location     string `json:"location"`
	Requirements string `json:"requirements"`
	Description  string `json:"description"`
	Posted       string `json:"posted"`
	Status       string `json:"status"`
}

// Unchecked lists jobs that still need a verdict, newest first. sourceIDs
// nil means every source.
func (s *Store) Unchecked(ctx context.Context, limit int, sourceIDs []int64, exclude map[int64]bool, withText bool) ([]ReviewJob, error) {
	if sourceIDs != nil && len(sourceIDs) == 0 {
		return nil, nil
	}
	text := "'' AS requirements, '' AS description, "
	if withText {
		text = "j.requirements, j.description, "
	}
	query := "SELECT j.id, j.source_id, j.title, j.location, " + text + "j.posted, j.status, s.locations " +
		"FROM jobs j JOIN sources s ON s.id=j.source_id " +
		"WHERE (j.eligible IS NULL OR j.relevant IS NULL) AND j.status != 'hidden' AND j.manual_eligible IS NULL " +
		"AND (j.eligible IS NULL OR j.eligible != 'no') AND (j.relevant IS NULL OR j.relevant != 'no')"
	var args []any
	if sourceIDs != nil {
		query += " AND j.source_id IN (" + marks(len(sourceIDs)) + ")"
		for _, id := range sourceIDs {
			args = append(args, id)
		}
	}
	end := Measure(ctx, "database")
	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		end(true)
		return nil, err
	}
	defer rows.Close()
	var out []ReviewJob
	for rows.Next() {
		var j ReviewJob
		var locations string
		if err := rows.Scan(&j.ID, &j.SourceID, &j.Title, &j.Location, &j.Requirements, &j.Description, &j.Posted, &j.Status, &locations); err != nil {
			end(true)
			return nil, err
		}
		if !exclude[j.ID] && (j.Status == "applied" || LocationMatches(j.Location, locations)) {
			out = append(out, j)
		}
	}
	end(false)
	out = NewestFirst(out, func(j ReviewJob) string { return j.Posted })
	if limit > 0 && len(out) > limit {
		out = out[:limit]
	}
	return out, rows.Err()
}

// Verdict is the result of checking one job.
type Verdict struct {
	MinYears        *float64 `json:"min_years"`
	Eligible        string   `json:"eligible"`
	Reason          string   `json:"reason"`
	By              string   `json:"by"`
	Relevant        *string  `json:"relevant,omitempty"`
	RelevanceReason string   `json:"relevance_reason,omitempty"`
	Posted          string   `json:"posted,omitempty"`
}

// SetEligibility saves verdicts. A verdict without relevance (the text-rule
// fallback) leaves relevance pending for a later AI run; an AI-read date
// fills only a job that had none.
func (s *Store) SetEligibility(ctx context.Context, verdicts map[int64]Verdict) error {
	return s.tx(ctx, func(tx *sql.Tx) error {
		for id, v := range verdicts {
			if _, err := tx.Exec("UPDATE jobs SET min_years=?, eligible=?, reason=?, checked_by=?, relevant=?, "+
				"relevance_reason=?, posted=CASE WHEN posted='' AND ?<>'' THEN ? ELSE posted END WHERE id=?",
				v.MinYears, v.Eligible, v.Reason, v.By, v.Relevant, v.RelevanceReason, v.Posted, v.Posted, id); err != nil {
				return err
			}
		}
		return nil
	})
}

// GuardSaved rechecks every "yes" verdict with the code guard and queues old
// code vetoes for a fresh AI review. It returns how many verdicts changed.
func (s *Store) GuardSaved(ctx context.Context, guard func(v Verdict, title, text string) Verdict) (int, error) {
	changed := 0
	err := s.tx(ctx, func(tx *sql.Tx) error {
		if _, err := tx.Exec("UPDATE jobs SET eligible=NULL, min_years=NULL, reason=NULL, checked_by=NULL " +
			"WHERE manual_eligible IS NULL AND eligible IN ('no','unclear') AND " +
			"(reason LIKE 'Posting asks for %' OR reason LIKE 'Mentions %years; check whether%')"); err != nil {
			return err
		}
		rows, err := tx.Query("SELECT id, title, requirements, description, min_years, eligible, reason FROM jobs WHERE eligible='yes'")
		if err != nil {
			return err
		}
		type row struct {
			id                int64
			title, reqs, desc string
			before            Verdict
		}
		var all []row
		for rows.Next() {
			var r row
			var reason *string
			if err := rows.Scan(&r.id, &r.title, &r.reqs, &r.desc, &r.before.MinYears, &r.before.Eligible, &reason); err != nil {
				rows.Close()
				return err
			}
			if reason != nil {
				r.before.Reason = *reason
			}
			all = append(all, r)
		}
		rows.Close()
		for _, r := range all {
			after := guard(r.before, r.title, r.reqs+"\n"+r.desc)
			if after.Eligible != r.before.Eligible || after.Reason != r.before.Reason || !sameFloat(after.MinYears, r.before.MinYears) {
				if _, err := tx.Exec("UPDATE jobs SET min_years=?, eligible=?, reason=? WHERE id=?", after.MinYears, after.Eligible, after.Reason, r.id); err != nil {
					return err
				}
				changed++
			}
		}
		return nil
	})
	return changed, err
}

func sameFloat(a, b *float64) bool {
	return (a == nil && b == nil) || (a != nil && b != nil && *a == *b)
}

// ResetEligibility clears every verdict, for example after the experience changes.
func (s *Store) ResetEligibility(ctx context.Context) error {
	_, err := s.db.ExecContext(ctx, "UPDATE jobs SET min_years=NULL, eligible=NULL, reason=NULL, checked_by=NULL")
	return err
}

// JobRow is a job as the Jobs page lists it.
type JobRow struct {
	ID              int64    `json:"id"`
	Title           string   `json:"title"`
	Location        string   `json:"location"`
	URL             string   `json:"url"`
	Posted          string   `json:"posted"`
	FirstSeenAt     int64    `json:"first_seen_at"`
	Status          string   `json:"status"`
	MinYears        *float64 `json:"min_years"`
	Eligible        *string  `json:"eligible"`
	Reason          *string  `json:"reason"`
	CheckedBy       *string  `json:"checked_by"`
	Relevant        *string  `json:"relevant"`
	RelevanceReason *string  `json:"relevance_reason"`
	ManualEligible  *string  `json:"manual_eligible"`
	Active          int64    `json:"active"`
	Company         string   `json:"company"`
	SourceID        int64    `json:"source_id"`
	Locations       string   `json:"locations"`
	Freshness       string   `json:"freshness"`
	Category        string   `json:"category"`
}

// Jobs lists every job the user should see, newest first.
func (s *Store) Jobs(ctx context.Context) ([]JobRow, error) {
	end := Measure(ctx, "database")
	rows, err := s.db.QueryContext(ctx, "SELECT j.id, j.title, j.location, j.url, j.posted, j.first_seen_at, j.status, j.min_years, j.eligible, "+
		"j.reason, j.checked_by, j.relevant, j.relevance_reason, j.manual_eligible, j.active, s.name AS company, s.id AS source_id, s.locations "+
		"FROM jobs j JOIN sources s ON s.id = j.source_id ORDER BY j.id DESC")
	if err != nil {
		end(true)
		return nil, err
	}
	defer rows.Close()
	var all []JobRow
	for rows.Next() {
		var j JobRow
		if err := rows.Scan(&j.ID, &j.Title, &j.Location, &j.URL, &j.Posted, &j.FirstSeenAt, &j.Status, &j.MinYears, &j.Eligible,
			&j.Reason, &j.CheckedBy, &j.Relevant, &j.RelevanceReason, &j.ManualEligible, &j.Active, &j.Company, &j.SourceID, &j.Locations); err != nil {
			end(true)
			return nil, err
		}
		j.Freshness = Freshness(j.Posted)
		all = append(all, j)
	}
	end(false)
	out := []JobRow{}
	for _, j := range NewestFirst(all, func(j JobRow) string { return j.Posted }) {
		if j.Status != "applied" && !LocationMatches(j.Location, j.Locations) {
			continue
		}
		if j.Status != "applied" {
			j.Location = matchingLocationLabel(j.Location, j.Locations)
		}
		j.Category = Category(j.Status, j.Posted, j.ManualEligible, j.Relevant, j.Eligible)
		out = append(out, j)
	}
	return out, rows.Err()
}

// JobDetail is one job's full posting.
type JobDetail struct {
	ID          int64  `json:"id"`
	Title       string `json:"title"`
	Company     string `json:"company"`
	Location    string `json:"location"`
	URL         string `json:"url"`
	Description string `json:"description"`
}

// Job returns one job's posting.
func (s *Store) Job(ctx context.Context, id int64) (*JobDetail, error) {
	var j JobDetail
	err := s.db.QueryRowContext(ctx, "SELECT j.id, j.title, s.name, j.location, j.url, j.description FROM jobs j JOIN sources s ON s.id = j.source_id WHERE j.id = ?", id).
		Scan(&j.ID, &j.Title, &j.Company, &j.Location, &j.URL, &j.Description)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, usererr.New("This job is no longer listed.")
	}
	return &j, err
}

func dismissRow(tx *sql.Tx, sourceID int64, externalID, link string) error {
	values := []string{externalID}
	if link != "" && jobURLKey(link) != externalID {
		values = append(values, jobURLKey(link))
	}
	for _, v := range values {
		if _, err := tx.Exec("INSERT OR IGNORE INTO dismissed_jobs (source_id, external_id) VALUES (?, ?)", sourceID, v); err != nil {
			return err
		}
	}
	return nil
}

// DeleteJob removes a job and makes sure it is never saved again.
func (s *Store) DeleteJob(ctx context.Context, id int64) error {
	return s.tx(ctx, func(tx *sql.Tx) error {
		if err := seedSequence(tx); err != nil {
			return err
		}
		var sourceID int64
		var ext, link string
		if err := tx.QueryRow("SELECT source_id, external_id, url FROM jobs WHERE id=?", id).Scan(&sourceID, &ext, &link); err != nil {
			return usererr.New("This job has already been deleted.")
		}
		if err := dismissRow(tx, sourceID, ext, link); err != nil {
			return err
		}
		_, err := tx.Exec("DELETE FROM jobs WHERE id=?", id)
		return err
	})
}

// DeleteCategory deletes every visible job in one category.
func (s *Store) DeleteCategory(ctx context.Context, group any) error {
	name, _ := group.(string)
	if !slices.Contains([]string{"eligible", "no", "notrel", "applied", "review", "old"}, name) {
		return usererr.New("Choose a jobs category.")
	}
	return s.tx(ctx, func(tx *sql.Tx) error {
		if err := seedSequence(tx); err != nil {
			return err
		}
		rows, err := tx.Query("SELECT j.id, j.source_id, j.external_id, j.url, j.location, j.posted, j.status, j.manual_eligible, j.relevant, j.eligible, s.locations FROM jobs j JOIN sources s ON s.id=j.source_id")
		if err != nil {
			return err
		}
		type row struct {
			id, source               int64
			ext, link, loc, posted   string
			status, locations        string
			manual, relevant, eligib *string
		}
		var all []row
		for rows.Next() {
			var r row
			if err := rows.Scan(&r.id, &r.source, &r.ext, &r.link, &r.loc, &r.posted, &r.status, &r.manual, &r.relevant, &r.eligib, &r.locations); err != nil {
				rows.Close()
				return err
			}
			all = append(all, r)
		}
		rows.Close()
		for _, r := range all {
			if Category(r.status, r.posted, r.manual, r.relevant, r.eligib) == name && (r.status == "applied" || LocationMatches(r.loc, r.locations)) {
				if err := dismissRow(tx, r.source, r.ext, r.link); err != nil {
					return err
				}
				if _, err := tx.Exec("DELETE FROM jobs WHERE id=?", r.id); err != nil {
					return err
				}
			}
		}
		return nil
	})
}

// PruneOld deletes postings older than eight days, except applied ones.
func (s *Store) PruneOld(ctx context.Context) error {
	cutoff := today().AddDate(0, 0, -8)
	return s.tx(ctx, func(tx *sql.Tx) error {
		if err := seedSequence(tx); err != nil {
			return err
		}
		rows, err := tx.Query("SELECT id, source_id, external_id, url, posted FROM jobs WHERE status!='applied'")
		if err != nil {
			return err
		}
		type row struct {
			id, source        int64
			ext, link, posted string
		}
		var old []row
		for rows.Next() {
			var r row
			if err := rows.Scan(&r.id, &r.source, &r.ext, &r.link, &r.posted); err != nil {
				rows.Close()
				return err
			}
			if d, ok := PostingDate(r.posted); ok && d.Before(cutoff) {
				old = append(old, r)
			}
		}
		rows.Close()
		for _, r := range old {
			if err := dismissRow(tx, r.source, r.ext, r.link); err != nil {
				return err
			}
			if _, err := tx.Exec("DELETE FROM jobs WHERE id=? AND status!='applied'", r.id); err != nil {
				return err
			}
		}
		return nil
	})
}

// SetManualEligibility marks a job not eligible ("no"), or undoes that (nil).
func (s *Store) SetManualEligibility(ctx context.Context, id int64, verdict any) error {
	if verdict != nil && verdict != "no" {
		return usererr.New("Choose not eligible, or undo the manual decision.")
	}
	res, err := s.db.ExecContext(ctx, "UPDATE jobs SET manual_eligible=? WHERE id=?", verdict, id)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return usererr.New("This job is no longer saved.")
	}
	return nil
}

// SetStatus changes a job's status.
func (s *Store) SetStatus(ctx context.Context, id int64, status any) error {
	value, _ := status.(string)
	if !slices.Contains(Statuses, value) {
		return usererr.New("Unknown job status.")
	}
	_, err := s.db.ExecContext(ctx, "UPDATE jobs SET status = ? WHERE id = ?", value, id)
	return err
}

// Meta reads one setting; ok is false when it is not set.
func (s *Store) Meta(ctx context.Context, key string) (value string, ok bool, err error) {
	var v *string
	err = s.db.QueryRowContext(ctx, "SELECT value FROM meta WHERE key = ?", key).Scan(&v)
	if errors.Is(err, sql.ErrNoRows) {
		return "", false, nil
	}
	if err != nil || v == nil {
		return "", false, err
	}
	return *v, true, nil
}

// SetMeta saves one setting; nil clears it (stored as NULL, like Python).
func (s *Store) SetMeta(ctx context.Context, key string, value *string) error {
	_, err := s.db.ExecContext(ctx, "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, value)
	return err
}

// ResetTestCollection empties a scratch store and copies companies in.
func (s *Store) ResetTestCollection(ctx context.Context, sources []Source, years *string) error {
	return s.tx(ctx, func(tx *sql.Tx) error {
		for _, stmt := range []string{"DELETE FROM jobs", "DELETE FROM dismissed_jobs", "DELETE FROM sources",
			"DELETE FROM meta WHERE key IN ('last_refresh', 'last_review', 'experience_years')"} {
			if _, err := tx.Exec(stmt); err != nil {
				return err
			}
		}
		for _, src := range sources {
			if _, err := tx.Exec("INSERT INTO sources (name,url,kind,include,exclude,locations,created_at) VALUES (?,?,?,?,?,?,?)",
				src.Name, src.URL, src.Kind, src.Include, src.Exclude, src.Locations, time.Now().Unix()); err != nil {
				return err
			}
		}
		if years != nil {
			if _, err := tx.Exec("INSERT INTO meta (key,value) VALUES ('experience_years',?)", *years); err != nil {
				return err
			}
		}
		return nil
	})
}

// SourcesByID lists sources ordered by ID (for copying into a test store).
func (s *Store) SourcesByID(ctx context.Context) ([]Source, error) {
	all, err := s.Sources(ctx)
	slices.SortFunc(all, func(a, b Source) int { return int(a.ID - b.ID) })
	return all, err
}
