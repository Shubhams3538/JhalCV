"""Deterministic overlap proofs: no real portals or AI providers are called."""
import tempfile
import threading
import time
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from resume.jobs import eligibility, sources
from resume.jobs.review_queue import ReviewQueue
from resume.jobs.service import JobService


def posting(name):
    return {'external_id': name, 'title': 'Software Engineer ' + name, 'location': 'India',
            'url': 'https://careers.example/' + name, 'posted': date.today().isoformat(),
            'requirements': 'Build software', 'description': 'Develop APIs'}


def verdict(rows, **changes):
    return {row['id']: {'min_years': 0, 'eligible': 'yes', 'relevant': 'yes',
                        'reason': 'No minimum.', 'relevance_reason': 'Software role.',
                        'by': 'codex', **changes} for row in rows}


class ReviewPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = JobService(self.temp.name)
        self.agent = SimpleNamespace(used=['codex'])

    def source(self, name):
        return self.service.store.add_source(name, 'https://job-boards.greenhouse.io/' + name, 'greenhouse')

    def test_other_company_is_reviewed_and_published_while_review_and_fetch_are_blocked(self):
        for name in ('A', 'B', 'C'):
            self.source(name)
        a_started, release_a, a_done = threading.Event(), threading.Event(), threading.Event()
        b_checked, b_published = threading.Event(), threading.Event()
        observed = []

        def read(url):
            name = url.rsplit('/', 1)[-1]
            if name == 'B':
                self.assertTrue(a_started.wait(3), 'First review never started')
            if name == 'C':
                try:
                    observed.append(('published_before_fetch_finished', b_published.wait(3)))
                finally:
                    release_a.set()
            return [posting(name)]

        def check(rows, years, agents):
            names = [row['title'].rsplit(' ', 1)[-1] for row in rows]
            if 'A' in names:
                a_started.set()
                self.assertTrue(release_a.wait(4), 'Review queue blocked behind company A')
                a_done.set()
            if 'B' in names:
                b_checked.set()
            return verdict(rows)

        def progress(_):
            if b_checked.is_set() and not b_published.is_set():
                b = next((row for row in self.service.store.jobs() if row['title'].endswith(' B')), None)
                if b and b['eligible'] == 'yes':
                    observed.append(('published_before_first_review_finished', not a_done.is_set()))
                    b_published.set()

        with patch('resume.jobs.service.Agents', return_value=self.agent), \
                patch.dict(sources.READERS, {'greenhouse': read}), \
                patch('resume.jobs.eligibility.check', side_effect=check):
            summary = self.service.refresh({'experience': []}, progress)
        self.assertIn(('published_before_fetch_finished', True), observed)
        self.assertIn(('published_before_first_review_finished', True), observed)
        self.assertIn('3 checked', summary)
        self.assertEqual(self.service.progress['review_completed'], 3)
        self.assertEqual(self.service.progress['review_queued'], 0)
        self.assertEqual(self.service.progress['review_active'], 0)

    def test_three_review_workers_share_ready_jobs_and_keep_batches_bounded(self):
        lock, release = threading.Lock(), threading.Event()
        active = peak = 0
        seen, batches, persisted = [], [], []

        def check(rows, years, agents):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                seen.extend(row['id'] for row in rows)
                batches.append(len(rows))
                if active == 3:
                    release.set()
            try:
                self.assertTrue(release.wait(3), 'Reviews were serialized')
                return verdict(rows)
            finally:
                with lock:
                    active -= 1

        store = Mock()
        store.set_eligibility.side_effect = lambda results: persisted.extend(results)
        with patch('resume.jobs.eligibility.check', side_effect=check):
            with ReviewQueue(store, 1, self.agent, lambda _: None) as reviews:
                reviews.add([{'id': i} for i in range(64)])
        self.assertEqual(peak, 3)
        self.assertLessEqual(max(batches), eligibility.BATCH)
        self.assertEqual(sorted(seen), list(range(64)))
        self.assertEqual(sorted(persisted), list(range(64)))

    def test_fallback_is_not_queued_again_during_same_refresh(self):
        self.source('A')
        with patch('resume.jobs.service.Agents', return_value=self.agent), \
                patch.dict(sources.READERS, {'greenhouse': lambda _: [posting('A')]}), \
                patch('resume.jobs.eligibility.check', side_effect=lambda rows, *_: verdict(rows, eligible='unclear', relevant=None)) as check:
            self.service.refresh({'experience': []}, lambda _: None)
        self.assertEqual(check.call_count, 1)
        self.assertEqual(len(self.service.store.unchecked(None)), 1)

    def test_large_company_does_not_admit_its_entire_backlog_before_ready_small_company(self):
        self.source('A')
        self.source('B')
        a_started = threading.Event()
        lock = threading.Lock()
        a_seen, at_b = [], []

        def read(url):
            if url.endswith('/A'):
                return [posting('A-' + str(i)) for i in range(240)]
            self.assertTrue(a_started.wait(3))
            return [posting('B')]

        def check(rows, *_):
            with lock:
                for row in rows:
                    if row['title'].endswith(' B'):
                        at_b.append(len(a_seen))
                    else:
                        a_seen.append(row['id'])
                        a_started.set()
            time.sleep(.002)
            return verdict(rows)

        with patch('resume.jobs.service.Agents', return_value=self.agent), \
                patch.dict(sources.READERS, {'greenhouse': read}), \
                patch('resume.jobs.eligibility.check', side_effect=check):
            self.service.refresh({'experience': []}, lambda _: None)
        self.assertEqual(len(a_seen), 240)
        self.assertEqual(len(set(a_seen)), 240)
        self.assertEqual(len(at_b), 1)
        self.assertLess(at_b[0], 160, 'Small company waited behind the large company backlog')

    def test_source_scoped_query_applies_exclusions_before_batch_limit(self):
        a, b = self.source('A'), self.source('B')
        self.service.store.save_listing(a['id'], [posting('A-' + str(i)) for i in range(10)])
        self.service.store.save_listing(b['id'], [posting('B')])
        first = self.service.store.unchecked(8, {a['id']})
        second = self.service.store.unchecked(8, iter([a['id']]), exclude_ids={row['id'] for row in first})
        self.assertEqual(len(second), 2)
        self.assertTrue(all(row['source_id'] == a['id'] for row in second))
        self.assertEqual(self.service.store.unchecked(8, set()), [])

    def test_partial_refresh_does_not_review_other_companies(self):
        selected, other = self.source('A'), self.source('B')
        self.service.store.save_listing(other['id'], [posting('B')])
        reviewed = []
        def check(rows, *_):
            reviewed.extend(row['source_id'] for row in rows)
            return verdict(rows)
        with patch('resume.jobs.service.Agents', return_value=self.agent), \
                patch.dict(sources.READERS, {'greenhouse': lambda _: [posting('A')]}), \
                patch('resume.jobs.eligibility.check', side_effect=check):
            self.service.refresh({'experience': []}, lambda _: None, source_ids=[selected['id']])
        self.assertEqual(reviewed, [selected['id']])
        self.assertEqual([row['source_id'] for row in self.service.store.unchecked(None)], [other['id']])

    def test_write_failure_drains_queue_and_surfaces_error(self):
        store = Mock()
        store.set_eligibility.side_effect = OSError('database write failed')
        with patch('resume.jobs.eligibility.check', side_effect=lambda rows, *_: verdict(rows)):
            with self.assertRaisesRegex(OSError, 'database write failed'):
                with ReviewQueue(store, 1, self.agent, lambda _: None) as reviews:
                    reviews.add([{'id': i} for i in range(200)])

    def test_queue_honors_budget_and_deduplicates_pending_jobs(self):
        seen = []
        def check(rows, *_):
            seen.extend(row['id'] for row in rows)
            return verdict(rows)
        with patch('resume.jobs.eligibility.check', side_effect=check):
            with ReviewQueue(Mock(), 1, self.agent, lambda _: None, limit=5) as reviews:
                reviews.add([{'id': i} for i in range(3)])
                reviews.add([{'id': i} for i in range(10)])
        self.assertEqual(sorted(seen), list(range(5)))
        self.assertEqual(reviews.completed, 5)
