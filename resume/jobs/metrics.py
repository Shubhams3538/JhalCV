"""Metadata-only live timings. Inclusive parallel spans must not be added together."""
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from datetime import datetime, timezone
from functools import wraps
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid

_CURRENT = ContextVar('job_metrics_run', default=None)
_COMPANY = ContextVar('job_metrics_company', default=None)
LABELS = {
    'source_failure': 'Company collection failures',
    'http': 'HTTP requests', 'browser': 'Browser rendering', 'browser_queue': 'Wait for browser slot',
    'ai': 'AI calls', 'ai_queue': 'Wait for AI slot', 'ai_cache_hit': 'AI cache hits',
    'database': 'Database (including lock waits)', 'listing': 'Listing collection (inclusive)',
    'discovery': 'Portal discovery (inclusive)', 'details': 'Posting details (inclusive)',
    'detail_queue': 'Wait for detail worker', 'source_queue': 'Wait for company worker',
    'source': 'Company collection (inclusive)', 'eligibility': 'Eligibility batches (inclusive)',
}


class Run:
    def __init__(self, mode):
        self.id = uuid.uuid4().hex
        self.mode = mode
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.start = time.perf_counter()
        self.end = None
        self.status = 'running'
        self.lock = threading.Lock()
        self.stages = {}
        self.companies = {}

    def add(self, stage, seconds, failed=False, company=None):
        if stage not in LABELS:
            return
        with self.lock:
            targets = [self.stages]
            if company is not None:
                ident, name = company
                targets.append(self.companies.setdefault(str(ident), {'id': ident, 'name': name, 'stages': {}})['stages'])
            for target in targets:
                item = target.setdefault(stage, {'stage': stage, 'label': LABELS[stage], 'calls': 0,
                                                 'seconds': 0., 'max_seconds': 0., 'failures': 0})
                item['calls'] += 1
                item['seconds'] += max(0., seconds)
                item['max_seconds'] = max(item['max_seconds'], seconds)
                item['failures'] += int(failed)

    def snapshot(self):
        with self.lock:
            return {'id': self.id, 'mode': self.mode, 'started_at': self.started_at,
                    'status': self.status, 'elapsed_seconds': (self.end or time.perf_counter())-self.start,
                    'stages': json.loads(json.dumps(list(self.stages.values()))),
                    'companies': json.loads(json.dumps(list(self.companies.values())))}


@contextmanager
def measure(stage):
    run = _CURRENT.get()
    if run is None:
        yield
        return
    start, failed = time.perf_counter(), False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        run.add(stage, time.perf_counter()-start, failed, _COMPANY.get())


def timed(stage):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            with measure(stage):
                return function(*args, **kwargs)
        return call
    return decorate


@contextmanager
def company_scope(ident, name):
    token = _COMPANY.set((ident, name))
    try:
        yield
    finally:
        _COMPANY.reset(token)


def mark(stage, seconds=0.):
    run = _CURRENT.get()
    if run:
        run.add(stage, seconds, company=_COMPANY.get())


def submit(pool, function, *args):
    # ContextVars do not propagate into executor threads by themselves.
    return pool.submit(copy_context().run, function, *args)


class Benchmarks:
    def __init__(self, root):
        self.folder = Path(root) / '.local' / 'job-benchmarks'
        self.active = None
        self.warning = False
        self.lock = threading.Lock()

    @contextmanager
    def run(self, mode):
        run = Run(mode)
        with self.lock:
            self.active = run
        token = _CURRENT.set(run)
        try:
            yield run
        except BaseException:
            run.status = 'failed'
            raise
        else:
            run.status = 'complete'
        finally:
            run.end = time.perf_counter()
            _CURRENT.reset(token)
            try:
                self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tempfile.NamedTemporaryFile(mode='w', dir=self.folder, delete=False) as stream:
                    tmp = Path(stream.name)
                    json.dump(run.snapshot(), stream)
                os.replace(tmp, self.folder / (run.id + '.json'))
            except OSError:
                self.warning = True  # Never retry or fail paid work because timing persistence failed.
            with self.lock:
                # Keep the completed report available if persistence failed.
                self.active = run

    def report(self, ident=None):
        if ident is not None and not re.fullmatch(r'[a-f0-9]{32}', ident):
            raise ValueError('Invalid benchmark run.')
        active = self.active
        if active and (ident is None or ident == active.id):
            report = active.snapshot()
        else:
            report = None
        paths = sorted(self.folder.glob('*.json'), key=lambda p:p.stat().st_mtime, reverse=True)
        history = []
        for path in paths:
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if ident is None:
                if report is None or (report['status'] != 'running' and data['started_at'] > report['started_at']):
                    report = data
            elif report is None and ident == data['id']:
                report = data
            if len(history) < 10:
                history.append({key:data[key] for key in ('id', 'mode', 'started_at', 'status', 'elapsed_seconds')})
        if ident is not None and report is None:
            raise ValueError('This benchmark run is no longer available.')
        return {'report':report, 'history':history, 'persistence_warning':self.warning}
