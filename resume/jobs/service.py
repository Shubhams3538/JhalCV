"""Refresh every career source, then check experience for jobs not yet checked."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from . import eligibility, sources
from .agents import Agents, AgentError, NAMES
from .fetch import FetchError, check_url
from .store import JobStore

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
    def add_source(self, name, url, include='', exclude='') -> dict:
        name = ' '.join(str(name or '').split())[:100]
        if not name:
            raise ValueError('Enter the company name.')
        url = check_url(url)
        return self.store.add_source(name, url, sources.detect(url),
                                     str(include or '')[:500], str(exclude or '')[:500])

    # refresh
    def start(self, profile: dict, recheck_only=False) -> bool:
        with self._guard:
            if self.progress['running']:
                return False
            self.progress = {'running': True, 'message': 'Starting…', 'finished_at': None, 'error': None}
        threading.Thread(target=self._background, args=(profile, recheck_only), daemon=True).start()
        return True

    def _background(self, profile, recheck_only):
        try:
            summary = self.refresh(profile, self._say, recheck_only=recheck_only)
            self.progress.update(running=False, message=summary, finished_at=int(time.time()))
        except Exception as e:  # reported in the UI, never raised into the server
            self.progress.update(running=False, message='', error=str(e) or 'The refresh failed.',
                                 finished_at=int(time.time()))

    def _say(self, message):
        self.progress['message'] = message

    def refresh(self, profile: dict, say=print, *, recheck_only=False) -> str:
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
            agents = Agents()
            found = new = 0
            failures = []
            if not recheck_only:
                for src in self.store.sources():
                    say(f'Reading {src["name"]}…')
                    try:
                        if src['kind'] == 'page':
                            items = sources.page_jobs(src['url'], agents.ask)
                        else:
                            items = sources.READERS[src['kind']](src['url'])
                        items = [j for j in items if title_matches(j['title'], src['include'], src['exclude'])][:sources.MAX_JOBS]
                        details = {j['external_id']: j for j in items if j.get('needs_detail')}
                        fresh = self.store.save_listing(src['id'], items)
                        detail = sources.DETAILS.get(src['kind'])
                        if detail and fresh:
                            pending = [r for r in (self.store.job(i) for i in fresh)
                                       if not r['requirements'] and r['external_id'] in details][:MAX_DETAILS]
                            for n, row in enumerate(pending, 1):
                                say(f'Reading {src["name"]} job details ({n}/{len(pending)})…')
                                try:
                                    self.store.set_detail(row['id'], detail(details[row['external_id']]))
                                except FetchError:
                                    pass
                        self.store.source_result(src['id'], count=len(items))
                        found += len(items)
                        new += len(fresh)
                    except (FetchError, AgentError, KeyError, ValueError, TypeError, IndexError) as e:
                        failures.append(src['name'])
                        self.store.source_result(src['id'], error=str(e)[:500] or 'Could not read this source.')
            years = self.experience_years(profile)['years']
            todo = self.store.unchecked(MAX_CHECKS)
            if todo:
                say(f'Checking experience requirements for {len(todo)} job(s)…')
                self.store.set_eligibility(eligibility.check(todo, years, agents))
            self.store.set_meta('last_refresh', int(time.time()))
            parts = [] if recheck_only else [f'{found} matching job(s), {new} new']
            if todo:
                parts.append(f'{len(todo)} checked by ' + (', '.join(NAMES[a] for a in agents.used) or 'text rules'))
            if failures:
                parts.append('could not read: ' + ', '.join(failures))
            text = '. '.join(parts)
            return text[:1].upper() + text[1:] + '.' if parts else 'Nothing to check.'
        finally:
            os.close(fd)

    def state(self, profile: dict) -> dict:
        return {'sources': [{**s, 'label': sources.LABELS[s['kind']]} for s in self.store.sources()],
                'jobs': self.store.jobs(), 'progress': dict(self.progress),
                'experience': self.experience_years(profile),
                'last_refresh': self.store.get_meta('last_refresh'),
                'agents': [NAMES[a] for a in Agents().order]}
