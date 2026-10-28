"""Tests for the new career-platform readers and the curl fetch fallback."""
import json
import unittest
from unittest.mock import patch

from resume.jobs import sources
from resume.jobs import fetch as fetchmod
from resume.jobs.fetch import FetchError


def fake_get_json(map_by_prefix):
    """get_json stub keyed by the first path segment(s) of the URL."""
    def get(url):
        for prefix, value in map_by_prefix.items():
            if prefix in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise FetchError('no fixture for ' + url)
    return get


class DetectNewPlatforms(unittest.TestCase):
    def test_detects_new_platform_hosts(self):
        cases = {
            'https://careers.qualcomm.com/careers': 'pcsx',
            'https://morganstanley.eightfold.ai/careers': 'pcsx',
            'https://www.salesforce.com/company/careers/jobs/?bc=OTH': 'salesforce',
            'https://nurix.keka.com/careers/': 'keka',
            'https://careers.kula.ai/rocketlane?jobs=true': 'kula',
            'https://www.acko.com/careers/jobs/': 'page',
        }
        for url, kind in cases.items():
            with self.subTest(url=url):
                self.assertEqual(sources.detect(url), kind)


PCSX_PAGE1 = {'data': {'count': 3, 'positions': [
    {'id': 1, 'displayJobId': 'A1', 'name': 'Engineer I', 'locations': ['Hyderabad, India'],
     'postedTs': 1790000000, 'positionUrl': '/careers/job/1'},
    {'id': 2, 'displayJobId': 'A2', 'name': 'Engineer II', 'locations': ['Bangalore, India'],
     'postedTs': 1789900000, 'positionUrl': '/careers/job/2'}]}}
PCSX_PAGE2 = {'data': {'count': 3, 'positions': [
    {'id': 3, 'displayJobId': 'A3', 'name': 'Engineer III', 'locations': ['Pune, India'],
     'postedTs': 1789800000, 'positionUrl': '/careers/job/3'}]}}


class PcsxTests(unittest.TestCase):
    def test_reads_and_paginates_the_public_search(self):
        pages = [PCSX_PAGE1, PCSX_PAGE2]
        def get(url):
            assert '/api/pcsx/search' in url
            return pages.pop(0)
        with patch.object(sources, 'get_json', side_effect=get):
            jobs = sources.pcsx('https://morganstanley.eightfold.ai/careers')
        self.assertEqual([j['external_id'] for j in jobs], ['A1', 'A2', 'A3'])
        self.assertTrue(jobs[0]['url'].startswith('https://morganstanley.eightfold.ai/'))
        self.assertIn('position_details?position_id=1', jobs[0]['needs_detail'])
        self.assertEqual(jobs[0]['location'], 'Hyderabad, India')
        self.assertEqual(jobs[0]['posted'], '2026-09-21')  # 1790000000s UTC

    def test_rate_limited_later_pages_keep_the_jobs_already_read(self):
        pages = [PCSX_PAGE1, FetchError('The site returned HTTP 429.')]
        def get(url):
            value = pages.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        with patch.object(sources, 'get_json', side_effect=get):
            jobs = sources.pcsx('https://careers.qualcomm.com/careers')
        self.assertEqual(len(jobs), 2)

    def test_first_page_failure_raises(self):
        with patch.object(sources, 'get_json', side_effect=FetchError('The site returned HTTP 429.')):
            with self.assertRaises(FetchError):
                sources.pcsx('https://careers.qualcomm.com/careers')


SALESFORCE_FIXTURE = {'Report_Entry': [{
    'Job_Requisition_Ref_ID': 'JR1', 'Job_Posting_Title': 'Member Technical Staff',
    'Job_Requisition_Primary_Location': 'India - Hyderabad',
    'External_Job_Posting_Site': 'https://salesforce.wd12.myworkdayjobs.com/x',
    'External_Job_Posting_Start_Date': '2026-10-20', 'Job_Description': '<p>Build <b>cloud</b> features.</p>'}]}


class SalesforceTests(unittest.TestCase):
    def test_reads_the_public_static_dump(self):
        with patch.object(sources, 'get_json', fake_get_json({'jobs_1.json': SALESFORCE_FIXTURE,
                                                             'jobs_2.json': FetchError('no segment')})):
            jobs = sources.salesforce('https://www.salesforce.com/company/careers/jobs/')
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['external_id'], 'JR1')
        self.assertEqual(jobs[0]['posted'], '2026-10-20')
        self.assertEqual(jobs[0]['location'], 'India - Hyderabad')
        self.assertIn('Build cloud features', jobs[0]['description'])


KEKA_PAGE = '<html><script src="https://nurix.keka.com/careers/api/embedjobs/js/a0c73184-bf37-48f4-9f6d-68b53e151e05"></script></html>'
KEKA_ROWS = [
    {'id': 89072, 'jobNumber': 'NU0106', 'title': 'UI/UX Engineer', 'publishedOn': '2026-09-18T12:28:11.16Z',
     'excerpt': 'UX Engineer Location: Bangalore, India Experience: 0-2 years',
     'description': '<div><strong>UX Engineer</strong> Location: Bangalore, India</div>'},
    {'id': 89073, 'jobNumber': 'NU0107', 'title': 'Product Designer', 'publishedOn': '2026-09-14T10:00:00Z',
     'excerpt': '', 'description': '<div>Design everything.</div>'},
]


class KekaTests(unittest.TestCase):
    def test_reads_the_embed_feed_after_finding_its_hash(self):
        def get(url):
            if url.startswith('https://nurix.keka.com/careers/') and 'embedjobs' not in url:
                return KEKA_PAGE
            if 'embedjobs/default/active' in url:
                return KEKA_ROWS
            raise FetchError('no fixture for ' + url)
        with patch.object(sources, 'get_text', side_effect=get), \
                patch.object(sources, 'get_json', side_effect=lambda url: KEKA_ROWS):
            jobs = sources.keka('https://nurix.keka.com/careers/')
        self.assertEqual([j['external_id'] for j in jobs], ['NU0106', 'NU0107'])
        self.assertEqual(jobs[0]['location'], 'Bangalore, India')
        self.assertEqual(jobs[0]['posted'], '2026-09-18')
        self.assertEqual(jobs[0]['url'], 'https://nurix.keka.com/careers/89072')

    def test_a_portal_without_a_feed_hash_fails_closed(self):
        with patch.object(sources, 'get_text', return_value='<html>plain page</html>'):
            with self.assertRaises(FetchError):
                sources.keka('https://nurix.keka.com/careers/')


KULA_LISTING = '''
<html><body>
<a class="card" href="/rocketlane/2112-technical-account-manager-us-timezone?jobs=true"><span>Apply Now</span></a>
<a class="card" href="/rocketlane/2230-senior-software-engineer-frontend?jobs=true"><span>Apply Now</span></a>
<a class="card" href="/rocketlane/2230-duplicate?jobs=true"><span>Apply Now</span></a>
<a href="/other/link">x</a>
</body></html>'''
KULA_JOB = ('<html><head><title>Senior Software Engineer-Frontend \u2013 Chennai | Rocketlane Careers</title></head>'
            '<body>Work type: Full time · Workplace: On-Site Chennai, Tamil Nadu, India Job details <p>Build UIs.</p></body></html>')


class KulaTests(unittest.TestCase):
    def test_listing_links_become_jobs_with_slug_titles(self):
        with patch.object(sources, 'get_text', return_value=KULA_LISTING):
            jobs = sources.kula('https://careers.kula.ai/rocketlane?jobs=true')
        self.assertEqual([j['external_id'] for j in jobs], ['2112', '2230'])
        self.assertEqual(jobs[1]['title'], 'Senior Software Engineer Frontend')
        self.assertEqual(jobs[1]['url'], 'https://careers.kula.ai/rocketlane/2230-senior-software-engineer-frontend?jobs=true')

    def test_detail_recovers_the_real_title_and_location(self):
        with patch.object(sources, 'get_text', return_value=KULA_JOB):
            detail = sources.kula_detail({'needs_detail': 'https://careers.kula.ai/rocketlane/2230-x?jobs=true'})
        self.assertEqual(detail['title'], 'Senior Software Engineer-Frontend')
        self.assertEqual(detail['location'], 'Chennai, Tamil Nadu, India')
        self.assertIn('Build UIs.', detail['description'])


class CurlFallbackTests(unittest.TestCase):
    def _run_ok(self, body=b'<html>x</html>', code=200, final=b'https://x.test/jobs', rc=0):
        class P:
            stdout = body + f'\nJHALCV_META {code} '.encode() + final
            returncode = rc
        return P()

    def test_urllib_timeouts_fall_back_to_curl(self):
        import urllib.error
        def boom(*a, **k):
            raise urllib.error.URLError('handshake timed out')
        with patch.object(fetchmod._OPENER, 'open', side_effect=boom), \
                patch.object(fetchmod.subprocess, 'run', return_value=self._run_ok()), \
                patch.object(fetchmod, 'remaining', return_value=10):
            self.assertEqual(fetchmod.get_text('https://x.test/jobs'), '<html>x</html>')

    def test_curl_http_error_raises_fetch_error(self):
        import urllib.error
        with patch.object(fetchmod._OPENER, 'open', side_effect=urllib.error.URLError('t')), \
                patch.object(fetchmod.subprocess, 'run',
                             return_value=self._run_ok(body=b'nope', code=403)), \
                patch.object(fetchmod, 'remaining', return_value=10):
            with self.assertRaises(FetchError):
                fetchmod.get_text('https://x.test/jobs')

    def test_curl_redirect_to_a_private_host_is_refused(self):
        import urllib.error
        with patch.object(fetchmod._OPENER, 'open', side_effect=urllib.error.URLError('t')), \
                patch.object(fetchmod.subprocess, 'run',
                             return_value=self._run_ok(final=b'http://127.0.0.1:8765/jobs')), \
                patch.object(fetchmod, 'remaining', return_value=10):
            with self.assertRaises(FetchError):
                fetchmod.get_text('https://x.test/jobs')


class ResolveNewPlatforms(unittest.TestCase):
    def test_found_in_page_recognises_new_boards(self):
        page = 'see https://careers.kula.ai/rocketlane?jobs=true and https://nurix.keka.com/careers/ plus ' \
               'https://morganstanley.eightfold.ai/careers'
        hits = fetchmod and None  # silence linters; real check below
        from resume.jobs import resolve
        hits = resolve.found_in_page(page)
        self.assertIn('https://careers.kula.ai/rocketlane', hits)
        self.assertIn('https://nurix.keka.com/careers/', hits)
        self.assertIn('https://morganstanley.eightfold.ai/careers', hits)


if __name__ == '__main__':
    unittest.main()
