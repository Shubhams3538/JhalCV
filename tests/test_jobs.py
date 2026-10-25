import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from resume.jobs import agents, eligibility, sources
from resume.jobs.fetch import FetchError, check_url, page_with_links
from resume.jobs.service import JobService, title_matches
from resume.jobs.store import JobStore


def job(ext, title='Software Engineer', reqs='Bachelor degree. 3+ years of software development experience.'):
    return {'external_id': ext, 'title': title, 'location': 'Bengaluru', 'url': f'https://x.test/{ext}',
            'posted': '2026-10-01', 'requirements': reqs, 'description': reqs}


class DetectAndFetchTests(unittest.TestCase):
    def test_detects_supported_sites(self):
        cases = {
            'https://www.google.com/about/careers/applications/jobs/results/?q=swe': 'google',
            'https://www.amazon.jobs/en/search?base_query=sde': 'amazon',
            'https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site': 'workday',
            'https://job-boards.greenhouse.io/anthropic': 'greenhouse',
            'https://jobs.lever.co/palantir': 'lever',
            'https://jobs.ashbyhq.com/openai': 'ashby',
            'https://jobs.apple.com/en-in/search': 'page',
        }
        for url, kind in cases.items():
            with self.subTest(url=url):
                self.assertEqual(sources.detect(url), kind)

    def test_only_public_urls(self):
        for url in ['ftp://x.test/', 'http://localhost:8765/', 'http://127.0.0.1/', 'http://10.0.0.5/jobs',
                    'https://user:pw@x.test/', 'not a url']:
            with self.subTest(url=url), self.assertRaises(FetchError):
                check_url(url)
        self.assertEqual(check_url(' https://careers.x.test/jobs '), 'https://careers.x.test/jobs')

    def test_page_links_are_absolute(self):
        text, links = page_with_links('<a href="/jobs/1">Engineer</a><script>x</script><a href="#top">Top</a>',
                                      'https://careers.x.test/search')
        self.assertIn('Engineer [link: https://careers.x.test/jobs/1]', text)
        self.assertEqual(links, {'https://careers.x.test/jobs/1'})


class ReaderTests(unittest.TestCase):
    @patch('resume.jobs.sources.get_json')
    def test_amazon_uses_json_endpoint_and_renames_filters(self, get_json):
        get_json.return_value = {'hits': 1, 'jobs': [{'id_icims': '42', 'title': 'SDE', 'normalized_location': 'Bengaluru, IND',
                                                       'job_path': '/en/jobs/42/sde', 'posted_date': 'October 22, 2026',
                                                       'basic_qualifications': '- 1+ years of experience<br/>',
                                                       'description': 'Build things'}]}
        jobs = sources.amazon('https://www.amazon.jobs/en/search?base_query=sde&country%5B%5D=IND&offset=10')
        url = get_json.call_args.args[0]
        self.assertTrue(url.startswith('https://www.amazon.jobs/en/search.json?'))
        self.assertIn('normalized_country_code%5B%5D=IND', url)
        self.assertIn('offset=0', url)
        self.assertEqual(jobs[0]['posted'], '2026-10-22')
        self.assertIn('1+ years of experience', jobs[0]['requirements'])

    @patch('resume.jobs.sources.get_text')
    def test_google_reads_embedded_results(self, get_text):
        data = [[['123', 'Software Engineer, Search', 'x', [None, '<li>Build</li>'], [None, '<li>2 years of experience</li>'],
                  None, None, 'Google', 'en', [['Bengaluru, India']], [None, 'About'], None, [1790000000]]], None, 1, 20]
        get_text.return_value = ("<script>AF_initDataCallback({key: 'ds:1', hash: '2', data:" + json.dumps(data) +
                                 ", sideChannel: {}});</script>")
        jobs = sources.google('https://www.google.com/about/careers/applications/jobs/results/?q=swe&page=3')
        self.assertIn('page=1', get_text.call_args.args[0])
        self.assertEqual(jobs[0]['location'], 'Bengaluru, India')
        self.assertEqual(jobs[0]['requirements'], '2 years of experience')
        self.assertTrue(jobs[0]['url'].endswith('/123-software-engineer-search'))

    @patch('resume.jobs.sources.get_text')
    def test_page_reader_keeps_only_links_that_exist_on_the_page(self, get_text):
        get_text.return_value = '<main>' + 'Open roles at Example. ' * 20 + '<a href="/jobs/7">Backend Engineer</a></main>'
        ask = lambda prompt, schema: {'note': '', 'jobs': [
            {'title': 'Backend Engineer', 'location': 'Remote', 'url': 'https://careers.x.test/jobs/7', 'posted': ''},
            {'title': 'Invented Job', 'location': '', 'url': 'https://careers.x.test/jobs/999', 'posted': ''}]}
        jobs = sources.page_jobs('https://careers.x.test/search', ask)
        self.assertEqual([j['title'] for j in jobs], ['Backend Engineer'])
        self.assertEqual(jobs[0]['needs_detail'], 'https://careers.x.test/jobs/7')

    @patch('resume.jobs.sources.get_text', return_value='<div id="app"></div>')
    def test_javascript_only_pages_explain_the_problem(self, _):
        with self.assertRaisesRegex(FetchError, 'JavaScript'):
            sources.page_jobs('https://careers.x.test/', lambda p, s: {})


class AgentTests(unittest.TestCase):
    def test_falls_back_to_claude_and_strips_api_keys(self):
        calls = []

        def codex(prompt, schema, folder):
            calls.append('codex')
            raise agents.AgentError('usage limit reached')

        def claude(prompt, schema, folder):
            calls.append('claude')
            self.assertNotIn('OPENAI_API_KEY', agents._env())
            return {'answer': 'ok'}

        with patch.dict(agents.RUNNERS, {'codex': codex, 'claude': claude}), \
                patch('resume.jobs.agents.shutil.which', return_value='/bin/x'), \
                patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-secret'}):
            runner = agents.Agents()
            schema = {'type': 'object', 'properties': {'answer': {'type': 'string'}}}
            self.assertEqual(runner.ask('q', schema), {'answer': 'ok'})
            self.assertEqual(runner.ask('q', schema), {'answer': 'ok'})
        self.assertEqual(calls, ['codex', 'claude', 'claude'])  # a failed agent is skipped for the run
        self.assertEqual(runner.used, ['claude'])

    def test_reports_when_every_agent_fails(self):
        def broken(prompt, schema, folder):
            raise agents.AgentError('logged out')
        with patch.dict(agents.RUNNERS, {'codex': broken, 'claude': broken}), \
                patch('resume.jobs.agents.shutil.which', return_value='/bin/x'):
            with self.assertRaisesRegex(agents.AgentError, 'Codex: logged out; Claude Code: logged out'):
                agents.Agents().ask('q', {'type': 'object', 'properties': {}})


class EligibilityTests(unittest.TestCase):
    def test_profile_years_counts_overlaps_once(self):
        profile = {'experience': [{'start': 'Jan 2024', 'end': 'Dec 2024'}, {'start': 'Jun 2024', 'end': 'Jun 2025'}]}
        self.assertEqual(eligibility.profile_years(profile), 1.5)

    def test_rule_fallback(self):
        self.assertEqual(eligibility.rule_check('Requires 3+ years of experience in Go.', 2.4)['eligible'], 'no')
        self.assertEqual(eligibility.rule_check('1-2 years of industry experience', 2.4)['eligible'], 'yes')
        self.assertEqual(eligibility.rule_check('Great team, 5 years old company.', 2.4)['eligible'], 'unclear')

    def test_agent_answers_and_rule_fallback_per_job(self):
        class Fake:
            used = ['codex']
            def ask(self, prompt, schema):
                self.prompt = prompt
                return {'results': [{'id': '1', 'min_years': 2, 'eligible': 'yes', 'reason': 'Needs 2 years.'}]}
        fake = Fake()
        out = eligibility.check([{'id': 1, 'title': 'SWE', 'requirements': '2 years'},
                                 {'id': 2, 'title': 'SWE II', 'requirements': '5+ years of experience'}], 2.4, fake)
        self.assertIn('2.4 years', fake.prompt)
        self.assertEqual(out[1]['by'], 'codex')
        self.assertEqual((out[2]['eligible'], out[2]['by']), ('no', 'rules'))


class StoreAndServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = JobService(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_title_filters(self):
        self.assertTrue(title_matches('Software Engineer II', 'engineer, developer', 'senior'))
        self.assertFalse(title_matches('Senior Software Engineer', 'engineer', 'senior, staff'))
        self.assertFalse(title_matches('Recruiter', 'engineer', ''))

    def test_listing_marks_new_and_closed_jobs_and_keeps_status(self):
        store = JobStore(self.root)
        src = store.add_source('Acme', 'https://x.test/careers', 'greenhouse')
        self.assertEqual(len(store.save_listing(src['id'], [job('a'), job('b')])), 2)
        first = {j['title']: j for j in store.jobs()}
        store.set_status(store.jobs()[0]['id'], 'applied')
        self.assertEqual(store.save_listing(src['id'], [job('a'), job('c', 'Backend Engineer')]), [store.jobs()[0]['id']])
        active = {j['url'] for j in store.jobs()}
        self.assertEqual(active, {'https://x.test/a', 'https://x.test/c'})
        with self.assertRaisesRegex(ValueError, 'already added'):
            store.add_source('Acme again', 'https://x.test/careers', 'greenhouse')
        self.assertTrue(first)

    def test_refresh_end_to_end_with_rule_fallback(self):
        self.service.add_source('Acme', 'https://job-boards.greenhouse.io/acme', exclude='senior')
        listing = [job('1', 'Software Engineer', '1+ years of experience'), job('2', 'Senior Engineer'),
                   job('3', 'Backend Engineer', '5+ years of professional experience')]
        with patch.dict(sources.READERS, {'greenhouse': lambda url: listing}), \
                patch('resume.jobs.agents.shutil.which', return_value=None):
            summary = self.service.refresh({'experience': [{'start': 'Jan 2024', 'end': 'Jun 2026'}]}, lambda m: None)
        self.assertIn('2 matching job(s), 2 new', summary)
        verdicts = {j['title']: j['eligible'] for j in self.service.store.jobs()}
        self.assertEqual(verdicts, {'Software Engineer': 'yes', 'Backend Engineer': 'no'})

    def test_source_errors_are_recorded_not_raised(self):
        self.service.add_source('Broken', 'https://job-boards.greenhouse.io/broken')
        def fail(url):
            raise FetchError('The site returned HTTP 404.')
        with patch.dict(sources.READERS, {'greenhouse': fail}), patch('resume.jobs.agents.shutil.which', return_value=None):
            summary = self.service.refresh({'experience': []}, lambda m: None)
        self.assertIn('could not read: Broken', summary)
        self.assertEqual(self.service.store.sources()[0]['last_error'], 'The site returned HTTP 404.')

    def test_changing_experience_resets_checks(self):
        store = self.service.store
        src = store.add_source('Acme', 'https://x.test/careers', 'greenhouse')
        store.save_listing(src['id'], [job('a')])
        store.set_eligibility({store.jobs()[0]['id']: {'min_years': 3, 'eligible': 'no', 'reason': 'r', 'by': 'codex'}})
        self.service.set_experience_years('3.5')
        self.assertEqual(self.service.experience_years({'experience': []})['years'], 3.5)
        self.assertIsNone(store.jobs()[0]['eligible'])
        with self.assertRaises(ValueError):
            self.service.set_experience_years('many')


if __name__ == '__main__':
    unittest.main()
