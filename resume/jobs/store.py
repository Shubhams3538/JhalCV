"""SQLite storage for career sources and the jobs found on them."""
from __future__ import annotations

import sqlite3
import re
import time
from datetime import date, timedelta
from contextlib import contextmanager
from pathlib import Path

from .freshness import category, freshness, newest_first, posting_date
from .imports import name_key, url_key

SCHEMA = """
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
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

STATUSES = ('new', 'seen', 'applied', 'hidden')


def location_matches(location: str, locations: str) -> bool:
    terms = [part.strip().casefold() for part in locations.split(',') if part.strip()]
    if 'india' in terms:
        terms.append('ind')  # ISO country code used by several official boards.
    value = location.casefold()
    return not terms or any(re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', value) for term in terms)


def matching_location_label(location: str, locations: str) -> str:
    if not locations:
        return location
    matches = [part.strip() for part in location.split(';') if location_matches(part, locations)]
    return '; '.join(matches) or location


class JobStore:
    def __init__(self, root):
        self.path = Path(root) / '.local' / 'jobs.db'

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        conn.executescript(SCHEMA)
        try:
            # Serialize additive migration, including callers from older installations.
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                columns = {r['name'] for r in conn.execute('PRAGMA table_info(jobs)')}
                source_columns = {r['name'] for r in conn.execute('PRAGMA table_info(sources)')}
                if 'locations' not in source_columns:
                    conn.execute("ALTER TABLE sources ADD COLUMN locations TEXT NOT NULL DEFAULT ''")
                legacy_dates = 'relevant' not in columns
                for name in ('relevant', 'relevance_reason'):
                    if name not in columns:
                        conn.execute(f'ALTER TABLE jobs ADD COLUMN {name} TEXT')
                if legacy_dates:
                    # Older Greenhouse readers substituted updated_at for posting time.
                    # The provenance is lost: quarantine until a refresh supplies first_published.
                    conn.execute("UPDATE jobs SET posted='' WHERE source_id IN (SELECT id FROM sources WHERE kind='greenhouse')")
                if 'detail_attempted_at' not in columns:
                    conn.execute('ALTER TABLE jobs ADD COLUMN detail_attempted_at INTEGER NOT NULL DEFAULT 0')
                self._seed_job_sequence(conn)
            with conn:
                yield conn
        finally:
            conn.close()

    # sources
    def sources(self) -> list[dict]:
        with self.db() as c:
            return [dict(r) for r in c.execute('SELECT * FROM sources ORDER BY name COLLATE NOCASE')]

    def add_source(self, name, url, kind, include='', exclude='', locations='') -> dict:
        with self.db() as c:
            try:
                cur = c.execute('INSERT INTO sources (name, url, kind, include, exclude, locations, created_at) VALUES (?,?,?,?,?,?,?)',
                                (name, url, kind, include, exclude, locations, int(time.time())))
            except sqlite3.IntegrityError:
                raise ValueError('This careers URL is already added.') from None
            return dict(c.execute('SELECT * FROM sources WHERE id = ?', (cur.lastrowid,)).fetchone())

    def set_source_locations(self, ident: int, locations: str) -> None:
        with self.db() as c:
            if not c.execute('UPDATE sources SET locations=? WHERE id=?', (locations, ident)).rowcount:
                raise ValueError('This careers page is no longer saved.')

    def remove_source(self, ident: int) -> None:
        with self.db() as c:
            c.execute('DELETE FROM sources WHERE id = ?', (ident,))

    def import_sources(self, rows, locations):
        """Insert one reviewed batch atomically; recheck duplicates at commit time."""
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            existing = list(c.execute('SELECT name, url FROM sources'))
            names = {name_key(s['name']) for s in existing}
            urls = {url_key(s['url']) for s in existing}
            imported = skipped = 0
            for row in rows:
                name, url = row['company'], row['url']
                if name_key(name) in names or url_key(url) in urls:
                    skipped += 1
                    continue
                c.execute('INSERT INTO sources (name,url,kind,locations,created_at) VALUES (?,?,?,?,?)',
                          (name, url, row['kind'], locations, int(time.time())))
                names.add(name_key(name))
                urls.add(url_key(url))
                imported += 1
        return {'imported': imported, 'skipped': skipped}

    def source_result(self, ident: int, count=None, error=None) -> None:
        with self.db() as c:
            c.execute('UPDATE sources SET last_run_at = ?, last_count = COALESCE(?, last_count), last_error = ? WHERE id = ?',
                      (int(time.time()), count, error, ident))

    # jobs
    def dismiss_external(self, source_id, external_id):
        with self.db() as c:
            c.execute('INSERT OR IGNORE INTO dismissed_jobs (source_id, external_id) VALUES (?, ?)',
                      (source_id, external_id))

    def known_ids(self, source_id: int) -> set[str]:
        with self.db() as c:
            return {r[0] for r in c.execute('SELECT external_id FROM jobs WHERE source_id=? UNION '
                                          'SELECT external_id FROM dismissed_jobs WHERE source_id=?',
                                          (source_id, source_id))}

    def save_listing(self, source_id: int, items: list[dict]) -> list[int]:
        """Upsert one source's current listing. Returns ids of jobs not seen before.
        Jobs absent from a refresh are retained with their status; active describes the latest listing only."""
        now, new = int(time.time()), []
        with self.db() as c:
            seen = []
            for j in items:
                if c.execute('SELECT 1 FROM dismissed_jobs WHERE source_id=? AND external_id=?',
                             (source_id, j['external_id'])).fetchone():
                    continue
                row = c.execute('SELECT id FROM jobs WHERE source_id = ? AND external_id = ?',
                                (source_id, j['external_id'])).fetchone()
                if row:
                    old = c.execute('SELECT title, requirements, description FROM jobs WHERE id=?', (row['id'],)).fetchone()
                    if old['title'] != j['title'] or (j['requirements'] and old['requirements'] != j['requirements']) or (j['description'] and old['description'] != j['description']):
                        c.execute('UPDATE jobs SET min_years=NULL, reason=NULL, checked_by=NULL, eligible=NULL, relevant=NULL, relevance_reason=NULL WHERE id=?', (row['id'],))
                    c.execute('UPDATE jobs SET title=?, location=?, url=?, posted=COALESCE(NULLIF(?, \'\'), posted), '
                              'last_seen_at=?, active=1 WHERE id=?',
                              (j['title'], j['location'], j['url'], j['posted'], now, row['id']))
                    c.execute("UPDATE jobs SET requirements=COALESCE(NULLIF(?, ''), requirements), "
                              "description=COALESCE(NULLIF(?, ''), description) WHERE id=?",
                              (j['requirements'], j['description'], row['id']))
                    seen.append(row['id'])
                else:
                    self._seed_job_sequence(c)
                    c.execute("UPDATE meta SET value=MAX(CAST(value AS INTEGER), (SELECT COALESCE(MAX(id),0) FROM jobs))+1 WHERE key='job_id_sequence'")
                    ident = int(c.execute("SELECT value FROM meta WHERE key='job_id_sequence'").fetchone()[0])
                    cur = c.execute('INSERT INTO jobs (id, source_id, external_id, title, location, url, posted, requirements, '
                                    'description, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                                    (ident, source_id, j['external_id'], j['title'], j['location'], j['url'], j['posted'],
                                     j['requirements'], j['description'], now, now))
                    seen.append(cur.lastrowid)
                    new.append(cur.lastrowid)
            marks = ','.join('?' * len(seen))
            c.execute(f'UPDATE jobs SET active = 0 WHERE source_id = ?' + (f' AND id NOT IN ({marks})' if seen else ''),
                      (source_id, *seen))
        return new

    def set_detail(self, ident: int, detail: dict) -> None:
        with self.db() as c:
            old = c.execute('SELECT requirements, description FROM jobs WHERE id=?', (ident,)).fetchone()
            if old is None:
                return  # Explicitly deleted while its page was being fetched.
            requirements = detail.get('requirements') or old['requirements']
            description = detail.get('description') or old['description']
            if requirements != old['requirements'] or description != old['description']:
                c.execute('UPDATE jobs SET min_years=NULL, eligible=NULL, reason=NULL, checked_by=NULL, '
                          'relevant=NULL, relevance_reason=NULL WHERE id=?', (ident,))
            c.execute("UPDATE jobs SET requirements=?, description=?, posted=COALESCE(NULLIF(?, ''), posted) WHERE id=?",
                      (requirements, description, detail.get('posted', ''), ident))

    def detail_candidates(self, source_id: int) -> list[dict]:
        with self.db() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM jobs WHERE source_id=?", (source_id,))]
            source = c.execute('SELECT locations FROM sources WHERE id=?', (source_id,)).fetchone()
        locations = source['locations'] if source else ''
        return sorted([r for r in rows if location_matches(r['location'], locations) and
                       (freshness(r['posted']) == 'unknown'
                       or (r['active'] and not r['requirements'] and freshness(r['posted']) != 'expired'))],
                      key=lambda r: r['detail_attempted_at'])

    def note_detail_attempt(self, source_id, external_id):
        with self.db() as c:
            c.execute('UPDATE jobs SET detail_attempted_at=? WHERE source_id=? AND external_id=?',
                      (time.time_ns(), source_id, external_id))

    def unchecked(self, limit: int, source_ids=None) -> list[dict]:
        with self.db() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT j.id, j.source_id, j.title, j.location, j.requirements, j.description, j.posted, j.status, s.locations "
                "FROM jobs j JOIN sources s ON s.id=j.source_id "
                "WHERE (j.eligible IS NULL OR j.relevant IS NULL) AND j.status != 'hidden'")]
        rows = [r for r in rows if (source_ids is None or r['source_id'] in source_ids)
                and (r['status'] == 'applied' or location_matches(r['location'], r['locations']))]
        return newest_first(rows)[:limit]

    def set_eligibility(self, verdicts: dict) -> None:
        with self.db() as c:
            c.executemany('UPDATE jobs SET min_years=?, eligible=?, reason=?, checked_by=?, relevant=?, relevance_reason=? WHERE id=?',
                          [(v['min_years'], v['eligible'], v['reason'], v['by'], v.get('relevant', 'unclear'), v.get('relevance_reason', ''), ident) for ident, v in verdicts.items()])

    def reset_eligibility(self) -> None:
        with self.db() as c:
            c.execute('UPDATE jobs SET min_years=NULL, eligible=NULL, reason=NULL, checked_by=NULL')

    def jobs(self, limit=None) -> list[dict]:
        with self.db() as c:
            rows = [{**dict(r), 'freshness': freshness(r['posted'])} for r in c.execute(
                'SELECT j.id, j.title, j.location, j.url, j.posted, j.first_seen_at, j.status, j.min_years, j.eligible, '
                'j.reason, j.checked_by, j.relevant, j.relevance_reason, j.active, s.name AS company, s.id AS source_id, s.locations '
                'FROM jobs j JOIN sources s ON s.id = j.source_id ORDER BY j.id DESC')]
        return [{**r, 'location': matching_location_label(r['location'], r['locations'])
                 if r['status'] != 'applied' else r['location'], 'category': category(r)}
                for r in newest_first(rows)
                if r['status'] == 'applied' or location_matches(r['location'], r['locations'])][:limit]

    def job(self, ident: int) -> dict:
        with self.db() as c:
            row = c.execute('SELECT j.*, s.name AS company FROM jobs j JOIN sources s ON s.id = j.source_id WHERE j.id = ?',
                            (ident,)).fetchone()
        if not row:
            raise ValueError('This job is no longer listed.')
        return dict(row)

    @staticmethod
    def _seed_job_sequence(conn):
        # Never reuse a deleted ID: another open tab may still reference it.
        conn.execute("INSERT OR IGNORE INTO meta (key, value) SELECT 'job_id_sequence', COALESCE(MAX(id),0) FROM jobs")

    def delete_job(self, ident: int) -> None:
        with self.db() as c:
            row = c.execute('SELECT source_id, external_id FROM jobs WHERE id=?', (ident,)).fetchone()
            if row is None:
                raise ValueError('This job has already been deleted.')
            self._seed_job_sequence(c)
            c.execute('INSERT OR IGNORE INTO dismissed_jobs (source_id, external_id) VALUES (?, ?)',
                      (row['source_id'], row['external_id']))
            c.execute('DELETE FROM jobs WHERE id=?', (ident,))

    def delete_category(self, group):
        if group not in ('eligible', 'no', 'applied', 'review', 'old'):
            raise ValueError('Choose a jobs category.')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            rows = [dict(r) for r in c.execute('SELECT j.*, s.locations FROM jobs j JOIN sources s ON s.id=j.source_id')]
            for row in rows:
                if category(row) == group and (row['status'] == 'applied' or location_matches(row['location'], row['locations'])):
                    c.execute('INSERT OR IGNORE INTO dismissed_jobs (source_id, external_id) VALUES (?, ?)',
                              (row['source_id'], row['external_id']))
                    c.execute('DELETE FROM jobs WHERE id=?', (row['id'],))

    def prune_old(self, today=None):
        cutoff = (today or date.today()) - timedelta(days=8)
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            rows = list(c.execute("SELECT id, source_id, external_id, posted FROM jobs WHERE status!='applied'"))
            for row in rows:
                posted = posting_date(row['posted'])
                if posted is not None and posted < cutoff:
                    c.execute('INSERT OR IGNORE INTO dismissed_jobs (source_id, external_id) VALUES (?, ?)',
                              (row['source_id'], row['external_id']))
                    c.execute("DELETE FROM jobs WHERE id=? AND status!='applied'", (row['id'],))

    def set_status(self, ident: int, status: str) -> None:
        if status not in STATUSES:
            raise ValueError('Unknown job status.')
        with self.db() as c:
            c.execute('UPDATE jobs SET status = ? WHERE id = ?', (status, ident))

    def get_meta(self, key, default=None):
        with self.db() as c:
            row = c.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        return row['value'] if row else default

    def set_meta(self, key, value) -> None:
        with self.db() as c:
            c.execute('INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                      (key, None if value is None else str(value)))
