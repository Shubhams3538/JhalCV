import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from resume.credentials import CredentialStore, MASK, PROVIDERS
from resume.web import App

SECRET = 'sk-test-PRIVATE-0123456789'


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = CredentialStore(self.root, include_env=False)

    def tearDown(self):
        self.temp.cleanup()

    def add(self, provider='openai', key=SECRET):
        self.store.add({'provider': provider, 'key': key, 'label': 'Personal', 'model': 'test-model'})

    def test_public_responses_contain_dummy_only_and_file_is_private(self):
        self.add()
        public = self.store.public()
        encoded = json.dumps(public)
        self.assertNotIn(SECRET, encoded)
        self.assertNotIn(SECRET[:10], encoded)
        self.assertNotIn(SECRET[-8:], encoded)
        self.assertEqual(public['keys'][0]['mask'], MASK)
        self.assertNotIn('key', public['keys'][0])
        self.assertEqual(self.store.routes()[0]['key'], SECRET)
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)

    def test_provider_model_menu_includes_default_and_uses_it_if_omitted(self):
        for provider in self.store.public()['providers']:
            self.assertIn(provider['default_model'], [m['id'] for m in provider['models']])
            self.assertTrue(all(m['name'] for m in provider['models']))
        self.store.add({'provider': 'openai', 'key': SECRET})
        self.assertEqual(self.store.routes()[0]['model'], PROVIDERS['openai']['default_model'])

    def test_saved_key_model_changes_without_reentering_or_returning_secret(self):
        self.add()
        ident = self.store.public()['keys'][0]['id']
        self.store.set_model(ident, 'custom-model-v2')
        restarted = CredentialStore(self.root, include_env=False)
        self.assertEqual(restarted.routes()[0]['model'], 'custom-model-v2')
        self.assertEqual(restarted.routes()[0]['key'], SECRET)
        self.assertNotIn(SECRET, json.dumps(restarted.public()))
        with self.assertRaises(ValueError):
            restarted.set_model(ident, SECRET)

    @patch('resume.llm.load_env')
    def test_environment_model_choice_survives_restart(self, _):
        with patch.dict(os.environ, {'OPENAI_API_KEY': SECRET}, clear=True):
            store = CredentialStore(self.root)
            store.set_model('env:openai', 'gpt-5.4-mini')
            restarted = CredentialStore(self.root)
            self.assertEqual(restarted.routes(openai_model='legacy-default')[0]['model'], 'gpt-5.4-mini')
            self.assertNotIn(SECRET, json.dumps(restarted.public()))

    def test_reminder_opt_out_survives_restart_and_key_removal(self):
        self.assertTrue(self.store.public()['show_reminder'])
        self.store.reminder(True)
        self.add()
        self.store.remove(self.store.public()['keys'][0]['id'])
        restarted = CredentialStore(self.root, include_env=False)
        self.assertFalse(restarted.public()['show_reminder'])
        restarted.reminder(False)
        self.assertTrue(restarted.public()['show_reminder'])

    def test_new_or_environment_key_cannot_leak_through_older_metadata(self):
        future = 'test-future-private-secret'
        self.store.add({'provider': 'openai', 'key': SECRET, 'label': future, 'model': 'test-model'})
        self.add('gemini', future)
        self.assertNotIn(future, json.dumps(self.store.public()))
        self.assertNotIn(future, self.store._read()['keys'][0]['label'])
        with patch('resume.llm.load_env'), patch.dict(os.environ, {'GEMINI_API_KEY': 'test-model'}, clear=True):
            store = CredentialStore(self.root)
            self.assertNotIn('test-model', json.dumps(store.public()))

    def test_multiple_keys_priority_and_removal(self):
        self.add()
        self.add('gemini', 'gemini-PRIVATE-backup-key')
        second = self.store.public()['keys'][1]['id']
        self.store.prefer(second)
        self.assertEqual(self.store.routes()[0]['provider'], 'gemini')
        self.store.remove(second)
        self.assertEqual(len(self.store.routes()), 1)
        with self.assertRaises(ValueError):
            self.add()

    def test_secret_cannot_be_echoed_in_label_or_model(self):
        for field in ('label', 'model'):
            body = {'provider': 'openai', 'key': SECRET, 'label': 'Personal', 'model': 'test-model'}
            body[field] = SECRET
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.add(body)
        self.assertFalse(self.store.path.exists())

    @patch('resume.llm.load_env')
    def test_environment_key_is_dummy_only_and_can_be_ignored(self, _):
        with patch.dict(os.environ, {'OPENAI_API_KEY': SECRET}, clear=True):
            store = CredentialStore(self.root)
            self.assertNotIn(SECRET, json.dumps(store.public()))
            self.assertEqual(store.public()['keys'][0]['source'], 'environment')
            store.remove('env:openai')
            self.assertTrue(store.public()['show_reminder'])
            self.assertEqual(os.environ['OPENAI_API_KEY'], SECRET)

    def test_app_key_actions_and_state_never_return_secret(self):
        project_root = Path(__file__).resolve().parents[1]
        (self.root / 'profile.example.json').write_bytes((project_root / 'profile.example.json').read_bytes())
        app = App(self.root, include_env=False)
        added = app.post('/api/keys/add', {'provider': 'openai', 'key': SECRET})
        self.assertNotIn(SECRET, json.dumps(added))
        self.assertNotIn(SECRET, json.dumps(app.state()))
        self.assertTrue(app.state()['ai_available'])
        ident = added['keys'][0]['id']
        app.post('/api/keys/remove', {'id': ident})
        self.assertTrue(app.state()['credentials']['show_reminder'])
        app.post('/api/keys/reminder', {'dismissed': True})
        self.assertFalse(App(self.root, include_env=False).state()['credentials']['show_reminder'])
