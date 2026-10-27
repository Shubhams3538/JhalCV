"""Refresh every career source, then check experience for jobs not yet checked."""
from __future__ import annotations

import os
import threading
import time
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl

from . import eligibility, sources
from .agents import Agents, AgentError, NAMES
from .fetch import FetchError, check_url, current_deadline, fetch_budget, remaining
from .store import JobStore, location_matches
from .freshness import freshness, newest_first
from .imports import MAX_ROWS, preview_csv, validate_entry
from .resolve import resolve

TITLE_SCREEN_MIN = 16
MAX_DETAILS = 40     # detail pages fetched per source per run (new jobs only)
SOURCE_WORKERS = 8
DETAIL_WORKERS = 12
SOURCE_SECONDS = 150

MAX_CHECKS = 120     # experience checks per run
RESOLVE_EVERY = 14 * 86400  # how often a careers page is re-examined for a direct feed


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

    def edit_source_url(self, ident, url):
        url = check_url(url)
        current = next((src for src in self.store.sources() if src['id'] == ident), None)
        kind = sources.detect(url)
        if current and current['kind'] == 'jibe' and urlsplit(current['url']).hostname == urlsplit(url).hostname:
            kind = 'jibe'
        self.store.edit_source_url(ident, url, kind)

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
            self.progress = {'running': True, 'phase': 'review' if recheck_only else 'fetch',
                             'message': 'Starting…', 'finished_at': None, 'error': None}
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
            self.store.prune_old()
            agents = Agents(usage_store=self.usage, cache_dir=self.root / '.cache' / 'jobs-ai')
            if recheck_only:
                return self._review_saved(profile, agents, say, source_ids)
            found = new = 0
            failures = []
            chosen = [s for s in self.store.sources() if source_ids is None or s['id'] in source_ids]
            if not recheck_only:
                active = {}
                completed = checked = 0
                years = self.experience_years(profile)['years']
                self.progress.update(phase='fetch', total=len(chosen), completed=0, active=[], started_at=int(time.time()))
                # Direct feeds finish first; slow rendered pages cannot hold up the entire queue.
                chosen.sort(key=lambda src: src['kind'] == 'page')
                with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as detail_pool, \
                        ThreadPoolExecutor(max_workers=SOURCE_WORKERS) as pool:
                    futures = {}
                    def read_source(src):
                        with self._guard:
                            active[src['id']] = src['name']
                            self.progress['active'] = list(active.values())
                        try:
                            return self._refresh_source(src, agents, detail_pool, say, years)
                        finally:
                            with self._guard:
                                active.pop(src['id'], None)
                                self.progress['active'] = list(active.values())
                                self.progress['completed'] += 1
                    for src in chosen:
                        futures[pool.submit(read_source, src)] = src
                    for future in as_completed(futures):
                        try:
                            count, added, failure = future.result()
                        except Exception as e:  # one company must never abort the others
                            src = futures[future]
                            self.store.source_result(src['id'], error=str(e)[:500] or 'Could not read this source.')
                            count, added, failure = 0, 0, src['name']
                        found += count
                        new += added
                        if failure:
                            failures.append(failure)
                        completed = self.progress['completed']
                        say(f'{completed}/{len(chosen)} companies completed')
                        # Publish verdicts as feeds finish, while other companies continue fetching.
                        if checked < MAX_CHECKS:
                            todo = [j for j in self.store.unchecked(MAX_CHECKS-checked, {futures[future]['id']})
                                    if j.get('posted') == date.today().isoformat()]
                            if todo:
                                say(f'{completed}/{len(chosen)} companies completed; checking {len(todo)} jobs…')
                                self.store.set_eligibility(eligibility.check(todo, years, agents))
                                checked += len(todo)

            self.store.prune_old()
            years = self.experience_years(profile)['years']
            checked = 0 if recheck_only else checked
            todo = self.store.unchecked(MAX_CHECKS-checked, None if source_ids is None else {s['id'] for s in chosen})
            if todo:
                say(f'Checking relevance and experience for {len(todo)} job(s)…')
                self.store.set_eligibility(eligibility.check(todo, years, agents))
            self.store.set_meta('last_refresh', int(time.time()))
            scope = '' if source_ids is None else ', '.join(s['name'] for s in chosen) + ': '
            parts = [] if recheck_only else [f'{scope}{found} matching job(s), {new} new']
            checked += len(todo)
            if checked:
                parts.append(f'{checked} checked by ' + (', '.join(NAMES[a] for a in agents.used) or 'text rules'))
            if failures:
                parts.append('could not read: ' + ', '.join(failures))
            text = '. '.join(parts)
            return text[:1].upper() + text[1:] + '.' if parts else 'Nothing to check.'
        finally:
            os.close(fd)

    def _review_saved(self, profile, agents, say, source_ids=None):
        # Snapshot the queue once: uncertain answers must never create an
        # endless retry loop, and jobs arriving later belong to the next run.
        todo = self.store.unchecked(None, source_ids)
        total = len(todo)
        self.progress.update(phase='review', total=total, completed=0, active=[], started_at=int(time.time()))
        years = self.experience_years(profile)['years']
        chunk = eligibility.BATCH * 3
        for offset in range(0, total, chunk):
            batch = todo[offset:offset+chunk]
            say(f'Reviewing saved jobs: {offset}/{total} completed…')
            self.store.set_eligibility(eligibility.check(batch, years, agents))
            self.progress['completed'] = offset + len(batch)
            say(f'Reviewing saved jobs: {self.progress["completed"]}/{total} completed…')
        self.store.set_meta('last_review', int(time.time()))
        if not total:
            return 'No unreviewed saved jobs.'
        by = ', '.join(NAMES[a] for a in agents.used) or 'text rules'
        return f'Reviewed {total} saved job(s) using {by}. Uncertain matches or dates remain in Needs review.'

    def _upgrade_page_source(self, src):
        """Look for the hiring platform behind a careers page, at most every two weeks."""
        if src['kind'] != 'page' or time.time() - (src.get('resolved_at') or 0) < RESOLVE_EVERY:
            return src
        # A role/team filter encoded in the supplied URL must not be silently
        # lost when switching from the search page to a whole-board feed.
        allowed = {'page', 'sort', 'sort_by', 'sort_by_new', 'descending'}
        if any(key.lower() not in allowed and not key.lower().startswith('utm_')
               for key, value in parse_qsl(urlsplit(src['url']).query)):
            return src
        self.store.note_resolve_attempt(src['id'])
        try:
            found = resolve(src['name'], src['url'], render=False, guess=False,
                            country='India' if 'india' in src.get('locations', '').lower().split(',') else None)
        except (FetchError, ValueError, TypeError, KeyError):
            found = None
        if not found:
            return src
        if found['kind'] == 'workday' and '?' not in found['url'] and src.get('locations'):
            return src
        return self.store.upgrade_source(src['id'], found['url'], found['kind']) or src

    def _refresh_source(self, src, agents, detail_pool, say, years):
        deadline = time.monotonic() + SOURCE_SECONDS
        found = new = 0
        failure = None
        attempted = set()
        with fetch_budget(deadline):
            try:
                src = self._upgrade_page_source(src)
                if src['kind'] == 'page':
                    items = sources.page_jobs(src['url'], agents.ask)
                else:
                    items = sources.READERS[src['kind']](src['url'])
                items = newest_first([dict(j) for j in items if freshness(j.get('posted')) != 'expired'
                                      and title_matches(j['title'], src['include'], src['exclude'])
                                      and location_matches(j.get('location') or '', src['locations'])])[:sources.MAX_JOBS]
                detail = sources.DETAILS.get(src['kind'])
                known = self.store.known_ids(src['id'])
                # Screening is worthwhile only when a large detail-only listing
                # would otherwise trigger many network requests. Full feeds need no extra AI pass.
                screening = [i for i in items if i['external_id'] not in known and i.get('needs_detail')
                             and (not i['requirements'] or freshness(i.get('posted')) == 'unknown')]
                screened = eligibility.screen_titles(screening, years, agents) if detail and len(screening) >= TITLE_SCREEN_MIN else {}
                if detail:
                    candidates = [item for item in items
                                  if item['external_id'] not in known and item.get('needs_detail')
                                  and item['external_id'] not in screened
                                  and (not item['requirements'] or freshness(item.get('posted')) == 'unknown')][:MAX_DETAILS]
                    attempted.update(item['external_id'] for item in candidates)
                    for item, data in self._details(candidates, detail, detail_pool, current_deadline()):
                        item.update(data)
                        if freshness(item.get('posted')) == 'expired':
                            self.store.dismiss_external(src['id'], item['external_id'])
                            known.add(item['external_id'])
                items = [j for j in items if freshness(j.get('posted')) != 'expired']
                details = {j['external_id']: j for j in items if j.get('needs_detail')}
                fresh = self.store.save_listing(src['id'], items)
                if screened:
                    ids = self.store.ids_by_external(src['id'], screened)
                    self.store.set_eligibility({ident: {
                        'min_years': None, 'eligible': 'unclear', 'reason': 'Not checked: title screened out.',
                        'by': agents.last_used or 'rules', 'relevant': 'no', 'relevance_reason': screened[ext]}
                        for ext, ident in ids.items()})
                for ext in attempted:
                    self.store.note_detail_attempt(src['id'], ext)
                if detail and src['kind'] != 'page':
                    candidates = self.store.detail_candidates(src['id'])
                    pending = [r for r in candidates if r['external_id'] in details
                               and r['external_id'] not in attempted][:MAX_DETAILS-len(attempted)]
                    requests = [dict(details[row['external_id']], saved_id=row['id']) for row in pending]
                    for row in pending:
                        self.store.note_detail_attempt(src['id'], row['external_id'])
                    for item, data in self._details(requests, detail, detail_pool, current_deadline()):
                        self.store.set_detail(item['saved_id'], data)
                self.store.source_result(src['id'], count=len(items))
                found, new = len(items), len(fresh)
            except (FetchError, AgentError, KeyError, ValueError, TypeError, IndexError) as e:
                failure = src['name']
                self.store.source_result(src['id'], error=str(e)[:500] or 'Could not read this source.')
            if src['kind'] == 'page':
                pending = [r for r in self.store.detail_candidates(src['id'])
                           if r['external_id'] not in attempted and r['url']][:MAX_DETAILS-len(attempted)]
                requests = [{'needs_detail': row['url'], 'saved_id': row['id']} for row in pending]
                for row in pending:
                    self.store.note_detail_attempt(src['id'], row['external_id'])
                for item, data in self._details(requests, sources.DETAILS['page'], detail_pool, current_deadline()):
                    self.store.set_detail(item['saved_id'], data)
        return found, new, failure

    @staticmethod
    def _details(items, detail, pool, deadline):
        def read(item):
            with fetch_budget(deadline):
                remaining()
                return detail(item)
        futures = {pool.submit(read, item): item for item in items}
        for future in as_completed(futures):
            try:
                yield futures[future], future.result()
            except (FetchError, AgentError, KeyError, ValueError, TypeError, IndexError, OSError):
                pass  # One unreadable posting is skipped; it keeps its unknown date and retries next time.

    def state(self, profile: dict) -> dict:
        self.store.prune_old()
        return {'sources': [{**s, 'label': sources.LABELS.get(s['kind'], 'Career page')} for s in self.store.sources()],
                'jobs': self.store.jobs(), 'progress': dict(self.progress),
                'experience': self.experience_years(profile),
                'last_refresh': self.store.get_meta('last_refresh'),
                'unreviewed_count': len(self.store.unchecked(None)),
                'agents': [NAMES[a] for a in Agents().order]}
