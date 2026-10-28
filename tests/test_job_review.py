import tempfile
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from resume.jobs.service import JobService
from resume.jobs import sources


def job(ident):
    return dict(external_id=str(ident), title='Software Engineer', location='India',
                url='https://careers.example/job/'+str(ident), posted=date.today().isoformat(),
                requirements='Build software', description='Develop APIs')


def verdict():
    return dict(min_years=0, eligible='yes', relevant='yes', reason='No minimum.',
                relevance_reason='Software work.', by='codex')


class SavedReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.service=JobService(self.temp.name)
        self.store=self.service.store
        self.src=self.service.add_source('Acme','https://job-boards.greenhouse.io/acme',locations='India')

    def tearDown(self):
        self.temp.cleanup()

    def test_all_unreviewed_are_checked_without_fetching_or_rechecking_completed(self):
        self.store.save_listing(self.src['id'],[job(i) for i in range(145)])
        existing=self.store.jobs()[0]['id']
        self.store.set_eligibility({existing:verdict()})
        self.store.set_meta('last_refresh','12345')
        reviewed=[]
        def check(rows, years, agents):
            reviewed.extend(j['id'] for j in rows)
            return {j['id']:verdict() for j in rows}
        with patch('resume.jobs.service.Agents',return_value=SimpleNamespace(used=['codex'])), \
                patch('resume.jobs.service.eligibility.check',side_effect=check), \
                patch('resume.jobs.sources.page_jobs',side_effect=AssertionError('portal called')), \
                patch.dict(sources.READERS,{'greenhouse':lambda url:self.fail('feed called')}), \
                patch('resume.jobs.service.resolve',side_effect=AssertionError('discovery called')):
            summary=self.service.refresh({'experience':[]},lambda _:None,recheck_only=True)
            self.assertEqual(len(reviewed),144)
            self.assertEqual(len(set(reviewed)),144)
            self.assertNotIn(existing,reviewed)
            self.assertIn('Reviewed 144 saved job(s)',summary)
            self.assertEqual(self.store.unchecked(None),[])
            self.assertEqual(self.service.progress['completed'],144)
            self.assertEqual(self.service.progress['phase'],'review')
            self.assertEqual(self.store.get_meta('last_refresh'),'12345')
            self.assertIsNotNone(self.store.get_meta('last_review'))
            reviewed.clear()
            self.assertEqual(self.service.refresh({'experience':[]},lambda _:None,recheck_only=True),'No unreviewed saved jobs.')
            self.assertEqual(reviewed,[])

    def test_uncertain_decisions_finish_once_and_keep_applied_status(self):
        self.store.save_listing(self.src['id'],[job(1)])
        ident=self.store.jobs()[0]['id']
        self.store.set_status(ident,'applied')
        def check(rows, years, agents):
            return {j['id']:dict(verdict(),eligible='unclear',relevant='unclear') for j in rows}
        with patch('resume.jobs.service.Agents',return_value=SimpleNamespace(used=[])), \
                patch('resume.jobs.service.eligibility.check',side_effect=check) as checking:
            self.service.refresh({'experience':[]},lambda _:None,recheck_only=True)
            self.service.refresh({'experience':[]},lambda _:None,recheck_only=True)
        self.assertEqual(checking.call_count,1)
        self.assertEqual(self.store.jobs()[0]['status'],'applied')
        self.assertEqual(self.store.unchecked(None),[])

    def test_hidden_and_out_of_location_jobs_are_not_reviewed(self):
        self.store.save_listing(self.src['id'],[job(1),dict(job(2),location='Singapore')])
        ident=next(j['id'] for j in self.store.jobs() if j['url'].endswith('/1'))
        self.store.set_status(ident,'hidden')
        with patch('resume.jobs.service.Agents',return_value=SimpleNamespace(used=[])), \
                patch('resume.jobs.service.eligibility.check') as checking:
            self.service.refresh({'experience':[]},lambda _:None,recheck_only=True)
        checking.assert_not_called()
        self.assertEqual(self.service.state({'experience':[]})['unreviewed_count'],0)
