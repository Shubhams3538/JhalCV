import base64
import copy
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from resume import llm, onboarding
from resume.storage import Store, validate_profile
from resume.web import App

ROOT = Path(__file__).resolve().parents[1]


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copy(ROOT / 'profile.example.json', self.root)
        self.app = App(self.root, include_env=False)
        self.profile = json.loads((ROOT / 'profile.example.json').read_text())
        self.body = {'profile': self.profile, 'preferences': {'provider': 'rules', 'years': 0,
                     'locations': 'India, Remote', 'include': 'software, backend', 'exclude': 'manager'},
                     'settings': self.app.store.settings(), 'companies': [{'name': 'Example', 'url': 'https://example.com/jobs'}],
                     'revision': ''}

    def docx(self, xml):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('word/document.xml', xml)
        return {'name': 'resume.docx', 'data': base64.b64encode(out.getvalue()).decode()}

    def test_new_install_starts_blank_with_catalog_and_existing_install_is_preserved(self):
        state = self.app.state()
        self.assertTrue(state['setup']['required'])
        self.assertEqual(state['setup']['profile']['basics']['name'], '')
        self.assertGreater(len(state['setup']['companies']), 100)
        self.assertEqual(self.app.jobs.store.sources(), [])
        self.app.store.save_profile(self.profile)
        state = self.app.state()
        self.assertFalse(state['setup']['required'])
        self.assertEqual(state['setup']['profile']['basics']['name'], self.profile['basics']['name'])
        self.assertEqual(state['setup']['companies'], [])

    def test_docx_extracts_split_runs_and_never_saves_profile(self):
        result = self.app.post('/api/setup/extract', self.docx('''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Jane Doe</w:t></w:r></w:p><w:p><w:r><w:t>jane@</w:t></w:r><w:r><w:t>example.com | Built reliable services.</w:t></w:r></w:p></w:body></w:document>'''))
        self.assertIn('jane@example.com', result['text'])
        self.assertEqual(result['profile']['basics']['name'], 'Jane Doe')
        self.assertFalse(self.app.store.profile_path.exists())

    def test_rejects_invalid_oversized_dtd_and_empty_uploads(self):
        bad = [{'name': 'resume.pdf', 'data': 'not base64'},
               {'name': '../resume.exe', 'data': base64.b64encode(b'x').decode()},
               {'name': 'resume.pdf', 'data': base64.b64encode(b'not a pdf').decode()},
               {'name': 'resume.docx', 'data': 'x' * 1400000},
               self.docx('<!DOCTYPE foo [<!ENTITY xx "data">]><foo/>'),
               self.docx('<foo/>')]
        for body in bad:
            with self.subTest(name=body['name']), self.assertRaises(ValueError):
                onboarding.extract(body)
        self.assertFalse(self.app.store.profile_path.exists())

    def test_bullet_quota_email_and_experience_are_validated_before_save(self):
        for mutation in ('bullets', 'email', 'years', 'roles', 'education'):
            body = copy.deepcopy(self.body)
            if mutation == 'bullets': body['profile']['projects'][0]['bullets'] = body['profile']['projects'][0]['bullets'][:2]
            if mutation == 'email': body['profile']['basics']['email'] = 'missing-at'
            if mutation == 'years': body['preferences']['years'] = 'nan'
            if mutation == 'roles': body['preferences']['include'] = ''
            if mutation == 'education': body['profile']['education'] = []
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.app.post('/api/setup/check', body)
        self.assertFalse(self.app.store.profile_path.exists())

    def checked(self):
        def preview(draft, edits, root, ident):
            draft['preview'] = {'revision': 'pdf-revision', 'path': self.root / 'test.pdf'}
            return {'revision': 'pdf-revision', 'url': '/api/pdf/' + ident + '/pdf-revision', 'notices': [], 'pages': 1}
        with patch('resume.web.shutil.which', return_value='/fake/tool'), patch('resume.web.build_preview', side_effect=preview):
            return self.app.post('/api/setup/check', self.body)

    def test_complete_requires_matching_preview_and_confirmation(self):
        result = self.checked()
        changed = copy.deepcopy(self.body)
        changed['profile']['basics']['name'] = 'Changed'
        for body in (dict(changed, receipt=result['receipt'], confirmed=True), dict(self.body, receipt=result['receipt'], confirmed=False)):
            with self.assertRaises(ValueError): self.app.post('/api/setup/complete', body)
        self.assertFalse(self.app.store.profile_path.exists())

    def test_completion_saves_profile_preferences_filters_and_survives_restart(self):
        result = self.checked()
        self.app.post('/api/setup/complete', dict(self.body, receipt=result['receipt'], confirmed=True))
        self.assertTrue(self.app.store.profile_path.exists())
        self.assertEqual(self.app.store.profile()['_locked']['min_project_bullets'], 3)
        source = self.app.jobs.store.sources()[0]
        self.assertEqual((source['locations'], source['include'], source['exclude']), ('India, Remote', 'software, backend', 'manager'))
        again = App(self.root, include_env=False).state()
        self.assertFalse(again['setup']['required'])
        self.assertTrue(again['setup']['completed'])
        self.assertEqual(again['setup']['preferences']['years'], 0)
        broken = self.app.store.profile()
        broken['projects'][0]['bullets'] = broken['projects'][0]['bullets'][:2]
        with self.assertRaisesRegex(ValueError, 'three confirmed'):
            validate_profile(broken)

    def test_stale_tab_and_running_refresh_block_completion(self):
        checked = self.checked()
        self.app.jobs.progress['running'] = True
        with self.assertRaisesRegex(ValueError, 'refresh'):
            self.app.post('/api/setup/complete', dict(self.body, receipt=checked['receipt'], confirmed=True))

        self.app.jobs.progress['running'] = False
        self.app.store.save_profile(self.profile)
        with self.assertRaisesRegex(ValueError, 'outside this tab'):
            self.app.post('/api/setup/complete', dict(self.body, receipt=checked['receipt'], confirmed=True))

    def test_failed_settings_write_rolls_back_profile_and_company_changes(self):
        checked = self.checked()
        with patch.object(self.app.store, 'save_settings', side_effect=OSError('disk write failed')):
            with self.assertRaises(OSError):
                self.app.post('/api/setup/complete', dict(self.body, receipt=checked['receipt'], confirmed=True))
        self.assertFalse(self.app.store.profile_path.exists())
        self.assertFalse((self.root / '.local' / 'setup.json').exists())
        self.assertEqual(self.app.jobs.store.sources(), [])

    def test_selection_preserves_saved_openai_environment_model(self):
        self.app.store.save_settings({**self.app.store.settings(), 'model': 'saved-model'})
        with patch.object(self.app.credentials, 'routes', return_value=[]) as routes:
            onboarding.selection(self.root, self.app.credentials)
        routes.assert_called_once_with('saved-model')

    def test_selected_provider_restricts_both_api_and_cli_routes(self):
        routes = [{'id': 'one', 'provider': 'gemini'}, {'id': 'two', 'provider': 'openai'}]
        with patch.object(self.app.credentials, 'routes', return_value=routes), patch('resume.llm.shutil.which', return_value='/fake'):
            with onboarding.scope(self.root, self.app.credentials, 'gemini') as session:
                self.assertEqual(session['routes'], [routes[0]])
                self.assertEqual(llm.cli_routes(), [])
            with onboarding.scope(self.root, self.app.credentials, 'codex') as session:
                self.assertEqual(session['routes'], [])
                self.assertEqual([r['provider'] for r in llm.cli_routes()], ['codex'])
            with onboarding.scope(self.root, self.app.credentials, 'rules'):
                self.assertFalse(llm.have_key())

    def test_failed_structure_keeps_profile_and_calls_selected_provider(self):
        with patch('resume.onboarding.llm.ask', side_effect=llm.LLMError('test failure')):
            with self.assertRaisesRegex(ValueError, 'saved profile is unchanged'):
                self.app.post('/api/setup/structure', {'text': 'Existing resume facts', 'provider': 'rules'})
        self.assertFalse(self.app.store.profile_path.exists())

    def test_selected_ai_requires_connection_check_before_pdf(self):
        self.body['preferences']['provider'] = 'codex'
        with self.assertRaisesRegex(ValueError, 'Test your selected AI'):
            self.checked()
        with patch('resume.llm.ask', return_value=({'ready': True}, False)):
            self.app.post('/api/setup/test-ai', {'provider': 'codex'})
        self.assertEqual(self.checked()['pages'], 1)

    def test_missing_experience_requires_explicit_fresher_confirmation(self):
        self.body['profile']['experience'] = []
        with self.assertRaisesRegex(ValueError, 'confirm you are a fresher'):
            self.checked()
        self.body['preferences']['no_experience'] = True
        result = self.checked()
        self.app.post('/api/setup/complete', dict(self.body, receipt=result['receipt'], confirmed=True))
        self.assertTrue(self.app.state()['setup']['preferences']['no_experience'])

    def test_successful_resume_autofill_also_satisfies_ai_readiness(self):
        self.body['preferences']['provider'] = 'codex'
        with patch('resume.onboarding.structure', return_value=self.profile):
            self.app.post('/api/setup/structure', {'provider': 'codex', 'text': 'Confirmed resume text'})
        self.assertEqual(self.checked()['pages'], 1)

    @unittest.skipUnless(all(shutil.which(cmd) for cmd in ('typst', 'pdfinfo', 'pdftotext')), 'PDF tools required')
    def test_real_pdf_readiness_and_roundtrip_extract(self):
        shutil.copytree(ROOT / 'templates', self.root / 'templates')
        result = self.app.post('/api/setup/check', self.body)
        self.assertEqual(result['pages'], 1)
        draft = self.app.get_draft(next(iter(self.app.setup_checks.values()))['draft'])
        pdf = draft['preview']['path']
        self.assertTrue(pdf.exists())
        extracted = onboarding.extract({'name': 'resume.pdf', 'data': base64.b64encode(pdf.read_bytes()).decode()})
        self.assertIn('ADA', extracted['text'])
        self.assertIn('LOVELACE', extracted['text'])
        self.assertIn('GitHub', [link['label'] for link in extracted['profile']['basics']['links']])


if __name__ == '__main__': unittest.main()
