"""Regression scenarios for experience checks and redundant collection work."""
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from resume.jobs import eligibility, fetch, sources
from resume.jobs.store import JobStore
from resume.jobs.service import JobService


class ExperienceRegressionTests(unittest.TestCase):
    def test_fractional_written_and_month_requirements(self):
        yes = dict(eligible='yes', min_years=None, reason='Likely match')
        for text, years, expected in [
            ('Minimum 2.5 years of professional experience.', 2, 'no'),
            ('Minimum three years of professional experience.', 2, 'no'),
            ('Minimum 18 months of software experience.', 1, 'no'),
            ('Minimum six months of software experience.', 1, 'yes'),
            ('Minimum 1.5 to 3 years of experience.', 1.5, 'yes'),
            ('At least 6 months - 1 year of experience.', .75, 'yes'),
            ('At least 6 months to 1 year of experience.', .75, 'yes'),
            ('Minimum 1 year and 6 months experience.', 1.2, 'no'),
            ('MINIMUM 1 YEAR AND 6 MONTHS EXPERIENCE.', 1.2, 'no'),
            ('Preferred: five years of experience.', 1, 'yes'),
            ('3 years Java OR 1 year Python experience.', 1, 'yes'),
            ('Minimum 5 years of experience using Python or Java and 2 years of team leadership experience.', 1.5, 'no'),
        ]:
            with self.subTest(text=text):
                self.assertEqual(eligibility.guard(yes, 'Engineer', text, years)['eligible'], expected)
        self.assertIn('OR', eligibility.experience_lines('3 years Java OR 1 year Python experience.'))
        self.assertEqual(eligibility.rule_check('3 years Java OR 1 year Python experience.', 1)['eligible'], 'unclear')

    def test_iso_profile_dates_and_future_experience(self):
        self.assertEqual(eligibility.profile_years({'experience': [
            dict(start='2024-07', end='2025-06')]}), 1)
        self.assertEqual(eligibility.profile_years({'experience': [
            dict(start='2099-01', end='2099-12')]}), 0)
        self.assertIsNone(eligibility._month('2024-99'))


class RuntimeSpeedTests(unittest.TestCase):
    def test_unknown_listing_location_refreshes_old_nonmatching_detail(self):
        with tempfile.TemporaryDirectory() as root:
            service = JobService(root)
            src = service.store.add_source('Acme', 'https://careers.example', 'kula', locations='India')
            item = dict(external_id='one', title='Engineer', location='', needs_detail='https://careers.example/one',
                        url='https://careers.example/one', posted=date.today().isoformat(),
                        requirements='Build APIs', description='Develop software')
            ident = service.store.save_listing(src['id'], [dict(item, location='London, UK')])[0]
            detail = {'location': 'Bengaluru, India'}
            with patch.dict(sources.READERS, {'kula': lambda _: [item]}), \
                    patch.dict(sources.DETAILS, {'kula': lambda _: detail}), \
                    patch('resume.jobs.service.Agents', return_value=SimpleNamespace(used=[])), \
                    patch('resume.jobs.eligibility.check', return_value={}):
                service.refresh({'experience': []}, lambda _: None)
            self.assertEqual(service.store.job(ident)['location'], 'Bengaluru, India')
            self.assertEqual(service.store.jobs()[0]['id'], ident)

    def test_large_detail_batch_leaves_workers_for_another_company(self):
        entered, release = threading.Event(), threading.Event()
        active = 0
        lock = threading.Lock()
        def slow(item):
            nonlocal active
            with lock:
                active += 1
                if active >= 2:
                    entered.set()
            release.wait(3)
            return {'description': 'done'}
        with ThreadPoolExecutor(max_workers=4) as details, ThreadPoolExecutor(max_workers=2) as sources:
            with patch('resume.jobs.service.DETAIL_WINDOW', 2):
                large = sources.submit(lambda: list(JobService._details(
                    list(range(20)), slow, details, None)))
                try:
                    self.assertTrue(entered.wait(2))
                    small = sources.submit(lambda: list(JobService._details(
                        ['small'], lambda item: {'description': item}, details, None)))
                    self.assertEqual(small.result(timeout=1), [('small', {'description': 'small'})])
                finally:
                    release.set()
                self.assertEqual(len(large.result(timeout=2)), 20)

    def test_detail_deadline_defers_remaining_items_without_dropping_results(self):
        calls, clock = [], [10.0]
        def read(item):
            calls.append(item)
            clock[0] = 12.0
            return {'description': str(item)}
        with ThreadPoolExecutor(max_workers=2) as pool, patch('resume.jobs.service.DETAIL_WINDOW', 1), \
                patch('resume.jobs.service.time.monotonic', side_effect=lambda: clock[0]):
            result = list(JobService._details(list(range(10)), read, pool, 11.0))
        self.assertEqual(calls, [0])
        self.assertEqual(result, [(0, {'description': '0'})])

    def test_single_company_can_use_a_larger_detail_window(self):
        barrier = threading.Barrier(4)
        def read(item):
            barrier.wait(timeout=2)
            return {'description': str(item)}
        with ThreadPoolExecutor(max_workers=4) as pool:
            result = list(JobService._details(list(range(4)), read, pool, None, window=4))
        self.assertEqual(len(result), 4)

    def test_deferred_details_save_location_and_listing_cannot_erase_it(self):
        with tempfile.TemporaryDirectory() as root:
            store = JobStore(root)
            src = store.add_source('Acme', 'https://careers.example', 'kula', locations='India')
            item = dict(external_id='one', title='Engineer', location='',
                        url='https://careers.example/one', posted='', requirements='', description='')
            ident = store.save_listing(src['id'], [item])[0]
            store.set_detail(ident, {'title': 'Backend Engineer', 'location': 'Bengaluru, India',
                                    'requirements': 'Build APIs', 'description': 'Develop software'})
            self.assertEqual(store.jobs()[0]['location'], 'Bengaluru, India')
            self.assertEqual(store.job(ident)['title'], 'Backend Engineer')
            store.save_listing(src['id'], [item])
            self.assertEqual(store.jobs()[0]['location'], 'Bengaluru, India')
            store.set_detail(ident, {'location': 'London, UK'})
            self.assertEqual(store.jobs(), [])

    def test_review_writer_can_commit_while_page_read_is_open(self):
        with tempfile.TemporaryDirectory() as root:
            store = JobStore(root)
            store.sources()
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                with store.db() as reader:
                    reader.execute('BEGIN')
                    reader.execute('SELECT * FROM sources').fetchall()
                    future = pool.submit(store.set_meta, 'concurrent-review', 'saved')
                    future.result(timeout=2)
            finally:
                pool.shutdown(wait=True)
            self.assertEqual(store.get_meta('concurrent-review'), 'saved')

    def test_normal_reads_do_not_repeat_migrations_or_take_write_lock(self):
        with tempfile.TemporaryDirectory() as root:
            store = JobStore(root)
            store.sources()
            with patch.object(store, '_initialize', side_effect=AssertionError('migration repeated')):
                for _ in range(3):
                    with store.db() as conn:
                        statements = []
                        conn.set_trace_callback(statements.append)
                        conn.execute('SELECT * FROM sources').fetchall()
                        self.assertFalse(conn.in_transaction)
                        self.assertFalse(any('BEGIN' in s for s in statements))

    def test_schema_change_is_detected_after_initialization(self):
        with tempfile.TemporaryDirectory() as root:
            store = JobStore(root)
            store.sources()
            with store.db() as conn:
                conn.execute('CREATE TABLE another_schema_version (id INTEGER)')
            with patch.object(store, '_initialize', wraps=store._initialize) as initialize:
                store.sources()
                store.sources()
                self.assertEqual(initialize.call_count, 1)

    def test_guard_upgrade_requeues_old_veto_but_preserves_manual_choice(self):
        with tempfile.TemporaryDirectory() as root:
            store = JobStore(root)
            src = store.add_source('Acme', 'https://careers.example', 'page')
            ids = store.save_listing(src['id'], [dict(external_id=str(i), title='Engineer',
                location='India', url=f'https://careers.example/{i}', posted='',
                requirements='Preferred: 5 years experience', description='Build APIs') for i in range(2)])
            store.set_eligibility({ident: dict(min_years=5, eligible='unclear', relevant='yes',
                by='codex', reason='Mentions 5 years; check whether it is required: preferred') for ident in ids})
            store.set_manual_eligibility(ids[1], 'no')
            store.guard_saved(lambda verdict, title, text: verdict)
            self.assertIsNone(store.job(ids[0])['eligible'])
            self.assertEqual(store.job(ids[1])['manual_eligible'], 'no')
            self.assertEqual(store.job(ids[1])['eligible'], 'unclear')

    def test_successful_curl_fallback_skips_repeated_failed_transport(self):
        request = urllib.request.Request('https://careers.example/jobs')
        with patch.object(fetch, '_CURL_HOSTS', {}), patch.object(fetch, 'check_url'), \
                patch.object(fetch._OPENER, 'open', side_effect=urllib.error.URLError('TLS refused')) as normal, \
                patch.object(fetch, '_curl', return_value=b'jobs') as curl:
            self.assertEqual(fetch._open(request), b'jobs')
            self.assertEqual(fetch._open(request), b'jobs')
            self.assertEqual(normal.call_count, 1)
            self.assertEqual(curl.call_count, 2)

    def test_failed_preferred_curl_can_recover_through_normal_transport(self):
        request = urllib.request.Request('https://careers.example/jobs')
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = request.full_url
        response.read.return_value = b'recovered'
        with patch.object(fetch, '_CURL_HOSTS', {'careers.example': float('inf')}), \
                patch.object(fetch, 'check_url'), patch.object(fetch._OPENER, 'open', return_value=response), \
                patch.object(fetch, '_curl', side_effect=fetch.FetchError('failed')) as curl:
            self.assertEqual(fetch._open(request), b'recovered')
            self.assertEqual(curl.call_count, 1)
