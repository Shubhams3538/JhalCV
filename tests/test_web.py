import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from resume.web import App, handler

ROOT = Path(__file__).resolve().parents[1]


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'profile.example.json').write_bytes((ROOT / 'profile.example.json').read_bytes())
        self.app = App(self.root)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, data=None, headers=None):
        req = urllib.request.Request(self.url + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers=headers or {})
        with urllib.request.urlopen(req) as response:
            return json.load(response)

    def test_state_and_authenticated_save(self):
        state = self.request('/api/state')
        self.assertTrue(state['example'])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/profile', state['profile'], {'Content-Type': 'application/json'})
        self.assertEqual(error.exception.code, 403)
        self.request('/api/profile', state['profile'], {'Content-Type': 'application/json', 'X-App-Token': state['token']})
        self.assertFalse(self.request('/api/state')['example'])

    def test_stale_tab_cannot_overwrite_newer_profile(self):
        state = self.request('/api/state')
        headers = {'Content-Type': 'application/json', 'X-App-Token': state['token']}
        first = self.request('/api/profile', {'profile': state['profile'], 'revision': state['profile_revision']}, headers)
        changed = dict(state['profile'], basics=dict(state['profile']['basics'], name='Changed Elsewhere'))
        self.request('/api/profile', {'profile': changed, 'revision': first['revision']}, headers)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/profile', {'profile': state['profile'], 'revision': first['revision']}, headers)
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.app.store.profile()['basics']['name'], 'Changed Elsewhere')

    def test_jobs_endpoints(self):
        headers = {'Content-Type': 'application/json', 'X-App-Token': self.app.token}
        state = self.request('/api/jobs/sources/add', {'name': 'Acme', 'url': 'https://job-boards.greenhouse.io/acme'}, headers)
        self.assertEqual([(s['name'], s['label']) for s in state['sources']], [('Acme', 'Greenhouse')])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/jobs/sources/add', {'name': 'Local', 'url': 'http://127.0.0.1:8765/'}, headers)
        self.assertEqual(error.exception.code, 400)
        store = self.app.jobs.store
        store.save_listing(state['sources'][0]['id'], [{'external_id': '1', 'title': 'Engineer', 'location': 'Remote',
            'url': 'https://x.test/1', 'posted': '', 'requirements': '2 years', 'description': 'Build APIs.'}])
        ident = self.request('/api/jobs')['jobs'][0]['id']
        self.assertEqual(self.request('/api/jobs/detail', {'id': ident}, headers)['description'], 'Build APIs.')
        state = self.request('/api/jobs/status', {'id': ident, 'status': 'applied'}, headers)
        self.assertEqual(state['jobs'][0]['status'], 'applied')
        with self.assertRaises(urllib.error.HTTPError):
            self.request('/api/jobs/status', {'id': ident, 'status': 'deleted'}, headers)

    def test_foreign_origin_and_host_are_rejected(self):
        headers = {'Content-Type': 'application/json', 'X-App-Token': self.app.token, 'Origin': 'https://evil.test'}
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/settings', {}, headers)
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/state', headers={'Host': 'evil.test'})
        self.assertEqual(error.exception.code, 403)

    @patch('resume.descriptions.llm.have_key', return_value=False)
    def test_rules_draft_keeps_frozen_data_and_honors_project_selection(self, _):
        profile = self.app.store.profile()
        self.app.store.save_profile(profile)
        self.app.store.save_settings({'output_dir': 'build', 'filename': 'shubham_Resume', 'tailorer': 'rules', 'model': 'test'})
        draft = self.app.post('/api/tailor', {'jd': 'Backend engineer building Python REST APIs and reliable database services.', 'projects': [profile['projects'][0]['id']]})
        self.assertEqual(len(draft['projects']), 1)
        stored = self.app.drafts[draft['id']]['data']
        for key in ('basics', 'education', 'coding_profiles'):
            self.assertEqual(stored[key], profile[key])
        self.assertEqual(stored['experience'][0]['bullets'], [b['text'] for b in profile['experience'][0]['bullets']])

    @patch('resume.descriptions.llm.have_key', return_value=False)
    def test_drafts_feed_insights_and_missing_skills_can_be_added(self, _):
        profile = self.app.store.profile()
        self.app.store.save_profile(profile)
        self.app.store.save_settings({'output_dir': 'build', 'filename': 'shubham_Resume', 'tailorer': 'rules', 'model': 'test'})
        jd = 'Backend engineer with Python, Kubernetes and Kafka experience building reliable services.'
        self.app.post('/api/tailor', {'jd': jd, 'projects': [profile['projects'][0]['id']]})
        self.app.post('/api/tailor', {'jd': jd, 'projects': [profile['projects'][0]['id']]})
        insights = self.request('/api/insights')
        self.assertEqual(insights['jobs'], 1)
        missing = {m['skill'] for m in insights['missing']}
        self.assertIn('Kafka', missing)
        group = next(k for k, v in profile['skills'].items() if not k.startswith('_') and isinstance(v, list))
        headers = {'Content-Type': 'application/json', 'X-App-Token': self.app.token}
        result = self.request('/api/insights/add', {'skill': 'Kafka', 'group': group}, headers)
        self.assertIn('Kafka', result['profile']['skills'][group])
        self.assertNotIn('Kafka', {m['skill'] for m in result['insights']['missing']})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/insights/add', {'skill': 'kafka', 'group': group}, headers)
        self.assertEqual(error.exception.code, 400)

    def test_remove_project_persists_after_reload_and_restart(self):
        profile = self.app.store.profile()
        self.app.store.save_profile(profile)
        ident = profile['projects'][0]['id']
        headers = {'Content-Type': 'application/json', 'X-App-Token': self.app.token}
        self.assertTrue(self.request('/api/projects/remove', {'id': ident}, headers)['removed'])
        reloaded = self.request('/api/state')['profile']
        self.assertEqual(reloaded['projects'], [])
        self.assertEqual(App(self.root).store.profile()['projects'], [])
        for key in ('basics', 'experience', 'education', 'skills', 'coding_profiles'):
            self.assertEqual(reloaded[key], profile[key])
        self.assertEqual(self.request('/api/projects/remove', {'id': ident}, headers), {'removed': False})

    def test_remove_matches_the_exact_saved_id(self):
        profile = self.app.store.profile()
        profile['projects'][0]['id'] = ' project-one '
        self.app.store.save_profile(profile)
        self.assertTrue(self.app.post('/api/projects/remove', {'id': ' project-one '})['removed'])
        self.assertEqual(self.app.store.profile()['projects'], [])

    def test_remove_unsaved_project_does_not_save_sample_profile(self):
        self.assertEqual(self.app.post('/api/projects/remove', {'id': 'unsaved-project'}), {'removed': False})
        self.assertFalse(self.app.store.profile_path.exists())

    def test_remove_failure_preserves_saved_project(self):
        self.app.store.save_profile(self.app.store.profile())
        before = self.app.store.profile_path.read_bytes()
        ident = self.app.store.profile()['projects'][0]['id']
        with patch.object(self.app.store, 'save_profile', side_effect=OSError('Disk full')):
            with self.assertRaises(OSError):
                self.app.post('/api/projects/remove', {'id': ident})
        self.assertEqual(self.app.store.profile_path.read_bytes(), before)

    @patch('resume.web.choose_folder')
    def test_folder_choice_does_not_persist_until_settings_are_saved(self, picker):
        picker.return_value = str(self.root / 'chosen')
        before = self.app.store.settings()
        chosen = self.app.post('/api/folder', {'initial': 'new folder'})
        self.assertEqual(chosen['path'], str(self.root / 'chosen'))
        self.assertEqual(picker.call_args.args[0], self.root / 'new folder')
        self.assertEqual(self.app.store.settings(), before)
        picker.return_value = None
        self.assertEqual(self.app.post('/api/folder', {}), {'path': None})
        self.assertEqual(self.app.store.settings(), before)

    def test_expired_or_unknown_draft_has_useful_error(self):
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.app.get_draft('../profile.json')


if __name__ == '__main__':
    unittest.main()
