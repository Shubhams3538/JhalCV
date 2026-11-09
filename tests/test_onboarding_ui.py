"""Run the first-run wizard in an owned headless Chrome against local fixtures."""
import base64
import json
import shutil
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from resume.jobs.browser_pool import ChromeWorker, DevTools
from resume.jobs.fetch import browser
from resume.web import App, handler

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(browser() and all(shutil.which(c) for c in ('typst', 'pdfinfo', 'pdftotext')), 'Chrome and PDF tools required')
class OnboardingBrowserTests(unittest.TestCase):
    def test_full_setup_error_feedback_pdf_completion_and_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy(ROOT / 'profile.example.json', root)
            shutil.copytree(ROOT / 'templates', root / 'templates')
            app = App(root, include_env=False)
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(app))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            worker = ChromeWorker(browser())
            client = None
            try:
                worker.start(time.monotonic() + 15)
                client = DevTools(worker.port, worker.path, time.monotonic() + 45)
                target = client.call('Target.createTarget', {'url': f'http://127.0.0.1:{server.server_port}'})['targetId']
                session = client.call('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
                def js(expression):
                    result = client.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True, 'awaitPromise': True}, session)
                    if 'exceptionDetails' in result:
                        self.fail(str(result['exceptionDetails']))
                    return result.get('result', {}).get('value')
                def wait(expression):
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline:
                        if js(expression): return
                        threading.Event().wait(.04)
                    self.fail('UI did not reach expected state: ' + expression)
                wait("!!document.querySelector('#setup-dialog')?.open && document.querySelector('#setup-content')?.children.length > 0")
                self.assertEqual(js("document.querySelectorAll('dialog[open]').length"), 1)
                self.assertEqual(js('setupDraft.profile.basics.name'), '')
                js("setupStep=2; renderSetup(); [...document.querySelectorAll('#setup-content button')].find(b=>b.textContent==='Add Experience').click()")
                wait('!busy')
                self.assertEqual(js('setupDraft.profile.experience.length'), 1)
                js('setupStep=4; renderSetup(); document.querySelector("#setup-content button").click()')
                wait('!busy')
                self.assertIn('name', js('document.querySelector("#setup-feedback").textContent').lower())
                self.assertFalse(app.store.profile_path.exists())
                profile = json.loads((ROOT / 'profile.example.json').read_text())
                js('setupDraft.profile=' + json.dumps(profile) + ";setupDraft.preferences.include='software, backend';setupDraft.preferences.provider='rules';setupDraft.companies=[];renderSetup();document.querySelector('#setup-content button').click()")
                wait('!busy && !!setupReceipt')
                self.assertEqual(js("document.querySelector('#setup-preview iframe').title"), 'Setup one-page resume preview')
                js("document.querySelector('#setup-confirm').checked=true;[...document.querySelectorAll('#setup-content button')].find(b=>b.textContent.startsWith('Finish setup')).click()")
                wait('!!document.querySelector("#reopen-setup") && typeof state !== "undefined" && state.setup.completed && !document.querySelector("#setup-dialog").open')
                self.assertEqual(app.store.profile()['basics']['name'], 'Ada Lovelace')
                # Save an edit in the current session, then reopen. The wizard
                # must refresh both saved profile and company data.
                js("(async()=>{profile.basics.name='Saved Fresh Name'; await storeProfile(profile); await openSetup();})()")
                self.assertEqual(js('setupDraft.profile.basics.name'), 'Saved Fresh Name')
                self.assertEqual(js('setupDraft.companies.length'), 0)
                self.assertGreater(js("document.querySelector('#setup-content').getBoundingClientRect().height"), 100)
                js('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                # Screenshot is available for local visual inspection when asked.
                shot = client.call('Page.captureScreenshot', {'format': 'png'}, session)['data']
                Path('/tmp/jhalcv-onboarding.png').write_bytes(base64.b64decode(shot))
                # Choosing AI now fills the form on Continue; it no longer
                # depends on discovering a separate extraction button.
                original_post, structured_calls = app.post, []
                def fake_structure(path, body):
                    if path == '/api/setup/structure':
                        structured_calls.append(body)
                        return {'profile': profile, 'review': {'experience_years': 1.5, 'missing': []}}
                    return original_post(path, body)
                with patch.object(app, 'post', side_effect=fake_structure):
                    js("setupDraft.text='Resume text for test';setupDraft.preferences.provider='codex';setupDraft.profileTouched=false;setupDraft.structured=false;setupStep=1;renderSetup();continueSetup()")
                    self.assertEqual(js('setupStep'), 2)
                    self.assertEqual(len(structured_calls), 1)
                    self.assertEqual(js('setupDraft.preferences.years'), 1.5)
                    self.assertEqual(js("[...document.querySelectorAll('#setup-content label')].filter(n=>n.textContent.startsWith('GitHub profile URL')).length"), 1)
                    self.assertEqual(js("document.querySelector('#setup-missing').textContent.includes('LinkedIn was not found')"), False)
                    js("(()=>{const label=[...document.querySelectorAll('#setup-content label')].find(n=>n.textContent.startsWith('GitHub profile URL')); const input=document.getElementById(label.htmlFor); input.value='https://github.com/edited-user';input.dispatchEvent(new Event('input',{bubbles:true}));})()")
                    js('setupDraft.structured=false;setupStep=1;renderSetup();continueSetup()')
                    self.assertEqual(len(structured_calls), 1)
                    self.assertEqual(js("socialLink(setupDraft.profile,'github.com').url"), 'https://github.com/edited-user')
            finally:
                if client: client.close()
                worker.close()
                server.shutdown(); server.server_close(); thread.join(2)
