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

    def test_expired_or_unknown_draft_has_useful_error(self):
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.app.get_draft('../profile.json')


if __name__ == '__main__':
    unittest.main()
