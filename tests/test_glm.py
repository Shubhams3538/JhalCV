import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from resume import provider_api, zcode
from resume.credentials import CredentialStore
from resume.jobs import agents

SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}}
ROUTE = {'id': 'k1', 'provider': 'zai', 'model': 'GLM-5.3-Flash', 'key': 'zai-test-key-123456', 'label': 'Z.AI'}
BRIDGE_ROUTE = dict(ROUTE, id='zcode:zai', key='z' * 40, label='ZCode login (temporary bridge)', source='zcode')


class ZaiProviderTests(unittest.TestCase):
    def test_request_uses_coding_plan_endpoint_and_states_the_schema(self):
        url, headers, body = provider_api.request_shape(ROUTE, 'hello', SCHEMA, 'Be brief.', 0, 'test')
        self.assertEqual(url, 'https://api.z.ai/api/anthropic/v1/messages')
        self.assertEqual(headers['x-api-key'], ROUTE['key'])
        self.assertEqual(headers['Authorization'], 'Bearer ' + ROUTE['key'])
        self.assertNotIn('output_config', body)
        self.assertIn('Be brief.', body['system'])
        self.assertIn('"ok"', body['system'])
        self.assertEqual(body['model'], 'GLM-5.3-Flash')

    def test_answers_in_fences_or_with_a_preamble_are_read_and_validated(self):
        for text in ('```json\n{"ok": true}\n```', 'Here you go:\n{"ok": true}', '{"ok": true}'):
            reply = {'stop_reason': 'end_turn', 'content': [{'type': 'thinking', 'thinking': 'x'}, {'type': 'text', 'text': text}]}
            with self.subTest(text=text), patch('resume.provider_api.send', return_value=reply):
                self.assertEqual(provider_api.call(ROUTE, 'q', SCHEMA, '', 0, 'test', 10), {'ok': True})
        bad = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '{"ok": "yes"}'}]}
        with patch('resume.provider_api.send', return_value=bad), self.assertRaises(provider_api.LLMError):
            provider_api.call(ROUTE, 'q', SCHEMA, '', 0, 'test', 10)

    def test_provider_is_offered_with_both_models(self):
        with tempfile.TemporaryDirectory() as root:
            public = CredentialStore(Path(root), include_env=False).public()
        zai = next(p for p in public['providers'] if p['id'] == 'zai')
        self.assertEqual([m['id'] for m in zai['models']], ['GLM-5.3-Flash', 'GLM-5.3'])
        self.assertEqual(zai['default_model'], 'GLM-5.3-Flash')


class GlmJobAgentTests(unittest.TestCase):
    def test_flash_first_then_glm_then_subscriptions(self):
        calls = []
        def fake_call(route, prompt, schema, system, temperature, name, timeout):
            calls.append(route['model'])
            if route['model'] == 'GLM-5.3-Flash':
                raise provider_api.LLMError('Provider request failed (HTTP 500).')
            return {'ok': True}
        with patch('resume.jobs.agents._glm_route', return_value=ROUTE), \
                patch('resume.jobs.agents.shutil.which', return_value='/bin/x'), \
                patch('resume.jobs.agents.RETRY_BACKOFF', 0), \
                patch('resume.provider_api.call', side_effect=fake_call):
            runner = agents.Agents()
            self.assertEqual(runner.order, ['glm-flash', 'glm', 'codex', 'claude'])
            self.assertEqual(runner.ask('q', SCHEMA), {'ok': True})
        # A transient server error is retried three times before the next model.
        self.assertEqual(calls, ['GLM-5.3-Flash'] * agents.AGENT_ATTEMPTS + ['GLM-5.3'])
        self.assertEqual(runner.used, ['glm'])
        self.assertNotIn('glm-flash', runner.failed)  # transient errors do not disable Flash for the run

    def test_glm_is_skipped_without_a_key_and_a_rejected_key_disables_it(self):
        with patch('resume.jobs.agents._glm_route', return_value=None), \
                patch('resume.jobs.agents.shutil.which', return_value='/bin/x'):
            self.assertEqual(agents.Agents().order, ['codex', 'claude'])
        with patch('resume.jobs.agents._glm_route', return_value=ROUTE), \
                patch('resume.jobs.agents.shutil.which', return_value=None), \
                patch('resume.provider_api.call', side_effect=provider_api.LLMAuthError('Provider rejected this API key.')):
            runner = agents.Agents()
            with self.assertRaises(agents.AgentError):
                runner.ask('q', SCHEMA)
        self.assertEqual(set(runner.failed), {'glm-flash', 'glm'})


class ZcodeBridgeConsentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.consent = Path(self.tmp.name) / 'zcode_glm.json'
        patcher = patch.object(zcode, 'CONSENT_PATH', self.consent)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, value):
        self.consent.write_text(value, encoding='utf-8')

    def test_route_only_with_explicit_enabled_consent(self):
        with patch.object(zcode, 'access_token', return_value=BRIDGE_ROUTE['key']) as resolve:
            self.assertIsNone(zcode.route())
            resolve.assert_not_called()  # no credential work without consent
            self.write(json.dumps({'enabled': True}))
            route = zcode.route()
            self.assertEqual(route['provider'], 'zai')
            self.assertEqual(route['id'], 'zcode:zai')
            self.assertEqual(route['source'], 'zcode')
            self.assertEqual(route['key'], BRIDGE_ROUTE['key'])
            self.write(json.dumps({'enabled': False}))
            self.assertIsNone(zcode.route())
        with patch.object(zcode, 'access_token', return_value=None), \
                patch.object(zcode, 'allowed', return_value=True):
            self.assertIsNone(zcode.route())

    def test_broken_or_refusing_consent_file_disables_the_bridge(self):
        for content in ('', 'not json', '{"enabled": "yes"}', '{"consent_date": "2026-10-26"}', '{}'):
            with self.subTest(content=content):
                self.write(content)
                self.assertIsNone(zcode.route())


class ZcodeBridgeTokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(zcode, 'CONSENT_PATH', Path(self.tmp.name) / 'zcode_glm.json')
        patcher.start()
        self.addCleanup(patcher.stop)
        zcode._cache = (0.0, None)
        self.addCleanup(setattr, zcode, '_cache', (0.0, None))

    def run_helper(self, stdout):
        return Mock(stdout=stdout)

    def test_token_is_decrypted_once_per_cache_window(self):
        with patch.object(zcode, '_node', return_value='/bin/node'), \
                patch('resume.zcode.subprocess.run', return_value=self.run_helper('t' * 40)) as run:
            self.assertEqual(zcode.access_token(), 't' * 40)
            self.assertEqual(zcode.access_token(), 't' * 40)
            self.assertEqual(run.call_count, 1)
            zcode._cache = (0.0, None)
            self.assertEqual(zcode.access_token(), 't' * 40)
            self.assertEqual(run.call_count, 2)

    def test_unusable_helper_output_yields_no_token(self):
        for stdout in ('', 'short', 'bad\nline\n', 'x' * 5000, 'tok\0secret'):
            with self.subTest(), patch.object(zcode, '_node', return_value='/bin/node'), \
                    patch('resume.zcode.subprocess.run', return_value=self.run_helper(stdout)):
                zcode._cache = (0.0, None)
                self.assertIsNone(zcode.access_token())

    def test_missing_node_runtime_yields_no_token(self):
        with patch.object(zcode, '_node', return_value=None):
            self.assertIsNone(zcode.access_token())

    def test_helper_failure_and_timeout_yield_no_token(self):
        with patch.object(zcode, '_node', return_value='/bin/node'), \
                patch('resume.zcode.subprocess.run', side_effect=OSError('no exec')):
            self.assertIsNone(zcode.access_token())
        with patch.object(zcode, '_node', return_value='/bin/node'), \
                patch('resume.zcode.subprocess.run', side_effect=subprocess.TimeoutExpired(cmd='node', timeout=15)):
            self.assertIsNone(zcode.access_token())


class ZcodeBridgeAgentTests(unittest.TestCase):
    def test_saved_key_wins_over_the_bridge(self):
        with patch.object(CredentialStore, 'routes', return_value=[ROUTE]), \
                patch('resume.zcode.route') as bridge:
            self.assertEqual(agents._glm_route(), ROUTE)
            bridge.assert_not_called()

    def test_bridge_is_used_when_no_key_is_saved(self):
        with patch.object(CredentialStore, 'routes', return_value=[]), \
                patch('resume.zcode.route', return_value=BRIDGE_ROUTE):
            self.assertEqual(agents._glm_route(), BRIDGE_ROUTE)

    def test_unreadable_saved_keys_fall_back_to_the_bridge(self):
        with patch.object(CredentialStore, 'routes', side_effect=ValueError('broken')), \
                patch('resume.zcode.route', return_value=BRIDGE_ROUTE):
            self.assertEqual(agents._glm_route(), BRIDGE_ROUTE)

    def test_bridge_route_runs_a_job_agent_call(self):
        calls = []
        def fake_call(route, prompt, schema, system, temperature, name, timeout):
            calls.append(route['model'])
            return {'ok': True}
        with patch.object(CredentialStore, 'routes', return_value=[]), \
                patch('resume.zcode.route', return_value=BRIDGE_ROUTE), \
                patch('resume.jobs.agents.shutil.which', return_value='/bin/x'), \
                patch('resume.provider_api.call', side_effect=fake_call):
            runner = agents.Agents()
            self.assertEqual(runner.order, ['glm-flash', 'glm', 'codex', 'claude'])
            self.assertEqual(runner.ask('q', SCHEMA), {'ok': True})
        self.assertEqual(calls, ['GLM-5.3-Flash'])
        self.assertEqual(runner.used, ['glm-flash'])


if __name__ == '__main__':
    unittest.main()
