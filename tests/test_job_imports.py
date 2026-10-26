import tempfile
import unittest

from resume.jobs.imports import preview_csv
from resume.jobs.service import JobService


class CompanyImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = JobService(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_preview_handles_bom_quotes_reversed_headers_and_bad_rows(self):
        result = preview_csv('\ufeffurl,company\r\nhttps://example.com/jobs,"Acme, Inc"\r\n'
                             'http://localhost/jobs,Private\r\nhttps://example.net,\r\n', [])
        self.assertEqual((result['ready'], result['invalid']), (1, 2))
        self.assertEqual(result['rows'][0]['company'], 'Acme, Inc')
        self.assertEqual(self.service.store.sources(), [])

    def test_preview_detects_name_url_and_file_duplicates(self):
        self.service.add_source('Existing', 'https://example.com/jobs?a=1&b=2')
        result = self.service.preview_sources('company,url\nDifferent,https://EXAMPLE.com/jobs/?b=2&a=1\n'
                                              ' existing ,https://elsewhere.com\nNew,https://new.example.com\n'
                                              'NEW,https://other.example.com\n')
        self.assertEqual((result['ready'], result['duplicates']), (1, 3))

    def test_import_is_atomic_and_revalidates_untrusted_rows(self):
        with self.assertRaises(ValueError):
            self.service.import_sources([{'company': 'Good', 'url': 'https://example.com'},
                                         {'company': 'Bad', 'url': 'http://127.0.0.1'}])
        self.assertEqual(self.service.store.sources(), [])

    def test_import_rechecks_duplicates_after_preview_and_uses_india(self):
        rows = [{'company': 'Acme', 'url': 'https://job-boards.greenhouse.io/acme'},
                {'company': 'Other', 'url': 'https://jobs.lever.co/other'}]
        self.service.add_source('Acme', rows[0]['url'])
        result = self.service.import_sources(rows)
        self.assertEqual(result, {'imported': 1, 'skipped': 1})
        source = next(s for s in self.service.store.sources() if s['name'] == 'Other')
        self.assertEqual((source['locations'], source['kind']), ('India', 'lever'))
        self.assertIsNone(source['last_run_at'])
        self.assertEqual(self.service.import_sources(rows), {'imported': 0, 'skipped': 2})

    def test_limits_headers_empty_files_and_malformed_csv(self):
        for content in ('', 'company,url\n', 'company,url,notes\nA,https://a.test,x',
                        'company,url\n"unclosed,https://example.com',
                        'company,url\n' + '\n'.join(f'C{i},https://example.com/{i}' for i in range(501))):
            with self.subTest(content=content[:50]), self.assertRaises(ValueError):
                preview_csv(content, [])
