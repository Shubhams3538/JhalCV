"""Bounded job-review queue, independent of the company-fetch executor."""
from __future__ import annotations

import queue
import threading
from concurrent.futures import ThreadPoolExecutor

from . import eligibility
from .agents import AI_SLOTS
from .metrics import submit


class ReviewQueue:
    def __init__(self, store, years, agents, progress, *, limit=None):
        self.store, self.years, self.agents = store, years, agents
        self.progress, self.limit = progress, limit
        self.jobs = queue.Queue(maxsize=eligibility.BATCH * AI_SLOTS * 2)
        self.lock = threading.RLock()
        self.seen = set()
        self.total = self.completed = self.active = self.failed = 0
        self.error = None
        self.pool = None

    def __enter__(self):
        self.pool = ThreadPoolExecutor(max_workers=AI_SLOTS, thread_name_prefix='job-review')
        for _ in range(AI_SLOTS):
            submit(self.pool, self._worker)
        return self

    @property
    def full(self):
        with self.lock:
            return self.limit is not None and self.total >= self.limit

    def report(self):
        # Serialize snapshots so an older worker cannot overwrite newer progress.
        with self.lock:
            try:
                self.progress({'review_total': self.total, 'review_completed': self.completed,
                               'review_active': self.active,
                               'review_queued': self.total - self.completed - self.active - self.failed})
            except Exception as error:
                self.error = self.error or error

    def add(self, jobs):
        """One producer, called after a source's descriptions have been saved.

        A run schedules an ID once even when rules leave it unreviewed in the
        database. Fetch workers keep running if this bounded queue fills up.
        """
        for job in jobs:
            with self.lock:
                if self.error:
                    raise self.error
                if job['id'] in self.seen:
                    continue
                if self.limit is not None and self.total >= self.limit:
                    break
                self.seen.add(job['id'])
                self.total += 1
            while True:
                with self.lock:
                    if self.error:
                        raise self.error
                try:
                    self.jobs.put(job, timeout=.1)
                    break
                except queue.Full:
                    continue
        self.report()

    def _worker(self):
        while True:
            first = self.jobs.get()
            if first is None:
                self.jobs.task_done()
                return
            batch, stop = [first], False
            # Combine already-ready jobs across companies without waiting to
            # fill a batch or for another company's entire review to finish.
            while len(batch) < eligibility.BATCH:
                try:
                    item = self.jobs.get_nowait()
                except queue.Empty:
                    break
                if item is None:
                    self.jobs.task_done()
                    stop = True
                    break
                batch.append(item)
            with self.lock:
                self.active += len(batch)
            self.report()
            succeeded = False
            try:
                with self.lock:
                    error = self.error
                if error is None:
                    # At most BATCH jobs: check() runs this batch inline, so
                    # there is no nested pool or additional AI concurrency.
                    verdicts = eligibility.check(batch, self.years, self.agents)
                    self.store.set_eligibility(verdicts)
                    succeeded = True
            except Exception as error:
                with self.lock:
                    self.error = self.error or error
            finally:
                with self.lock:
                    self.active -= len(batch)
                    if succeeded:
                        self.completed += len(batch)
                    else:
                        self.failed += len(batch)
                for _ in batch:
                    self.jobs.task_done()
                self.report()
            if stop:
                return

    def __exit__(self, exc_type, exc, traceback):
        if exc is not None:
            with self.lock:
                self.error = self.error or exc
        # Workers drain or skip queued work after an error, so a full queue
        # cannot deadlock shutdown. In-flight calls finish before releasing the
        # refresh lock or closing its benchmark report.
        for _ in range(AI_SLOTS):
            self.jobs.put(None)
        self.pool.shutdown(wait=True)
        if exc_type is None and self.error:
            raise self.error
        return False
