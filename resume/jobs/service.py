"""Refresh every career source, then check experience for jobs not yet checked."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from . import eligibility, sources
from .agents import Agents, AgentError, NAMES
from .fetch import FetchError, check_url
from .store import JobStore, location_matches
from .freshness import freshness, newest_first
from .imports import MAX_ROWS, preview_csv, validate_entry

MAX_DETAILS = 40     # detail pages fetched per source per run (new jobs only)
MAX_CHECKS = 120     # experience checks per run


def _keywords(value) -> list[str]:
    return [w.strip().lower() for w in str(value or '').split(',') if w.strip()]


def title_matches(title: str, include: str, exclude: str) -> bool:
    title = title.lower()
    inc, exc = _keywords(include), _keywords(exclude)
    return (not inc or any(w in title for w in inc)) and not any(w in title for w in exc)


class JobService:
    def __init__(self, root):
        self.root = Path(root)
        self.store = JobStore(root)
        from ..usage import UsageStore
        self.usage = UsageStore(root)
        self.lock_path = self.root / '.local' / 'jobs.lock'
        self.progress = {'running': False, 'message': '', 'finished_at': None, 'error': None}
        self._guard = threading.Lock()

    # settings
    def experience_years(self, profile: dict) -> dict:
        saved = self.store.get_meta('experience_years')
        computed = eligibility.profile_years(profile)
        return {'years': float(saved) if saved is not None else computed, 'computed': computed, 'custom': saved is not None}

    def set_experience_years(self, value) -> None:
        if value in (None, ''):
            self.store.set_meta('experience_years', None)
        else:
            try:
                years = round(float(value), 1)
            except (TypeError, ValueError):
                raise ValueError('Enter your years of experience as a number, e.g. 1.5.') from None
            if not 0 <= years <= 50:
                raise ValueError('Enter years of experience between 0 and 50.')
            self.store.set_meta('experience_years', years)
        self.store.reset_eligibility()

    # sources
    def add_source(self, name, url, include='', exclude='', locations='') -> dict:
        name = ' '.join(str(name or '').split())[:100]
        if not name:
            raise ValueError('Enter the company name.')
        url = check_url(url)
        return self.store.add_source(name, url, sources.detect(url),
                                     str(include or '')[:500], str(exclude or '')[:500],
                                     self._locations(locations))

    @staticmethod
    def _locations(value) -> str:
        return ', '.join(part.strip() for part in str(value or '')[:500].split(',') if part.strip())

    def set_source_locations(self, ident, value) -> None:
        self.store.set_source_locations(ident, self._locations(value))

    def preview_sources(self, content):
        return preview_csv(content, self.store.sources())

    def import_sources(self, rows, locations='India'):
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
            raise ValueError(f'Choose between 1 and {MAX_ROWS} companies to import.')
        entries = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Each company must have a company name and URL.')
            entry = validate_entry(row.get('company'), row.get('url'))
            entries.append({**entry, 'kind': sources.detect(entry['url'])})
        return self.store.import_sources(entries, self._locations(locations))

    # refresh
    def start(self, profile: dict, recheck_only=False, source_ids=None) -> bool:
        with self._guard:
            if self.progress['running']:
                return False
            self.progress = {'running': True, 'message': 'Starting…', 'finished_at': None, 'error': None}
        threading.Thread(target=self._background, args=(profile, recheck_only, source_ids), daemon=True).start()
        return True

    def _background(self, profile, recheck_only, source_ids):
        try:
            summary = self.refresh(profile, self._say, recheck_only=recheck_only, source_ids=source_ids)
            self.progress.update(running=False, message=summary, finished_at=int(time.time()))
        except Exception as e:  # reported in the UI, never raised into the server
            self.progress.update(running=False, message='', error=str(e) or 'The refresh failed.',
                                 finished_at=int(time.time()))

    def _say(self, message):
        self.progress['message'] = message

    def refresh(self, profile: dict, say=print, *, recheck_only=False, source_ids=None) -> str:
        """Refresh every source, or only `source_ids` when given. Experience and
        relevance checks then cover only the refreshed sources' jobs, so a
        partial refresh never spends agent usage on other companies."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError('Another job refresh is already running.') from None
            agents = Agents(usage_store=self.usage)
            found = new = 0
            failures = []
            chosen = [s for s in self.store.sources() if source_ids is None or s['id'] in source_ids]
            if not recheck_only:
                for src in chosen:
                    say(f'Reading {src["name"]}…')
                    attempted = set()
                    try:
                        if src['kind'] == 'page':
                            items = sources.page_jobs(src['url'], agents.ask)
                        else:
                            items = sources.READERS[src['kind']](src['url'])
                        items = newest_first([dict(j) for j in items if freshness(j.get('posted')) != 'expired'
                                              and title_matches(j['title'], src['include'], src['exclude'])
                                              and location_matches(j.get('location') or '', src['locations'])])[:sources.MAX_JOBS]
                        detail = sources.DETAILS.get(src['kind'])
                        attempted = set()
                        # Resolve new detail-only dates before admitting jobs to the saved collection.
                        known = self.store.known_ids(src['id'])
                        if detail:
                            for item in items:
                                if len(attempted) >= MAX_DETAILS:
                                    break
                                if item['external_id'] not in known and item.get('needs_detail') and (not item['requirements'] or freshness(item.get('posted')) == 'unknown'):
                                    attempted.add(item['external_id'])
                                    say(f'Reading {src["name"]} new job details ({len(attempted)}/{MAX_DETAILS})…')
                                    try:
                                        item.update(detail(item))
                                        if freshness(item.get('posted')) == 'expired':
                                            self.store.dismiss_external(src['id'], item['external_id'])
                                            known.add(item['external_id'])
                                    except FetchError:
                                        pass
                        items = [j for j in items if freshness(j.get('posted')) != 'expired']
                        details = {j['external_id']: j for j in items if j.get('needs_detail')}
                        fresh = self.store.save_listing(src['id'], items)
                        for ext in attempted:
                            self.store.note_detail_attempt(src['id'], ext)
                        if detail and src['kind'] != 'page':
                            candidates = self.store.detail_candidates(src['id'])
                            pending = [r for r in candidates
                                       if r['external_id'] in details and r['external_id'] not in attempted][:MAX_DETAILS-len(attempted)]
                            for n, row in enumerate(pending, 1):
                                say(f'Reading {src["name"]} saved job details ({n}/{len(pending)})…')
                                try:
                                    self.store.note_detail_attempt(src['id'], row['external_id'])
                                    self.store.set_detail(row['id'], detail(details[row['external_id']]))
                                except FetchError:
                                    pass
                        self.store.source_result(src['id'], count=len(items))
                        found += len(items)
                        new += len(fresh)
                    except (FetchError, AgentError, KeyError, ValueError, TypeError, IndexError) as e:
                        failures.append(src['name'])
                        self.store.source_result(src['id'], error=str(e)[:500] or 'Could not read this source.')
                    if src['kind'] == 'page':
                        # Search failure does not prevent checking already-saved direct links.
                        pending = [r for r in self.store.detail_candidates(src['id'])
                                   if r['external_id'] not in attempted and r['url']][:MAX_DETAILS-len(attempted)]
                        for row in pending:
                            self.store.note_detail_attempt(src['id'], row['external_id'])
                            say(f'Checking saved {src["name"]} posting dates…')
                            try:
                                self.store.set_detail(row['id'], sources.DETAILS['page']({'needs_detail': row['url']}))
                            except FetchError:
                                pass

            self.store.prune_old()
            years = self.experience_years(profile)['years']
            todo = self.store.unchecked(MAX_CHECKS, None if source_ids is None else {s['id'] for s in chosen})
            if todo:
                say(f'Checking relevance and experience for {len(todo)} job(s)…')
                self.store.set_eligibility(eligibility.check(todo, years, agents))
            self.store.set_meta('last_refresh', int(time.time()))
            scope = '' if source_ids is None else ', '.join(s['name'] for s in chosen) + ': '
            parts = [] if recheck_only else [f'{scope}{found} matching job(s), {new} new']
            if todo:
                parts.append(f'{len(todo)} checked by ' + (', '.join(NAMES[a] for a in agents.used) or 'text rules'))
            if failures:
                parts.append('could not read: ' + ', '.join(failures))
            text = '. '.join(parts)
            return text[:1].upper() + text[1:] + '.' if parts else 'Nothing to check.'
        finally:
            os.close(fd)

    def state(self, profile: dict) -> dict:
        self.store.prune_old()
        return {'sources': [{**s, 'label': sources.LABELS[s['kind']]} for s in self.store.sources()],
                'jobs': self.store.jobs(), 'progress': dict(self.progress),
                'experience': self.experience_years(profile),
                'last_refresh': self.store.get_meta('last_refresh'),
                'agents': [NAMES[a] for a in Agents().order]}
