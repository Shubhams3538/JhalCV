import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from resume.jobs import sources, agents
from resume.jobs.fetch import FetchError, fetch_budget, remaining
from resume.jobs.service import JobService


def job(ident, posted=None):
    return dict(external_id=ident, title='Software Engineer', location='India', url='https://x.test/'+ident,
                posted=posted or date.today().isoformat(), requirements='1 year of experience', description='Build software')


class PerformanceTests(unittest.TestCase):
    def test_company_fetches_overlap_and_are_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            service = JobService(root)
            for i in range(16):
                service.add_source(str(i), 'https://job-boards.greenhouse.io/c'+str(i))
            active = peak = 0
            lock = threading.Lock()
            def read(url):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(.025)
                with lock:
                    active -= 1
                return [job(url.rsplit('/',1)[-1])]
            with patch.dict(sources.READERS, {'greenhouse':read}), patch('resume.jobs.agents.shutil.which',return_value=None):
                result=service.refresh({'experience':[]},lambda _:None)
            self.assertIn('16 matching job(s)', result)
            self.assertGreater(peak,1)
            self.assertLessEqual(peak,8)
            self.assertEqual(service.progress['completed'],16)
            self.assertEqual(service.progress['active'],[])
            self.assertEqual(len(service.store.jobs()),16)

    def test_detail_reads_overlap_and_expired_budget_never_starts_them(self):
        active = peak = 0
        lock=threading.Lock()
        def read(item):
            nonlocal active,peak
            with lock:
                active+=1
                peak=max(peak,active)
            time.sleep(.02)
            with lock:active-=1
            return {'posted':date.today().isoformat()}
        items=[job(str(i)) for i in range(12)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(len(list(JobService._details(items,read,pool,time.monotonic()+3))),12)
            self.assertEqual(list(JobService._details(items,read,pool,time.monotonic()-1)),[])
        self.assertGreater(peak,1)
        self.assertLessEqual(peak,4)

    def test_today_jobs_win_over_older_fast_source(self):
        with tempfile.TemporaryDirectory() as root:
            service=JobService(root)
            service.add_source('A Fast','https://job-boards.greenhouse.io/fast')
            service.add_source('B Slow','https://job-boards.greenhouse.io/slow')
            def read(url):
                if url.endswith('slow'):
                    time.sleep(.05)
                    return [job('new')]
                return [job('old',(date.today()-timedelta(days=7)).isoformat())]
            with patch.dict(sources.READERS,{'greenhouse':read}), patch('resume.jobs.agents.shutil.which',return_value=None), patch('resume.jobs.service.MAX_CHECKS',1):
                service.refresh({'experience':[]},lambda _:None)
            saved={j['url']:j for j in service.store.jobs()}
            self.assertIsNotNone(saved['https://x.test/new']['eligible'])
            self.assertIsNone(saved['https://x.test/old']['eligible'])

    def test_unchanged_agent_answers_persist_and_changed_prompts_miss(self):
        with tempfile.TemporaryDirectory() as root:
            calls=[]
            def run(prompt,schema,folder):
                calls.append(prompt)
                return {'answer':'ok'}
            schema={'type':'object','properties':{'answer':{'type':'string'}}}
            with patch('resume.jobs.agents.shutil.which',return_value='/bin/fake'),patch.dict(agents.RUNNERS,{'codex':run}):
                agents.Agents(order=['codex'],cache_dir=root).ask('same',schema)
                second=agents.Agents(order=['codex'],cache_dir=root)
                second.ask('same',schema)
                second.ask('changed',schema)
            self.assertEqual(calls,['same','changed'])

    def test_slow_page_does_not_disable_provider_for_every_company(self):
        calls=[]
        def run(prompt,schema,folder):
            calls.append(prompt)
            if prompt=='slow':raise agents.AgentError('timed out')
            return {'answer':'ok'}
        with patch('resume.jobs.agents.shutil.which',return_value='/bin/fake'),patch('resume.jobs.agents.RETRY_BACKOFF',0),patch.dict(agents.RUNNERS,{'codex':run}):
            ai=agents.Agents(order=['codex'])
            with self.assertRaises(agents.AgentError):ai.ask('slow',{'type':'object'})
            self.assertEqual(ai.ask('good',{'type':'object','properties':{'answer':{'type':'string'}}}),{'answer':'ok'})
            self.assertEqual(ai.failed,{})

    def test_upgrade_retains_jobs_statuses_and_deleted_url(self):
        with tempfile.TemporaryDirectory() as root:
            service=JobService(root)
            src=service.add_source('Acme','https://acme.test/careers')
            store=service.store
            store.save_listing(src['id'],[job('old')])
            ident=store.jobs()[0]['id']
            store.set_status(ident,'applied')
            store.upgrade_source(src['id'],'https://job-boards.greenhouse.io/acme','greenhouse')
            feed=dict(job('new'),url='https://x.test/old')
            store.save_listing(src['id'],[feed])
            self.assertEqual([(j['id'],j['status']) for j in store.jobs()],[(ident,'applied')])
            store.dismiss_external(src['id'],'https://x.test/deleted')
            store.save_listing(src['id'],[dict(job('id-from-feed'),url='https://x.test/deleted')])
            self.assertEqual(len(store.jobs()),1)

    def test_discovery_preserves_role_filters_and_never_guesses_in_refresh(self):
        with tempfile.TemporaryDirectory() as root:
            service=JobService(root)
            src=service.add_source('Acme','https://acme.test/careers?q=engineer',locations='India')
            with patch('resume.jobs.service.resolve') as resolve:
                self.assertEqual(service._upgrade_page_source(src),src)
                resolve.assert_not_called()
            other=service.add_source('Other','https://other.test/careers',locations='India')
            with patch('resume.jobs.service.resolve',return_value=None) as resolve:
                service._upgrade_page_source(other)
                self.assertEqual(resolve.call_args.kwargs,dict(render=False,guess=False,country='India'))

    def test_source_budget_prevents_starting_expired_work(self):
        with fetch_budget(time.monotonic()-1):
            with self.assertRaises(FetchError):remaining()

    def test_workday_locale_upgrade_preserves_applied_and_deletions(self):
        with tempfile.TemporaryDirectory() as root:
            service=JobService(root)
            src=service.add_source('Acme','https://acme.test/careers')
            store=service.store
            original='https://acme.wd1.myworkdayjobs.com/en-US/External/job/Pune/Engineer_R1'
            canonical=original.replace('/en-US/','/')
            store.save_listing(src['id'],[dict(job(original),url=original)])
            ident=store.jobs()[0]['id']
            store.set_status(ident,'applied')
            store.upgrade_source(src['id'],'https://acme.wd1.myworkdayjobs.com/External','workday')
            store.save_listing(src['id'],[dict(job('R1'),url=canonical)])
            self.assertEqual([(j['id'],j['status']) for j in store.jobs()],[(ident,'applied')])
            store.dismiss_external(src['id'],original.replace('R1','R2'))
            store.save_listing(src['id'],[dict(job('R2'),url=canonical.replace('R1','R2'))])
            self.assertEqual(len(store.jobs()),1)

    def test_city_query_filter_prevents_automatic_whole_board_upgrade(self):
        with tempfile.TemporaryDirectory() as root:
            service=JobService(root)
            src=service.add_source('Acme','https://acme.test/jobs?location=Bengaluru',locations='India')
            with patch('resume.jobs.service.resolve') as resolve:
                self.assertEqual(service._upgrade_page_source(src),src)
                resolve.assert_not_called()

    def test_manual_link_edit_keeps_jobs_and_validates_public_urls(self):
        with tempfile.TemporaryDirectory() as root:
            service=JobService(root)
            src=service.add_source('Acme','https://acme.test/careers')
            service.store.save_listing(src['id'],[job('saved')])
            service.edit_source_url(src['id'],'https://job-boards.greenhouse.io/acme')
            self.assertEqual(len(service.store.jobs()),1)
            self.assertEqual(service.store.sources()[0]['kind'],'greenhouse')
            with self.assertRaises(FetchError):service.edit_source_url(src['id'],'http://127.0.0.1:8765/')

    def test_deletions_survive_reverse_feed_to_page_transition(self):
        for action in ('delete', 'category', 'prune'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as root:
                service=JobService(root)
                src=service.add_source('Acme','https://acme.wd1.myworkdayjobs.com/External')
                url='https://acme.wd1.myworkdayjobs.com/en-US/External/job/Pune/Engineer_R1'
                old=(date.today()-timedelta(days=10)).isoformat() if action=='prune' else None
                service.store.save_listing(src['id'],[dict(job('R1',old),url=url)])
                ident=service.store.jobs()[0]['id']
                if action=='delete': service.store.delete_job(ident)
                elif action=='category': service.store.delete_category('review')
                else: service.store.prune_old()
                service.edit_source_url(src['id'],'https://acme.test/careers')
                canonical=url.replace('/en-US/','/')
                service.store.save_listing(src['id'],[dict(job(canonical),url=canonical)])
                self.assertEqual(service.store.jobs(),[])
