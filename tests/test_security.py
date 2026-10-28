"""Regression tests for the October 26 security hardening."""
import os
import socket
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from resume import cli, llm
from resume.jobs import agents, fetch, sources
from resume.jobs.fetch import FetchError, check_url


class CheckUrlTests(unittest.TestCase):
    def test_private_and_non_web_targets_are_rejected(self):
        for url in ('http://127.0.0.1:8765/api', 'http://10.0.0.5/x', 'http://169.254.169.254/latest',
                    'ftp://example.com/x', 'javascript:alert(1)', 'file:///etc/passwd',
                    'http://user:pass@example.com/'):
            with self.subTest(url=url):
                with self.assertRaises(FetchError):
                    check_url(url)

    def test_hostname_answering_with_a_private_address_is_rejected(self):
        infos = [(socket.AF_INET, 1, 6, '', ('10.1.2.3', 0))]
        with patch.object(fetch.socket, 'getaddrinfo', return_value=infos):
            with self.assertRaises(FetchError):
                check_url('https://rebinds.example/jobs')

    def test_transient_dns_failure_fails_open(self):
        with patch.object(fetch.socket, 'getaddrinfo', side_effect=socket.gaierror(8, 'nx')):
            self.assertEqual(check_url('https://unreachable.example/jobs'),
                             'https://unreachable.example/jobs')

    def test_returned_url_is_normalized(self):
        result = check_url(' https://example.com/a\r\nX: y\r\n')
        self.assertNotIn('\r', result)
        self.assertNotIn('\n', result)


class RedirectGuardTests(unittest.TestCase):
    def test_redirect_to_a_private_host_is_refused_before_being_followed(self):
        guard = fetch._GuardedRedirects()
        with patch.object(urllib.request.HTTPRedirectHandler, 'redirect_request', return_value=None) as follow:
            with self.assertRaises(FetchError):
                guard.redirect_request(Mock(), None, 302, 'found', {}, 'http://127.0.0.1:8765/state')
            follow.assert_not_called()
            self.assertIsNone(guard.redirect_request(Mock(), None, 302, 'found', {}, 'https://example.com/x'))
            follow.assert_called_once()


class StoredJobUrlTests(unittest.TestCase):
    def test_only_web_links_are_stored(self):
        self.assertEqual(sources._job('1', 'T', url='https://x.test/jobs/1')['url'], 'https://x.test/jobs/1')
        for bad in ('javascript:alert(1)', 'file:///etc/passwd', 'data:text/html,x', '/relative/path'):
            with self.subTest(url=bad):
                self.assertEqual(sources._job('1', 'T', url=bad)['url'], '')

    def test_json_ld_relative_urls_resolve_and_schemes_are_dropped(self):
        page = ('<script type="application/ld+json">{"@type":"JobPosting","title":"Eng","url":"/jobs/1"}</script>'
                '<script type="application/ld+json">{"@type":"JobPosting","title":"Bad","url":"javascript:alert(1)"}</script>')
        jobs = sources._json_ld_jobs(page, 'https://board.test/careers')
        self.assertEqual(jobs[0]['url'], 'https://board.test/jobs/1')
        self.assertEqual(jobs[1]['url'], '')

    def test_greenhouse_and_lever_reject_malformed_board_tokens(self):
        with self.assertRaises(FetchError):
            sources.greenhouse('https://boards.greenhouse.io/embed/job_board?for=x%3Fy')
        with self.assertRaises(FetchError):
            sources.lever('https://jobs.lever.co/../etc')


class PageParsingTests(unittest.TestCase):
    def test_block_and_tag_stripping_matches_the_old_behavior(self):
        text = fetch.html_to_text('<p>Hi</p><script>evil()<b>x</script>after<style>.q{}</style>')
        self.assertNotIn('evil', text)
        self.assertNotIn('.q', text)
        self.assertIn('Hi', text)
        self.assertIn('after', text)
        # An unclosed element keeps its content, like the removed regex.
        self.assertIn('no close', fetch.html_to_text('<p>A</p><script>no close<p>B</p>'))

    def test_page_with_links_keeps_absolute_links_and_skips_schemes(self):
        text, links = fetch.page_with_links(
            '<a href="/jobs/1">Eng</a><a href="javascript:x()">j</a><a href="data:text/html,x">d</a>',
            'https://board.test/')
        self.assertEqual(links, {'https://board.test/jobs/1'})
        self.assertIn('[link: https://board.test/jobs/1]', text)

    def test_anchor_stripping_stays_fast_with_many_anchors(self):
        page = ''.join(f'<a href="/x{i}">t</a>' for i in range(5000)) + 'end'
        text, links = fetch.page_with_links(page, 'https://board.test/')
        self.assertEqual(len(links), 5000)


class CliPlainTests(unittest.TestCase):
    def test_model_text_is_stripped_of_terminal_control_sequences(self):
        self.assertEqual(cli._plain('\x1b]52;c;abc\x07ok\x1b[31mred\x07'), 'okred')
        self.assertEqual(cli._plain(None), '')


class CodexSandboxTests(unittest.TestCase):
    def test_codex_wraps_in_seatbelt_and_overrides_home(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = Mock(returncode=0, stdout='', stderr='')
            with patch.object(agents, '_seatbelt', return_value=['/usr/bin/sandbox-exec', '-f', '/tmp/x.sb']), \
                    patch.object(agents.subprocess, 'run', return_value=proc) as run:
                with self.assertRaises(agents.AgentError):
                    agents._codex('page text', {'type': 'object'}, Path(folder))
            argv = run.call_args.args[0]
            self.assertEqual(argv[:2], ['/usr/bin/sandbox-exec', '-f'])
            self.assertIn('--sandbox', argv)
            self.assertEqual(argv[argv.index('--sandbox') + 1], 'read-only')
            env = run.call_args.kwargs['env']
            self.assertEqual(env['HOME'], folder)
            self.assertTrue(env['CODEX_HOME'])
            self.assertNotIn('OPENAI_API_KEY', env)

    def test_seatbelt_denies_secret_paths_when_available(self):
        args = agents._seatbelt()
        if not args:
            self.skipTest('sandbox-exec unavailable')
        allowed = subprocess.run(args + ['/bin/echo', 'ok'], capture_output=True)
        denied = subprocess.run(args + ['/bin/cat', str(Path.home() / '.env')], capture_output=True)
        self.assertEqual(allowed.returncode, 0)
        self.assertNotEqual(denied.returncode, 0)


class CachePermissionTests(unittest.TestCase):
    def test_llm_cache_files_are_user_only(self):
        with tempfile.TemporaryDirectory() as folder:
            llm._cache_write('abc', {'q': 1}, {'a': 2}, directory=folder)
            mode = os.stat(Path(folder) / 'abc.json').st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_load_env_tightens_env_file_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / '.env'
            env.write_text('_JHALCV_TEST_SECRET=1\n')
            env.chmod(0o644)
            try:
                llm.load_env(env)
                self.assertEqual(env.stat().st_mode & 0o777, 0o600)
            finally:
                os.environ.pop('_JHALCV_TEST_SECRET', None)


if __name__ == '__main__':
    unittest.main()
