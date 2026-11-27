"""Metadata-only timings for the active tailoring operation."""
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import secrets
import threading
import time

_CURRENT = ContextVar('tailoring_timing', default=None)


class TailoringTiming:
    def __init__(self):
        self.lock = threading.RLock()
        self.started = None
        self.data = {'running': False, 'stages': [], 'elapsed_seconds': 0}

    @contextmanager
    def run(self):
        with self.lock:
            self.started = time.perf_counter()
            self.data = {'id': secrets.token_hex(12), 'running': True, 'stage': 'Starting', 'stages': []}
        token = _CURRENT.set(self)
        try:
            yield
        finally:
            _CURRENT.reset(token)
            with self.lock:
                self.data.update(running=False, elapsed_seconds=time.perf_counter() - self.started)

    def snapshot(self):
        with self.lock:
            out = copy.deepcopy(self.data)
            if self.data['running']:
                out['elapsed_seconds'] = time.perf_counter() - self.started
            return out


@contextmanager
def stage(ident, label):
    timing = _CURRENT.get()
    if timing is None:
        yield
        return
    start = time.perf_counter()
    with timing.lock:
        timing.data['stage'] = label
    success = False
    try:
        yield
        success = True
    finally:
        with timing.lock:
            timing.data['stages'].append({'id': ident, 'label': label, 'seconds': time.perf_counter() - start,
                                          'status': 'completed' if success else 'fallback'})
