"""SQLite storage for career sources and the jobs found on them."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    include TEXT NOT NULL DEFAULT '',
    exclude TEXT NOT NULL DEFAULT '',
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
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

STATUSES = ('new', 'seen', 'applied', 'hidden')


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
            with conn:
                yield conn
        finally:
            conn.close()

    # sources
    def sources(self) -> list[dict]:
        with self.db() as c:
            return [dict(r) for r in c.execute('SELECT * FROM sources ORDER BY name COLLATE NOCASE')]

    def add_source(self, name, url, kind, include='', exclude='') -> dict:
        with self.db() as c:
            try:
                cur = c.execute('INSERT INTO sources (name, url, kind, include, exclude, created_at) VALUES (?,?,?,?,?,?)',
                                (name, url, kind, include, exclude, int(time.time())))
            except sqlite3.IntegrityError:
                raise ValueError('This careers URL is already added.') from None
            return dict(c.execute('SELECT * FROM sources WHERE id = ?', (cur.lastrowid,)).fetchone())

    def remove_source(self, ident: int) -> None:
        with self.db() as c:
            c.execute('DELETE FROM sources WHERE id = ?', (ident,))

    def source_result(self, ident: int, count=None, error=None) -> None:
        with self.db() as c:
            c.execute('UPDATE sources SET last_run_at = ?, last_count = COALESCE(?, last_count), last_error = ? WHERE id = ?',
                      (int(time.time()), count, error, ident))

    # jobs
    def save_listing(self, source_id: int, items: list[dict]) -> list[int]:
        """Upsert one source's current listing. Returns ids of jobs not seen before.
        Jobs that are no longer listed become inactive; they keep their status."""
        now, new = int(time.time()), []
        with self.db() as c:
            seen = []
            for j in items:
                row = c.execute('SELECT id FROM jobs WHERE source_id = ? AND external_id = ?',
                                (source_id, j['external_id'])).fetchone()
                if row:
                    c.execute('UPDATE jobs SET title=?, location=?, url=?, posted=COALESCE(NULLIF(?, \'\'), posted), '
                              'last_seen_at=?, active=1 WHERE id=?',
                              (j['title'], j['location'], j['url'], j['posted'], now, row['id']))
                    if j['requirements']:
                        c.execute("UPDATE jobs SET requirements=?, description=? WHERE id=? AND requirements=''",
                                  (j['requirements'], j['description'], row['id']))
                    seen.append(row['id'])
                else:
                    cur = c.execute('INSERT INTO jobs (source_id, external_id, title, location, url, posted, requirements, '
                                    'description, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                                    (source_id, j['external_id'], j['title'], j['location'], j['url'], j['posted'],
                                     j['requirements'], j['description'], now, now))
                    seen.append(cur.lastrowid)
                    new.append(cur.lastrowid)
            marks = ','.join('?' * len(seen))
            c.execute(f'UPDATE jobs SET active = 0 WHERE source_id = ?' + (f' AND id NOT IN ({marks})' if seen else ''),
                      (source_id, *seen))
        return new

    def set_detail(self, ident: int, detail: dict) -> None:
        with self.db() as c:
            c.execute("UPDATE jobs SET requirements=?, description=?, posted=COALESCE(NULLIF(?, ''), posted) WHERE id=?",
                      (detail.get('requirements', ''), detail.get('description', ''), detail.get('posted', ''), ident))

    def unchecked(self, limit: int) -> list[dict]:
        with self.db() as c:
            return [dict(r) for r in c.execute(
                "SELECT id, title, requirements FROM jobs WHERE active = 1 AND eligible IS NULL AND status != 'hidden' "
                'ORDER BY first_seen_at DESC LIMIT ?', (limit,))]

    def set_eligibility(self, verdicts: dict) -> None:
        with self.db() as c:
            c.executemany('UPDATE jobs SET min_years=?, eligible=?, reason=?, checked_by=? WHERE id=?',
                          [(v['min_years'], v['eligible'], v['reason'], v['by'], ident) for ident, v in verdicts.items()])

    def reset_eligibility(self) -> None:
        with self.db() as c:
            c.execute('UPDATE jobs SET min_years=NULL, eligible=NULL, reason=NULL, checked_by=NULL WHERE active = 1')

    def jobs(self, limit=1000) -> list[dict]:
        with self.db() as c:
            return [dict(r) for r in c.execute(
                'SELECT j.id, j.title, j.location, j.url, j.posted, j.first_seen_at, j.status, j.min_years, j.eligible, '
                'j.reason, j.checked_by, s.name AS company, s.id AS source_id FROM jobs j JOIN sources s ON s.id = j.source_id '
                'WHERE j.active = 1 ORDER BY j.first_seen_at DESC, j.posted DESC, j.id DESC LIMIT ?', (limit,))]

    def job(self, ident: int) -> dict:
        with self.db() as c:
            row = c.execute('SELECT j.*, s.name AS company FROM jobs j JOIN sources s ON s.id = j.source_id WHERE j.id = ?',
                            (ident,)).fetchone()
        if not row:
            raise ValueError('This job is no longer listed.')
        return dict(row)

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
