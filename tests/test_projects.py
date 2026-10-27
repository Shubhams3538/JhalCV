import base64
import copy
import unittest
from unittest.mock import patch

from resume.descriptions import import_project, rewrite_projects, within
from resume.github import fetch_repository, repository, GitHubError

SETTINGS = {'tailorer': 'auto', 'model': 'test-model'}


class GitHubTests(unittest.TestCase):
    def test_repository_rejects_untrusted_urls(self):
        for url in ['http://github.com/me/repo', 'https://github.com.evil.test/me/repo', 'https://github.com/me/repo/tree/main',
                    'https://user:token@github.com/me/repo', 'https://github.com/me/..', 'https://github.com/me/repo?x=y', 'https://127.0.0.1/me/repo']:
            with self.subTest(url=url), self.assertRaises(GitHubError):
                repository(url)
        self.assertEqual(repository('https://github.com/me/repo.git'), ('me', 'repo'))

    @patch('resume.github.request')
    def test_readme_uses_repository_default_branch(self, request):
        readme = '# Project\nAn HTTP server using socket connections.\nHandles requests through a worker pool.'
        request.side_effect = [{'name': 'server', 'description': 'An HTTP server.', 'language': 'C++'},
                               {'encoding': 'base64', 'content': base64.b64encode(readme.encode()).decode()}]
        result = fetch_repository('https://github.com/me/server')
        self.assertEqual(result['readme'], readme)
        self.assertEqual(request.call_args_list[1].args[0], '/repos/me/server/readme')


class DescriptionTests(unittest.TestCase):
    def setUp(self):
        self.data = {'basics': {'name': 'Ada'}, 'experience': [{'bullets': ['Fixed fact']}], 'education': [], 'coding_profiles': [],
                     'projects': [{'name': 'Server', 'bullets': ['Built an HTTP server with a fixed worker pool.', 'Used sockets to handle incoming HTTP requests.']}], 'skills': []}

    def test_word_character_and_number_budgets(self):
        self.assertTrue(within('Built a server.', self.data['projects'][0]['bullets'][0]))
        self.assertFalse(within('Built 500 servers.', self.data['projects'][0]['bullets'][0]))
        self.assertFalse(within('a b c d e f g h i j k l m n o p', 'a single extraordinarilylongword'))
        self.assertFalse(within('Built\na server.', 'Built a server with sockets.'))

    @patch('resume.descriptions.llm.have_key', return_value=True)
    @patch('resume.descriptions.llm.ask')
    def test_invalid_rewrites_preserve_original_and_frozen_sections(self, ask, _):
        ask.return_value = ({'projects': [{'bullets': ['Handled 100000 requests.', 'Built distributed fault-tolerant clusters that scale to millions of users.']}]}, False)
        before = copy.deepcopy(self.data)
        result, notice = rewrite_projects(self.data, 'Distributed database job', SETTINGS)
        self.assertEqual(result, before)
        self.assertEqual(self.data, before)
        self.assertIn('Kept 2', notice)

    @patch('resume.descriptions.llm.have_key', return_value=True)
    @patch('resume.descriptions.llm.ask')
    def test_rewrites_must_keep_bold_phrases(self, ask, _):
        data = copy.deepcopy(self.data)
        data['projects'][0]['bullets'] = ['Built an **HTTP server** with a fixed **worker pool** today.',
                                          'Used **sockets** to handle incoming HTTP requests quickly.']
        ask.return_value = ({'projects': [{'bullets': ['Built an **HTTP server** with a **worker pool**.',
                                                       'Used sockets to handle HTTP requests.']}]}, False)
        result, notice = rewrite_projects(data, 'Backend role', SETTINGS)
        self.assertEqual(result['projects'][0]['bullets'],
                         ['Built an **HTTP server** with a **worker pool**.', data['projects'][0]['bullets'][1]])
        self.assertIn('Kept 1 original', notice)

    @patch('resume.descriptions.llm.have_key', return_value=True)
    @patch('resume.descriptions.llm.ask')
    def test_valid_rewrites_only_change_project_bullets(self, ask, _):
        ask.return_value = ({'projects': [{'bullets': ['Built an HTTP server with worker pooling.', 'Handled HTTP requests using sockets.']}]}, False)
        result, _ = rewrite_projects(self.data, 'Backend server job', SETTINGS)
        for key in ('basics', 'experience', 'education', 'coding_profiles', 'skills'):
            self.assertEqual(result[key], self.data[key])
        self.assertNotEqual(result['projects'], self.data['projects'])

    @patch('resume.descriptions.llm.have_key', return_value=False)
    def test_import_without_ai_is_reviewable_source_excerpt(self, _):
        evidence = {'name': 'server', 'description': 'An HTTP server built using sockets.', 'language': 'C++',
                    'url': 'https://github.com/me/server', 'readme': 'Handles requests using a fixed worker pool and a bounded queue.\nProvides static file serving for local network clients.'}
        draft = import_project(evidence, 'Backend job', SETTINGS)
        self.assertGreaterEqual(len(draft['project']['bullets']), 2)
        self.assertIn('Check each claim', draft['notice'])
        self.assertEqual(draft['project']['_readme'], evidence['readme'])

    @patch('resume.descriptions.llm.have_key', return_value=False)
    def test_single_paragraph_readme_provides_distinct_facts(self, _):
        evidence = {'name': 'server', 'description': 'An HTTP server built using sockets.', 'language': 'C++',
                    'url': 'https://github.com/me/server', 'readme': '# server\nAn HTTP server built using sockets. Handles requests using a fixed worker pool and a bounded queue. Provides static file serving for local network clients.'}
        draft = import_project(evidence, 'Backend job', SETTINGS)
        self.assertEqual(len(draft['project']['bullets']), 3)
        self.assertIn('worker pool', draft['project']['bullets'][1]['text'])

    @patch('resume.descriptions.llm.have_key', return_value=True)
    @patch('resume.descriptions.llm.ask')
    def test_import_rejects_invented_metrics(self, ask, _):
        ask.return_value = ({'tagline': 'A fast server', 'angles': ['systems'], 'bullets': ['Served 500000 users.', 'Reduced latency by 90%.']}, False)
        evidence = {'name': 'server', 'description': 'An HTTP server built using sockets.', 'language': 'C++',
                    'url': 'https://github.com/me/server', 'readme': 'Handles requests using a fixed worker pool and a bounded queue.\nProvides static file serving for local network clients.'}
        draft = import_project(evidence, 'Backend job', SETTINGS)
        self.assertIn('showing README excerpts', draft['notice'])
        self.assertNotIn('500000', str(draft['project']['bullets']))


if __name__ == '__main__':
    unittest.main()
