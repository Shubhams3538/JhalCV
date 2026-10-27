import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from resume.jobs import agents, eligibility, sources
from resume.jobs.fetch import FetchError, check_url, page_with_links
from resume.jobs.service import JobService, title_matches
from resume.jobs.store import JobStore

# Tests use fake hosts; never launch the real browser unless a test asks for it.
_no_browser = patch('resume.jobs.sources.browser', return_value=None)
_no_resolver = patch('resume.jobs.service.resolve', return_value=None)


def setUpModule():
    _no_browser.start()
    _no_resolver.start()


def tearDownModule():
    _no_browser.stop()
    _no_resolver.stop()


def job(ext, title='Software Engineer', reqs='Bachelor degree. 3+ years of software development experience.'):
    return {'external_id': ext, 'title': title, 'location': 'Bengaluru', 'url': f'https://x.test/{ext}',
            'posted': date.today().isoformat(), 'requirements': reqs, 'description': reqs}


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
    def test_lever_preserves_explicit_country_for_city_only_locations(self, fetch):
        fetch.return_value = [{'id': 'in', 'text': 'Engineer', 'categories': {'location': 'Bengaluru'}, 'country': 'IN'},
                              {'id': 'us', 'text': 'Engineer', 'categories': {'location': 'San Francisco'}, 'country': 'US'}]
        jobs = sources.lever('https://jobs.lever.co/example')
        self.assertEqual([j['location'] for j in jobs], ['Bengaluru, India', 'San Francisco'])

    @patch('resume.jobs.sources.get_json')
    def test_ashby_preserves_country_and_secondary_offices(self, fetch):
        fetch.return_value = {'jobs': [{'id': '1', 'title': 'Engineer', 'location': 'Bengaluru',
            'address': {'postalAddress': {'addressCountry': 'IND'}},
            'secondaryLocations': [{'location': 'San Francisco', 'address': {'postalAddress': {'addressCountry': 'USA'}}}]}]}
        job = sources.ashby('https://jobs.ashbyhq.com/example')[0]
        self.assertEqual(job['location'], 'Bengaluru, India; San Francisco')

    @patch('resume.jobs.sources.post_json')
    def test_workday_preserves_verified_selected_country_from_nested_facets(self, fetch):
        fetch.return_value = {'total': 1, 'jobPostings': [{'title': 'Engineer', 'locationsText': 'Bangalore',
            'externalPath': '/job/Bangalore/Engineer_123', 'postedOn': 'Posted Today'}],
            'facets': [{'facetParameter': 'locationMainGroup', 'values': [
                {'facetParameter': 'locationCountry', 'values': [{'id': 'india-id', 'descriptor': 'India'}]}]}]}
        job = sources.workday('https://example.wd5.myworkdayjobs.com/Careers?locationCountry=india-id')[0]
        self.assertEqual(job['location'], 'Bangalore, India')
        self.assertEqual(fetch.call_args.args[1]['appliedFacets'], {'locationCountry': ['india-id']})
        mixed = sources.workday('https://example.wd5.myworkdayjobs.com/Careers?locationCountry=india-id&locationCountry=us-id')[0]
        self.assertEqual(mixed['location'], 'Bangalore')

    @patch('resume.jobs.sources.post_json')
    def test_workday_keeps_verified_country_on_later_pages_without_facets(self, fetch):
        fetch.side_effect = [
            {'total': 2, 'jobPostings': [{'title': 'A', 'externalPath': '/job/A_1', 'locationsText': 'Bangalore'}],
             'facets': [{'facetParameter': 'locationCountry', 'values': [{'id': 'in', 'descriptor': 'India'}]}]},
            {'total': 2, 'jobPostings': [{'title': 'B', 'externalPath': '/job/B_2', 'locationsText': 'Noida'}]}]
        jobs = sources.workday('https://example.wd5.myworkdayjobs.com/Careers?locationCountry=in')
        self.assertEqual([j['location'] for j in jobs], ['Bangalore, India', 'Noida, India'])

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
        self.assertIn('sort=recent', url)
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
        self.assertIn('sort_by=date', get_text.call_args.args[0])
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

    @patch('resume.jobs.sources.get_json')
    @patch('resume.jobs.sources.get_text')
    def test_page_reader_follows_embedded_greenhouse_board(self, get_text, get_json):
        get_text.return_value = ('<main>Explore roles</main>'
                                 '<script src="https://boards.greenhouse.io/embed/job_board/js?for=towerresearchcapital"></script>')
        get_json.return_value = {'jobs': [{'id': 42, 'title': 'Software Engineer',
                                          'first_published': date.today().isoformat(),
                                          'absolute_url': 'https://tower-research.com/open-positions/?gh_jid=42',
                                          'content': '<p>Build trading systems</p>',
                                          'location': {'name': 'New York'}}]}
        jobs = sources.page_jobs('https://tower-research.com/roles/',
                                 lambda prompt, schema: self.fail('The embedded board should not need AI.'))
        self.assertEqual(get_json.call_args.args[0],
                         'https://boards-api.greenhouse.io/v1/boards/towerresearchcapital/jobs?content=true')
        self.assertEqual([(j['title'], j['posted']) for j in jobs],
                         [('Software Engineer', date.today().isoformat())])
        self.assertIn('Build trading systems', jobs[0]['requirements'])

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


class AgentCacheTests(unittest.TestCase):
    def test_unchanged_prompt_reuses_answer_for_days(self):
        calls = []
        def codex(prompt, schema, folder):
            calls.append(prompt)
            return {'answer': 'ok'}
        schema = {'type': 'object', 'properties': {'answer': {'type': 'string'}}}
        with tempfile.TemporaryDirectory() as cache, patch.dict(agents.RUNNERS, {'codex': codex}), \
                patch('resume.jobs.agents.shutil.which', return_value='/bin/x'):
            agents.Agents(order=('codex',), cache_dir=cache).ask('same page', schema)
            three_days = agents.time.time() + 3 * 86400
            with patch('resume.jobs.agents.time.time', return_value=three_days):
                agents.Agents(order=('codex',), cache_dir=cache).ask('same page', schema)
                agents.Agents(order=('codex',), cache_dir=cache).ask('changed page', schema)
            with patch('resume.jobs.agents.time.time', return_value=three_days + 12 * 86400):
                agents.Agents(order=('codex',), cache_dir=cache).ask('same page', schema)
        self.assertEqual(calls, ['same page', 'changed page', 'same page'])


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

    def test_one_crashing_company_does_not_abort_the_refresh(self):
        self.service.add_source('Good', 'https://job-boards.greenhouse.io/good')
        self.service.add_source('Crash', 'https://job-boards.greenhouse.io/crash')
        def reader(url):
            if url.endswith('crash'):
                raise RuntimeError('unexpected parser bug')
            return [job('g1')]
        with patch.dict(sources.READERS, {'greenhouse': reader}), patch('resume.jobs.agents.shutil.which', return_value=None):
            summary = self.service.refresh({'experience': []}, lambda m: None)
        self.assertIn('could not read: Crash', summary)
        self.assertEqual([j['url'] for j in self.service.store.jobs()], ['https://x.test/g1'])
        errors = {s['name']: s['last_error'] for s in self.service.store.sources()}
        self.assertEqual(errors['Crash'], 'unexpected parser bug')
        self.assertIsNone(errors['Good'])

    def test_unreadable_job_page_is_skipped_not_fatal(self):
        def detail(item):
            if item['external_id'] == 'bad':
                raise KeyError('missing field')
            return {'requirements': 'Needs 1 year', 'description': 'Build', 'posted': date.today().isoformat()}
        items = [{'external_id': 'bad'}, {'external_id': 'good'}]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(2) as pool:
            got = list(JobService._details(items, detail, pool, None))
        self.assertEqual([item['external_id'] for item, _ in got], ['good'])

    def test_title_filters(self):
        self.assertTrue(title_matches('Software Engineer II', 'engineer, developer', 'senior'))
        self.assertFalse(title_matches('Senior Software Engineer', 'engineer', 'senior, staff'))
        self.assertFalse(title_matches('Recruiter', 'engineer', ''))

    def test_listing_marks_availability_and_retains_saved_jobs_and_status(self):
        store = JobStore(self.root)
        src = store.add_source('Acme', 'https://x.test/careers', 'greenhouse')
        self.assertEqual(len(store.save_listing(src['id'], [job('a'), job('b')])), 2)
        first = {j['title']: j for j in store.jobs()}
        store.set_status(store.jobs()[0]['id'], 'applied')
        self.assertEqual(store.save_listing(src['id'], [job('a'), job('c', 'Backend Engineer')]), [store.jobs()[0]['id']])
        active = {j['url'] for j in store.jobs() if j['active']}
        self.assertEqual(active, {'https://x.test/a', 'https://x.test/c'})
        with self.assertRaisesRegex(ValueError, 'already added'):
            store.add_source('Acme again', 'https://x.test/careers', 'greenhouse')
        self.assertEqual(len(store.jobs()), 3)
        self.assertEqual(next(j for j in store.jobs() if j['url'].endswith('/b'))['status'], 'applied')
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

    def test_partial_refresh_reads_and_checks_only_chosen_sources(self):
        acme = self.service.add_source('Acme', 'https://job-boards.greenhouse.io/acme')
        other = self.service.add_source('Other', 'https://job-boards.greenhouse.io/other')
        boards = {'acme': [job('a1')], 'other': [job('o1')]}
        reads = []

        def reader(url):
            name = url.rsplit('/', 1)[-1]
            reads.append(name)
            return boards[name]

        profile = {'experience': [{'start': 'Jan 2024', 'end': 'Jun 2026'}]}
        with patch.dict(sources.READERS, {'greenhouse': reader}), patch('resume.jobs.agents.shutil.which', return_value=None):
            self.service.refresh(profile, lambda m: None)
            boards['other'].append(job('o2'))
            boards['acme'].append(job('a2'))
            reads.clear()
            summary = self.service.refresh(profile, lambda m: None, source_ids={other['id']})
        self.assertEqual(reads, ['other'])
        self.assertTrue(summary.startswith('Other: 2 matching job(s), 1 new'))
        by_ext = {j['url'].rsplit('/', 1)[-1]: j for j in self.service.store.jobs()}
        self.assertEqual(set(by_ext), {'a1', 'o1', 'o2'})  # Acme's new job was not fetched
        self.assertIsNotNone(by_ext['o2']['eligible'])
        self.assertEqual(self.service.store.sources()[0]['id'], acme['id'])

    def test_location_filter_excludes_other_countries_and_preserves_applied(self):
        src = self.service.add_source('Acme', 'https://job-boards.greenhouse.io/acme', locations='India')
        india = dict(job('india'), location='Gurgaon, Haryana, India')
        singapore = dict(job('singapore'), location='Singapore')
        with patch.dict(sources.READERS, {'greenhouse': lambda url: [singapore, india]}), \
                patch('resume.jobs.agents.shutil.which', return_value=None):
            summary = self.service.refresh({'experience': []}, lambda _: None)
        self.assertIn('1 matching job(s), 1 new', summary)
        self.assertEqual([j['url'] for j in self.service.store.jobs()], [india['url']])
        self.service.store.save_listing(src['id'], [singapore])  # previously saved before a filter change
        self.assertEqual([j['url'] for j in self.service.store.jobs()], [india['url']])
        self.assertEqual(self.service.store.unchecked(10), [])
        with self.service.store.db() as c:
            singapore_id = c.execute("SELECT id FROM jobs WHERE external_id='singapore'").fetchone()['id']
        self.service.store.set_status(singapore_id, 'applied')
        self.assertEqual({j['url'] for j in self.service.store.jobs()}, {india['url'], singapore['url']})
        self.service.set_source_locations(src['id'], 'Singapore')
        self.assertEqual([j['url'] for j in self.service.store.jobs() if j['status'] != 'applied'], [])

    def test_location_filter_shows_only_matching_offices_on_multi_location_job(self):
        src = self.service.add_source('Acme', 'https://job-boards.greenhouse.io/acme', locations='India')
        listing = dict(job('multi'), location='Gurgaon, Haryana, India; Singapore; New York, United States')
        self.service.store.save_listing(src['id'], [listing])
        self.assertEqual(self.service.store.jobs()[0]['location'], 'Gurgaon, Haryana, India')

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


class FreshnessTests(unittest.TestCase):
    def test_seven_day_boundary_and_unknown_dates(self):
        from resume.jobs.freshness import freshness
        today = date(2026, 10, 24)
        for value, expected in [('2026-10-24', 'recent'), ('2026-10-17', 'recent'),
                                ('2026-10-16', 'expired'), ('2026-10-25', 'unknown'),
                                ('2026-02-30', 'unknown'), ('', 'unknown'), ('not a date', 'unknown')]:
            with self.subTest(value=value):
                self.assertEqual(freshness(value, today), expected)

    def test_source_date_validation_and_workday_relative_dates(self):
        self.assertEqual(sources._date('2026-02-30T00:00:00Z'), '')
        self.assertEqual(sources._date('Posted Today'), date.today().isoformat())
        self.assertEqual(sources._date('Posted Yesterday'), (date.today() - timedelta(days=1)).isoformat())
        self.assertEqual(sources._date('Posted 7 Days Ago'), (date.today() - timedelta(days=7)).isoformat())
        self.assertEqual(sources._date('Posted 30+ Days Ago'), (date.today() - timedelta(days=30)).isoformat())

    @patch('resume.jobs.sources.get_json')
    def test_greenhouse_update_date_is_not_a_posting_date(self, fetch):
        fetch.return_value = {'jobs': [{'id': 1, 'title': 'Engineer', 'updated_at': date.today().isoformat()}]}
        self.assertEqual(sources.greenhouse('https://job-boards.greenhouse.io/acme')[0]['posted'], '')

    @patch('resume.jobs.sources.get_json')
    def test_greenhouse_office_location_includes_country(self, fetch):
        fetch.return_value = {'jobs': [{'id': 1, 'title': 'Engineer', 'location': {'name': 'Gurgaon'},
                                        'offices': [{'location': 'Gurgaon, Haryana, India'}]}]}
        self.assertEqual(sources.greenhouse('https://job-boards.greenhouse.io/acme')[0]['location'],
                         'Gurgaon, Haryana, India')


class FreshnessServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = JobService(self.temp.name)
        self.store = self.service.store
        self.src = self.service.add_source('Acme', 'https://job-boards.greenhouse.io/acme')

    def tearDown(self):
        self.temp.cleanup()

    def test_refresh_filters_old_before_ai_and_caches_unchanged_decisions(self):
        seen = []
        class Fake:
            used = ['codex']
            last_used = 'codex'
            def ask(self, prompt, schema):
                if 'drop' in schema['properties']:
                    return {'drop': []}  # title screen keeps both titles
                seen.append(prompt)
                return {'results': [{'id': str(j['id']), 'min_years': 0, 'eligible': 'yes',
                                     'reason': 'No required years.', 'relevant': 'no' if j['title'] == 'Recruiter' else 'yes',
                                     'relevance_reason': 'Based on duties.'} for j in store.unchecked(120)]}
        store = self.store
        old = dict(job('old'), posted=(date.today() - timedelta(days=8)).isoformat())
        recent = dict(job('recent', 'Application Associate', 'Bachelor degree.'), description='Build and test software APIs.')
        unknown = dict(job('unknown', 'Recruiter', 'Bachelor degree.'), posted='')
        with patch.dict(sources.READERS, {'greenhouse': lambda url: [old, unknown, recent]}), \
                patch('resume.jobs.service.Agents', return_value=Fake()):
            self.service.refresh({'experience': []}, lambda m: None)
            first_calls = len(seen)
            self.service.refresh({'experience': []}, lambda m: None)
        self.assertEqual(first_calls, 2)  # today's jobs first, undated backlog after collection
        self.assertEqual(len(seen), first_calls)
        self.assertNotIn('https://x.test/old', {j['url'] for j in store.jobs()})
        rows = store.jobs()
        self.assertEqual([(j['title'], j['freshness'], j['relevant']) for j in rows],
                         [('Application Associate', 'recent', 'yes'), ('Recruiter', 'unknown', 'no')])
        self.assertIn('Build and test software APIs.', seen[0])
        self.assertIn('not just exact title matches', seen[0])

    def test_saved_jobs_survive_aging_and_order_by_posted_not_discovery(self):
        newest = dict(job('newest'), posted=date.today().isoformat())
        older = dict(job('older'), posted=(date.today() - timedelta(days=2)).isoformat())
        old = dict(job('old'), posted=(date.today() - timedelta(days=8)).isoformat())
        self.store.save_listing(self.src['id'], [newest, older, old, dict(job('unknown'), posted='')])
        self.assertEqual([j['url'] for j in self.store.jobs()],
                         ['https://x.test/newest', 'https://x.test/older', 'https://x.test/old', 'https://x.test/unknown'])
        self.assertEqual([j['posted'] for j in self.store.unchecked(1)], [date.today().isoformat()])

    def test_existing_checks_receive_relevance_check_and_changed_text_rechecks(self):
        self.store.save_listing(self.src['id'], [job('1')])
        ident = self.store.jobs()[0]['id']
        with self.store.db() as c:
            c.execute("UPDATE jobs SET eligible='yes', relevant=NULL WHERE id=?", (ident,))
        self.assertEqual(len(self.store.unchecked(120)), 1)
        self.store.set_eligibility({ident: {'min_years': 0, 'eligible': 'yes', 'reason': 'r', 'by': 'codex',
                                          'relevant': 'yes', 'relevance_reason': 'Builds software.'}})
        self.assertEqual(self.store.unchecked(120), [])
        self.store.save_listing(self.src['id'], [dict(job('1'), description='Changed duties')])
        self.assertEqual(len(self.store.unchecked(120)), 1)

    def test_legacy_database_migration_preserves_data(self):
        import sqlite3
        self.store.save_listing(self.src['id'], [job('1')])
        with sqlite3.connect(self.store.path) as c:
            # Reconstruct the previous schema without relying on modern DROP COLUMN support.
            c.execute('ALTER TABLE jobs RENAME TO legacy_jobs')
            from resume.jobs.store import SCHEMA
            c.executescript(SCHEMA)
            cols = [r[1] for r in c.execute('PRAGMA table_info(jobs)')]
            names = ','.join(cols)
            c.execute(f'INSERT INTO jobs ({names}) SELECT {names} FROM legacy_jobs')
            c.execute('DROP TABLE legacy_jobs')
        self.assertEqual(self.store.jobs()[0]['url'], 'https://x.test/1')
        self.assertIsNone(self.store.jobs()[0]['relevant'])

    def test_detail_date_old_is_never_sent_to_ai(self):
        # Listing date is unavailable; only the detail page reveals this is stale.
        with self.store.db() as c:
            c.execute("UPDATE sources SET kind='workday' WHERE id=?", (self.src['id'],))
        item = dict(job('1', reqs=''), posted='', needs_detail='https://x.test/detail')
        old_detail = {'posted': (date.today() - timedelta(days=9)).isoformat(),
                      'requirements': 'Build software.', 'description': 'Build software.'}
        with patch.dict(sources.READERS, {'workday': lambda url: [item]}), \
                patch.dict(sources.DETAILS, {'workday': lambda item: old_detail}), \
                patch('resume.jobs.service.Agents') as ai:
            self.service.refresh({'experience': []}, lambda m: None)
        # Only the cheap title screen may run before the date is known; no full-text check.
        prompts = [c.args[0] for c in ai.return_value.ask.call_args_list]
        self.assertTrue(all(p.startswith('Below are job titles') for p in prompts))
        self.assertEqual(self.store.jobs(), [])

    def test_deferred_details_are_retried_next_refresh(self):
        with self.store.db() as c:
            c.execute("UPDATE sources SET kind='workday' WHERE id=?", (self.src['id'],))
        item = dict(job('1', reqs=''), needs_detail='https://x.test/detail')
        calls = []
        def detail(item):
            calls.append(1)
            if len(calls) == 1:
                raise FetchError('temporarily unavailable')
            return {'posted': date.today().isoformat(), 'requirements': '1 year of experience', 'description': 'Build software'}
        with patch.dict(sources.READERS, {'workday': lambda url: [item]}), \
                patch.dict(sources.DETAILS, {'workday': detail}), \
                patch('resume.jobs.agents.shutil.which', return_value=None):
            self.service.refresh({'experience': []}, lambda m: None)
            self.service.refresh({'experience': []}, lambda m: None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.store.job(self.store.jobs()[0]['id'])['description'], 'Build software')


class FreshnessRegressionTests(unittest.TestCase):
    setUp = FreshnessServiceTests.setUp
    tearDown = FreshnessServiceTests.tearDown
    def test_description_only_changes_persist_and_stop_rechecking(self):
        original = dict(job('1', reqs=''), description='Build software')
        self.store.save_listing(self.src['id'], [original])
        ident = self.store.jobs()[0]['id']
        verdict = {ident: {'min_years': None, 'eligible': 'unclear', 'reason': 'r', 'by': 'codex',
                           'relevant': 'yes', 'relevance_reason': 'Software'}}
        self.store.set_eligibility(verdict)
        changed = dict(original, description='Sell furniture')
        self.store.save_listing(self.src['id'], [changed])
        self.assertEqual(self.store.unchecked(1)[0]['description'], 'Sell furniture')
        self.store.set_eligibility(verdict)
        self.store.save_listing(self.src['id'], [changed])
        self.assertEqual(self.store.unchecked(1), [])

    def test_legacy_greenhouse_update_date_is_quarantined(self):
        import sqlite3
        self.store.save_listing(self.src['id'], [job('1')])
        with sqlite3.connect(self.store.path) as c:
            c.execute('ALTER TABLE jobs RENAME TO old_jobs')
            from resume.jobs.store import SCHEMA
            c.executescript(SCHEMA)
            names = ','.join(r[1] for r in c.execute('PRAGMA table_info(jobs)'))
            c.execute(f'INSERT INTO jobs ({names}) SELECT {names} FROM old_jobs')
            c.execute('DROP TABLE old_jobs')
        self.assertEqual(self.store.jobs()[0]['freshness'], 'unknown')
        # New authoritative first_published restores a trusted date after migration.
        self.store.save_listing(self.src['id'], [job('1')])
        self.assertEqual(self.store.jobs()[0]['freshness'], 'recent')

    @patch('resume.jobs.sources.get_text')
    def test_json_ld_keeps_recent_jobs_beyond_old_entries(self, fetch):
        old = (date.today() - timedelta(days=20)).isoformat()
        rows = [{'@type': 'JobPosting', 'title': 'Old job', 'url': f'https://x.test/{i}', 'datePosted': old}
                for i in range(sources.MAX_JOBS)]
        rows.append({'@type': 'JobPosting', 'title': 'New job', 'url': 'https://x.test/new',
                     'datePosted': date.today().isoformat()})
        fetch.return_value = '<script type="application/ld+json">' + json.dumps(rows) + '</script>'
        self.assertEqual(sources.page_jobs('https://x.test/', lambda p,s: {})[0]['title'], 'New job')


class DateReaderRegressionTests(unittest.TestCase):
    @patch('resume.jobs.sources.get_json')
    def test_newest_amazon_stops_paging_once_entire_page_is_old(self, fetch):
        fetch.return_value = {'hits': 1000, 'jobs': [{'id': '1', 'title': 'Engineer', 'posted_date': 'January 01, 2020'}]}
        sources.amazon('https://www.amazon.jobs/en/search?sort=relevant&country%5B%5D=IND')
        self.assertEqual(fetch.call_count, 1)
        self.assertIn('sort=recent', fetch.call_args.args[0])
        self.assertNotIn('sort=relevant', fetch.call_args.args[0])

    @patch('resume.jobs.sources.get_text')
    def test_page_agent_cannot_supply_a_date_missing_from_page_text(self, fetch):
        fetch.return_value = '<main>' + 'We are hiring engineers. ' * 20 + '<a href="/jobs/1">Engineer</a></main>'
        def ask(prompt, schema):
            return {'jobs': [{'title': 'Engineer', 'url': 'https://x.test/jobs/1', 'posted': date.today().isoformat()}]}
        self.assertEqual(sources.page_jobs('https://x.test/search', ask)[0]['posted'], '')


class SavedJobPersistenceTests(unittest.TestCase):
    setUp = FreshnessServiceTests.setUp
    tearDown = FreshnessServiceTests.tearDown

    def test_applied_survives_absent_listing_age_and_store_restart(self):
        ids = self.store.save_listing(self.src['id'], [job('1'), job('2')])
        self.store.set_status(ids[0], 'applied')
        self.store.set_status(ids[1], 'hidden')
        self.store.save_listing(self.src['id'], [])
        with self.store.db() as c:
            c.execute("UPDATE jobs SET posted='2020-01-01'")
        reopened = JobStore(self.temp.name)
        self.assertEqual({j['id']: (j['status'], j['active']) for j in reopened.jobs()},
                         {ids[0]: ('applied', 0), ids[1]: ('hidden', 0)})

    def test_deleted_jobs_are_not_reimported_and_ids_are_not_reused(self):
        ident = self.store.save_listing(self.src['id'], [job('1')])[0]
        self.store.delete_job(ident)
        self.assertEqual(self.store.jobs(), [])
        reopened = JobStore(self.temp.name)
        self.assertEqual(reopened.save_listing(self.src['id'], [job('1')]), [])
        new_id = reopened.save_listing(self.src['id'], [job('2')])[0]
        self.assertGreater(new_id, ident)
        with self.assertRaises(ValueError):
            reopened.delete_job(ident)
        self.assertEqual(reopened.jobs()[0]['id'], new_id)

    def test_applied_is_exempt_from_new_retention_policy(self):
        old = dict(job('saved'), posted='2020-01-01')
        ident = self.store.save_listing(self.src['id'], [old])[0]
        self.store.set_status(ident, 'applied')
        self.store.set_eligibility({ident: dict(min_years=0,eligible='yes',reason='Entry level',by='codex',
                                              relevant='yes',relevance_reason='Software')})
        with patch.dict(sources.READERS, {'greenhouse': lambda url: [old, dict(job('unseen'), posted='2020-01-01')]}), \
                patch('resume.jobs.agents.shutil.which', return_value=None):
            self.service.refresh({'experience': []}, lambda m: None)
        self.assertEqual([(j['id'], j['eligible'], j['relevant']) for j in self.store.jobs()], [(ident, 'yes', 'yes')])

    def test_legacy_ids_survive_source_deletion_before_next_insert(self):
        ident = self.store.save_listing(self.src['id'], [job('1')])[0]
        with self.store.db() as c:
            c.execute("DELETE FROM meta WHERE key='job_id_sequence'")
        self.store.remove_source(self.src['id'])
        source = self.store.add_source('Other', 'https://x.test/other', 'greenhouse')
        replacement = self.store.save_listing(source['id'], [job('2')])[0]
        self.assertGreater(replacement, ident)
        with self.assertRaises(ValueError):
            self.store.delete_job(ident)

    def test_recheck_includes_saved_inactive_jobs(self):
        ident = self.store.save_listing(self.src['id'], [job('1')])[0]
        self.store.save_listing(self.src['id'], [])
        self.service.set_experience_years(1)
        self.assertEqual([j['id'] for j in self.store.unchecked(10)], [ident])


class PostingDateBackfillTests(unittest.TestCase):
    setUp = FreshnessServiceTests.setUp
    tearDown = FreshnessServiceTests.tearDown

    @patch('resume.jobs.sources.get_text')
    def test_barclays_visible_date_fallback_and_no_locale_guess_elsewhere(self, fetch):
        fetch.return_value = '<h1>Engineer</h1><div>Date live: <span>08/24/2026</span></div><p>Build software</p>'
        self.assertEqual(sources.page_detail({'needs_detail': 'https://search.jobs.barclays/job/pune/engineer/1'})['posted'], '2026-08-24')
        self.assertEqual(sources.page_detail({'needs_detail': 'https://x.test/job/1'}).get('posted', ''), '')

    def test_saved_description_does_not_block_date_backfill(self):
        with self.store.db() as c:
            c.execute("UPDATE sources SET kind='page' WHERE id=?", (self.src['id'],))
        item = dict(job('1'), posted='')
        ident = self.store.save_listing(self.src['id'], [item])[0]
        self.store.set_eligibility({ident: dict(min_years=0,eligible='yes',reason='r',by='codex',
                                              relevant='yes',relevance_reason='Software')})
        # The old job has vanished from the first search page, but its direct URL is still saved.
        def detail(item):
            self.assertEqual(item['needs_detail'], 'https://x.test/1')
            return {'posted': date.today().isoformat()}
        with patch('resume.jobs.sources.page_jobs', return_value=[]), \
                patch.dict(sources.DETAILS, {'page': detail}), \
                patch('resume.jobs.service.Agents') as ai:
            self.service.refresh({'experience': []}, lambda m: None)
        saved = self.store.job(ident)
        self.assertEqual(saved['posted'], date.today().isoformat())
        self.assertEqual(saved['eligible'], 'yes')
        self.assertEqual(saved['description'], item['description'])
        ai.return_value.ask.assert_not_called()


class SimplifiedJobCategoriesTests(unittest.TestCase):
    setUp = FreshnessServiceTests.setUp
    tearDown = FreshnessServiceTests.tearDown

    def test_category_priority_and_seven_day_boundary(self):
        from resume.jobs.freshness import category
        base = dict(status='new', eligible='yes', relevant='yes', posted=date.today().isoformat())
        self.assertEqual(category(base), 'eligible')
        self.assertEqual(category(dict(base, eligible='no')), 'no')
        self.assertEqual(category(dict(base, relevant='no')), 'no')
        self.assertEqual(category(dict(base, eligible='unclear')), 'review')
        self.assertEqual(category(dict(base, posted='')), 'review')
        self.assertEqual(category(dict(base, posted=(date.today()-timedelta(days=7)).isoformat())), 'eligible')
        old = dict(base, posted=(date.today()-timedelta(days=8)).isoformat())
        self.assertEqual(category(old), 'old')
        self.assertEqual(category(dict(old, status='applied')), 'applied')

    def test_cleanup_boundary_protects_applied_unknown_and_prevents_recollection(self):
        today=date.today()
        items=[dict(job('eight'),posted=(today-timedelta(days=8)).isoformat()),
               dict(job('nine'),posted=(today-timedelta(days=9)).isoformat()),
               dict(job('applied'),posted='2020-01-01'),dict(job('unknown'),posted='')]
        ids=self.store.save_listing(self.src['id'],items)
        self.store.set_status(ids[2],'applied')
        self.store.prune_old(today)
        self.assertEqual({j['id'] for j in self.store.jobs()}, {ids[0],ids[2],ids[3]})
        self.assertEqual(self.store.save_listing(self.src['id'],[items[1]]),[])
        self.store.prune_old(today+timedelta(days=1))
        self.assertEqual({j['id'] for j in self.store.jobs()}, {ids[2],ids[3]})

    def test_bulk_delete_is_category_scoped_and_durable(self):
        ids=self.store.save_listing(self.src['id'],[job('a'),job('b')])
        self.store.set_status(ids[0],'applied')
        self.store.delete_category('applied')
        self.assertEqual([j['id'] for j in self.store.jobs()],[ids[1]])
        self.assertEqual(self.store.save_listing(self.src['id'],[job('a')]),[])
        with self.assertRaises(ValueError):
            self.store.delete_category('all')

    def test_empty_search_failure_does_not_block_saved_date_backfill(self):
        with self.store.db() as c:
            c.execute("UPDATE sources SET kind='page' WHERE id=?",(self.src['id'],))
        ident=self.store.save_listing(self.src['id'],[dict(job('a'),posted='')])[0]
        with patch('resume.jobs.sources.page_jobs',side_effect=FetchError('No listings')), \
                patch.dict(sources.DETAILS,{'page':lambda item: {'posted':date.today().isoformat()}}), \
                patch('resume.jobs.agents.shutil.which',return_value=None):
            self.service.refresh({'experience':[]},lambda m:None)
        self.assertEqual(self.store.job(ident)['posted'],date.today().isoformat())

    def test_undated_pages_do_not_starve_other_saved_dates(self):
        with self.store.db() as c:
            c.execute("UPDATE sources SET kind='page' WHERE id=?",(self.src['id'],))
        self.store.save_listing(self.src['id'],[dict(job('a'),posted=''),dict(job('b'),posted='')])
        calls=[]
        def detail(item):
            calls.append(item['needs_detail'])
            return {'posted':date.today().isoformat()} if item['needs_detail'].endswith('/b') else {}
        with patch('resume.jobs.sources.page_jobs',side_effect=FetchError('No listings')), \
                patch.dict(sources.DETAILS,{'page':detail}), \
                patch('resume.jobs.service.MAX_DETAILS',1), \
                patch('resume.jobs.agents.shutil.which',return_value=None):
            self.service.refresh({'experience':[]},lambda m:None)
            self.service.refresh({'experience':[]},lambda m:None)
        self.assertEqual(set(calls),{'https://x.test/a','https://x.test/b'})

    def test_new_expired_details_are_remembered_so_later_dates_get_checked(self):
        with self.store.db() as c:
            c.execute("UPDATE sources SET kind='page' WHERE id=?",(self.src['id'],))
        listing=[dict(job('a',reqs=''),posted='',needs_detail='https://x.test/a'),
                 dict(job('b',reqs=''),posted='',needs_detail='https://x.test/b')]
        calls=[]
        def detail(item):
            calls.append(item['needs_detail'])
            return {'posted': '2020-01-01' if item['needs_detail'].endswith('/a') else date.today().isoformat()}
        with patch('resume.jobs.sources.page_jobs',return_value=listing), \
                patch.dict(sources.DETAILS,{'page':detail}), \
                patch('resume.jobs.service.MAX_DETAILS',1), \
                patch('resume.jobs.agents.shutil.which',return_value=None):
            self.service.refresh({'experience':[]},lambda m:None)
            self.service.refresh({'experience':[]},lambda m:None)
        self.assertEqual(calls,['https://x.test/a','https://x.test/b'])
        self.assertEqual(self.store.jobs()[0]['posted'],date.today().isoformat())

    @patch('resume.jobs.sources.get_text')
    def test_barclays_visible_live_date_wins_over_conflicting_structured_date(self, fetch):
        structured={'@type':'JobPosting','title':'Engineer','datePosted':date.today().isoformat()}
        fetch.return_value='<script type="application/ld+json">'+json.dumps(structured)+'</script><div>Date live: 08/31/2026</div>'
        url='https://search.jobs.barclays/job/pune/engineer/1'
        self.assertEqual(sources.page_detail({'needs_detail':url})['posted'],'2026-08-31')
        self.assertEqual(sources.page_jobs(url,lambda p,s:{})[0]['posted'],'2026-08-31')


class BrowserRenderTests(unittest.TestCase):
    LISTING = ('<main>' + 'Open roles. ' * 30 + '<a href="/profile/job_details/11">WhatsApp - Software Engineer - iOS'
               '<div>Bangalore, India</div></a></main>')
    DETAIL = ('<script type="application/ld+json">' + json.dumps({
        '@type': 'JobPosting', 'title': 'WhatsApp - Software Engineer - iOS', 'datePosted': '2026-10-16T04:54:43-07:00',
        'description': 'Build WhatsApp.', 'responsibilities': 'Ship iOS features.',
        'qualifications': '2+ years of programming experience.',
        'jobLocation': [{'@type': 'Place', 'name': 'Bangalore, India'}]}) + '</script>')

    def test_listing_falls_back_to_the_browser_when_plain_request_fails(self):
        ask = lambda prompt, schema: {'note': '', 'jobs': [{'title': 'WhatsApp - Software Engineer - iOS', 'location': 'Bangalore, India',
                                                            'url': 'https://www.metacareers.com/profile/job_details/11', 'posted': ''}]}
        with patch('resume.jobs.sources.browser', return_value='/chrome'), \
                patch('resume.jobs.sources.get_text', side_effect=FetchError('The site returned HTTP 400.')), \
                patch('resume.jobs.sources.render_page', return_value=self.LISTING) as render:
            jobs = sources.page_jobs('https://www.metacareers.com/jobsearch/?q=india', ask)
        render.assert_called_once()
        self.assertEqual(jobs[0]['url'], 'https://www.metacareers.com/profile/job_details/11')

    def test_listing_without_structured_data_is_rendered(self):
        ask = lambda prompt, schema: {'note': '', 'jobs': [{'title': 'WhatsApp - Software Engineer - iOS', 'location': '',
                                                            'url': 'https://www.metacareers.com/profile/job_details/11', 'posted': ''}]}
        with patch('resume.jobs.sources.browser', return_value='/chrome'), \
                patch('resume.jobs.sources.get_text', return_value='<div id="root"></div>' + 'x' * 600), \
                patch('resume.jobs.sources.render_page', return_value=self.LISTING) as render:
            self.assertEqual(len(sources.page_jobs('https://www.metacareers.com/jobsearch/', ask)), 1)
        render.assert_called_once()

    def test_detail_reads_rendered_structured_posting(self):
        with patch('resume.jobs.sources.browser', return_value='/chrome'), \
                patch('resume.jobs.sources.get_text', return_value='<html>Log in</html>'), \
                patch('resume.jobs.sources.render_page', return_value=self.DETAIL):
            detail = sources.page_detail({'needs_detail': 'https://www.metacareers.com/profile/job_details/11'})
        self.assertEqual(detail['posted'], '2026-10-16')
        self.assertEqual(detail['requirements'], '2+ years of programming experience.')
        self.assertIn('Ship iOS features.', detail['description'])

    def test_static_structured_pages_skip_the_browser(self):
        with patch('resume.jobs.sources.browser', return_value='/chrome'), \
                patch('resume.jobs.sources.get_text', return_value=self.DETAIL), \
                patch('resume.jobs.sources.render_page') as render:
            sources.page_detail({'needs_detail': 'https://careers.x.test/job/11'})
        render.assert_not_called()


class CostSavingTests(unittest.TestCase):
    def test_full_requirements_still_reach_the_ai(self):
        class Fake:
            used, last_used, prompts = ['codex'], 'codex', []
            def ask(self, prompt, schema):
                self.prompts.append(prompt)
                return {'results': [{'id': '2', 'min_years': None, 'eligible': 'yes', 'reason': 'r',
                                     'relevant': 'yes', 'relevance_reason': 'r'}]}
        fake = Fake()
        out = eligibility.check([{'id': 1, 'title': 'SWE III', 'requirements': '8+ years of professional experience.'},
                                 {'id': 2, 'title': 'SWE', 'requirements': 'Bachelor degree.'}], 1.5, fake)
        self.assertEqual((out[1]['eligible'], out[1]['by']), ('no', 'rules'))
        self.assertEqual(out[2]['by'], 'codex')
        self.assertEqual(len(fake.prompts), 1)
        self.assertIn('SWE III', fake.prompts[0])

    def test_title_screen_drops_listed_titles_and_survives_failures(self):
        items = [{'external_id': 'a', 'title': 'Account Executive'}, {'external_id': 'b', 'title': 'Software Engineer'}]
        class Fake:
            def ask(self, prompt, schema):
                return {'drop': [{'id': '0', 'reason': 'Sales role.'}, {'id': '9', 'reason': 'out of range'}]}
        self.assertEqual(eligibility.screen_titles(items, 1.5, Fake()), {'a': 'Sales role.'})
        class Broken:
            def ask(self, prompt, schema):
                raise agents.AgentError('usage limit reached')
        self.assertEqual(eligibility.screen_titles(items, 1.5, Broken()), {})

    def test_screened_titles_skip_details_and_land_in_not_eligible(self):
        with tempfile.TemporaryDirectory() as root:
            service = JobService(root)
            src = service.add_source('Acme', 'https://acme.wd1.myworkdayjobs.com/External')
            listing = [dict(job('sales', 'Account Executive', ''), posted='', needs_detail='https://x.test/sales'),
                       dict(job('swe', 'Software Engineer', ''), posted='', needs_detail='https://x.test/swe')]
            opened = []
            def detail(item):
                opened.append(item['external_id'])
                return {'posted': date.today().isoformat(), 'requirements': 'Bachelor degree.', 'description': 'Build.'}
            class Fake:
                used, last_used, failed, order = ['codex'], 'codex', {}, ['codex']
                def ask(self, prompt, schema):
                    if 'drop' in schema['properties']:
                        return {'drop': [{'id': '0', 'reason': 'Sales role.'}]}
                    return {'results': []}
            with patch.dict(sources.READERS, {'workday': lambda url: listing}), \
                    patch.dict(sources.DETAILS, {'workday': detail}), \
                    patch('resume.jobs.service.Agents', return_value=Fake()), \
                    patch('resume.jobs.service.TITLE_SCREEN_MIN', 1):
                service.refresh({'experience': [{'start': 'Jan 2025', 'end': 'Jun 2026'}]}, lambda m: None)
            self.assertEqual(opened, ['swe'])
            sales = next(j for j in service.store.jobs() if j['title'] == 'Account Executive')
            self.assertEqual((sales['category'], sales['relevance_reason']), ('no', 'Sales role.'))
            self.assertEqual(src['kind'], 'workday')

    def test_queue_wait_is_not_charged_to_the_company_budget(self):
        import threading
        from resume.jobs import fetch
        slot = threading.BoundedSemaphore(1)
        slot.acquire()
        threading.Timer(0.3, slot.release).start()
        with fetch.fetch_budget(fetch.time.monotonic() + 0.2):
            fetch.acquire_slot(slot, 'busy')
            self.assertGreater(fetch.remaining(), 0.05)  # budget extended by the 0.3s wait
        slot.release()


class ResolverAndSmartRecruitersTests(unittest.TestCase):
    def test_finds_platform_boards_in_page_markup(self):
        from resume.jobs.resolve import found_in_page
        page = ('<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>'
                '<a href="https://jobs.lever.co/acme-labs/123">x</a>'
                '{"url":"https:\\/\\/acme.wd5.myworkdayjobs.com\\/en-US\\/External"}')
        self.assertEqual(found_in_page(page), ['https://job-boards.greenhouse.io/acme', 'https://jobs.lever.co/acme-labs',
                                               'https://acme.wd5.myworkdayjobs.com/External'])

    def test_guessed_board_must_belong_to_the_company(self):
        from resume.jobs import resolve
        boards = {('greenhouse', 'acme'): (5, 'Acme'), ('greenhouse', 'acmecorp'): (9, 'Someone Else')}
        with patch('resume.jobs.resolve.get_text', return_value='<html></html>'), \
                patch('resume.jobs.resolve._count', side_effect=lambda kind, token: boards.get((kind, token), (0, ''))):
            self.assertEqual(resolve.resolve('Acme', 'https://acme.com/careers', render=False)['url'],
                             'https://job-boards.greenhouse.io/acme')
            self.assertIsNone(resolve.resolve('Acme Corp', 'https://acmecorp.com/careers', render=False))

    @patch('resume.jobs.sources.get_json')
    def test_smartrecruiters_reader(self, get_json):
        get_json.return_value = {'totalFound': 1, 'content': [{
            'id': '77', 'name': 'Backend Engineer', 'releasedDate': '2026-10-23T07:00:19.336Z',
            'location': {'fullLocation': 'Bengaluru, , India', 'country': 'in'},
            'ref': 'https://api.smartrecruiters.com/v1/companies/Acme/postings/77'}]}
        self.assertEqual(sources.detect('https://careers.smartrecruiters.com/Acme'), 'smartrecruiters')
        job_ = sources.smartrecruiters('https://careers.smartrecruiters.com/Acme')[0]
        self.assertEqual((job_['location'], job_['posted'], job_['url']),
                         ('Bengaluru, India', '2026-10-23', 'https://jobs.smartrecruiters.com/Acme/77'))
        get_json.return_value = {'jobAd': {'sections': {'jobDescription': {'text': '<p>Build APIs</p>'},
                                                        'qualifications': {'text': '<li>2+ years</li>'}}}}
        detail = sources.smartrecruiters_detail(job_)
        self.assertEqual(detail['requirements'], '2+ years')


class UpgradeTests(unittest.TestCase):
    def test_page_source_is_upgraded_to_its_feed_once(self):
        with tempfile.TemporaryDirectory() as root:
            service = JobService(root)
            src = service.add_source('Acme', 'https://acme.com/careers', locations='India')
            store = service.store
            store.save_listing(src['id'], [dict(job('page-1'), location='Pune, India'), dict(job('page-2'), location='Pune, India')])
            store.set_status(next(j['id'] for j in store.jobs() if j['url'].endswith('page-1')), 'applied')
            found = {'url': 'https://job-boards.greenhouse.io/acme', 'kind': 'greenhouse', 'jobs': 3, 'evidence': 'x'}
            calls = []
            with patch('resume.jobs.service.resolve', side_effect=lambda *a, **k: calls.append(a) or found), \
                    patch.dict(sources.READERS, {'greenhouse': lambda url: [dict(job('gh-1'), location='Pune, India')]}), \
                    patch('resume.jobs.agents.shutil.which', return_value=None):
                service.refresh({'experience': []}, lambda m: None)
                service.refresh({'experience': []}, lambda m: None)
            saved = store.sources()[0]
            self.assertEqual((saved['kind'], saved['url'], saved['original_url']),
                             ('greenhouse', 'https://job-boards.greenhouse.io/acme', 'https://acme.com/careers'))
            self.assertEqual(len(calls), 1)  # not re-examined on the next refresh
            jobs = {j['url']: j['status'] for j in store.jobs()}
            # Switching feeds must retain both applied and unapplied saved jobs.
            self.assertEqual(set(jobs), {'https://x.test/page-1', 'https://x.test/page-2', 'https://x.test/gh-1'})
            self.assertEqual(jobs['https://x.test/page-1'], 'applied')

    def test_workday_with_country_selects_city_values(self):
        from resume.jobs import resolve
        facets = [{'facetParameter': 'locationMainGroup', 'values': [{'facetParameter': 'locations', 'descriptor': 'Locations', 'values': [
            {'descriptor': 'Pune, India', 'id': 'a'}, {'descriptor': 'Remote - Indiana', 'id': 'b'},
            {'descriptor': 'IN KA Bengaluru', 'id': 'c'}, {'descriptor': 'London', 'id': 'd'}]}]}]
        self.assertEqual(resolve._facet_values(facets, 'India'), ('locations', ['a', 'c']))


if __name__ == '__main__':
    unittest.main()
