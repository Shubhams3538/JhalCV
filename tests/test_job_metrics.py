import json
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from resume.jobs.metrics import Benchmarks, company_scope, measure, submit, timed
from resume.jobs.service import JobService
from resume.jobs import sources
from resume.jobs import fetch, agents


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.bench=Benchmarks(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_parallel_metrics_propagate_company_and_keep_inclusive_times(self):
        @timed('http')
        def read():time.sleep(.015)
        def company(ident):
            with company_scope(ident,'Company '+str(ident)), measure('source'):
                with ThreadPoolExecutor(max_workers=2) as details:
                    for future in [submit(details,read),submit(details,read)]:future.result()
        with self.bench.run('refresh'):
            with ThreadPoolExecutor(max_workers=2) as pool:
                for future in [submit(pool,company,1),submit(pool,company,2)]:future.result()
        report=self.bench.report()['report']
        stages={s['stage']:s for s in report['stages']}
        self.assertEqual(stages['http']['calls'],4)
        self.assertEqual(len(report['companies']),2)
        for company in report['companies']:
            self.assertEqual(company['stages']['http']['calls'],2)
            self.assertGreater(company['stages']['source']['seconds'],0)
        self.assertEqual(report['status'],'complete')
        restored=Benchmarks(self.root).report()['report']
        self.assertEqual(restored['id'],report['id'])

    def test_failed_calls_and_run_are_recorded_without_saving_exception_text(self):
        with self.assertRaisesRegex(RuntimeError,'PRIVATE_TEXT'):
            with self.bench.run('review'), measure('ai'):
                raise RuntimeError('PRIVATE_TEXT')
        report=self.bench.report()['report']
        self.assertEqual(report['status'],'failed')
        self.assertEqual(report['stages'][0]['failures'],1)
        self.assertNotIn('PRIVATE_TEXT',json.dumps(report))
        self.assertNotIn('PRIVATE_TEXT',next(self.bench.folder.glob('*.json')).read_text())

    def test_live_report_and_newer_external_run(self):
        with self.bench.run('review'):
            self.assertEqual(self.bench.report()['report']['status'],'running')
        first=self.bench.report()['report']['id']
        with Benchmarks(self.root).run('refresh'):pass
        self.assertNotEqual(self.bench.report()['report']['id'],first)
        self.assertEqual(self.bench.report(first)['report']['id'],first)
        with self.assertRaises(ValueError):self.bench.report('../credentials')

    def test_persistence_failure_does_not_fail_paid_work(self):
        with patch('resume.jobs.metrics.os.replace',side_effect=OSError('disk unavailable')):
            with self.bench.run('review'):pass
        self.assertTrue(self.bench.report()['persistence_warning'])
        self.assertEqual(self.bench.report()['report']['status'],'complete')

    def test_http_ai_and_slot_wait_are_instrumented(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def geturl(self):return 'https://company.test/'
            def read(self,limit):return b'ok'
        import threading
        with self.bench.run('refresh'):
            with patch('resume.jobs.fetch._OPENER.open',return_value=Response()):
                self.assertEqual(fetch.get_text('https://company.test/'),'ok')
            semaphore=threading.Semaphore(1)
            fetch.acquire_slot(semaphore,'busy','browser_queue');semaphore.release()
            proc=subprocess.CompletedProcess([],0,'{}','')
            with patch('resume.jobs.agents.subprocess.run',return_value=proc):
                agents._run(['claude'],'PRIVATE_PROMPT',self.root)
        stages={s['stage']:s for s in self.bench.report()['report']['stages']}
        self.assertEqual(stages['http']['calls'],1)
        self.assertEqual(stages['ai']['calls'],1)
        self.assertEqual(stages['browser_queue']['calls'],1)
        self.assertNotIn('PRIVATE_PROMPT',json.dumps(self.bench.report()))

    def test_service_refresh_and_review_have_separate_persisted_reports(self):
        service=JobService(self.root)
        service.add_source('Acme','https://job-boards.greenhouse.io/acme')
        with patch.dict(sources.READERS,{'greenhouse':lambda url:[]}),patch('resume.jobs.agents.shutil.which',return_value=None):
            service.refresh({'experience':[]},lambda _:None)
            service.refresh({'experience':[]},lambda _:None,recheck_only=True)
        report=service.benchmarks.report()
        self.assertEqual(report['report']['mode'],'review')
        self.assertEqual(len(report['history']),2)
        prior=service.benchmarks.report(report['history'][1]['id'])['report']
        self.assertEqual(prior['mode'],'refresh')
        self.assertEqual(prior['companies'][0]['name'],'Acme')
        self.assertIn('database',{s['stage'] for s in prior['stages']})

    def test_failed_slot_wait_is_counted_as_a_failure(self):
        class Busy:
            def acquire(self,timeout):return False
        with self.bench.run('refresh'):
            with self.assertRaises(fetch.FetchError):
                fetch.acquire_slot(Busy(),'busy','ai_queue')
        stat=self.bench.report()['report']['stages'][0]
        self.assertEqual((stat['calls'],stat['failures']),(1,1))
