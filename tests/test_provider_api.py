import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from resume import llm
from resume.provider_api import call, request_shape, send

SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}}
KEY1 = 'test-PRIVATE-OPENAI-key'
KEY2 = 'test-PRIVATE-GEMINI-key'


def route(provider='openai', key=KEY1, ident='one'):
    return {'id': ident, 'provider': provider, 'key': key, 'model': 'test-model', 'source': 'saved'}


class ProviderTests(unittest.TestCase):
    def test_all_provider_requests_keep_key_out_of_url_and_body(self):
        for provider in ('openai', 'gemini', 'anthropic', 'openrouter'):
            with self.subTest(provider=provider):
                url, headers, body = request_shape(route(provider), 'prompt', SCHEMA, 'system', 0, 'test')
                self.assertNotIn(KEY1, url)
                self.assertNotIn(KEY1, json.dumps(body))
                self.assertTrue(any(KEY1 in value for value in headers.values()))
                self.assertTrue(url.startswith('https://'))

    @patch('resume.provider_api.send')
    def test_native_responses_are_parsed_and_validated(self, send):
        responses = {
            'openai': {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]},
            'openrouter': {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]},
            'gemini': {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': '{"ok":true}'}]}}]},
            'anthropic': {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '{"ok":true}'}]},
        }
        for provider, response in responses.items():
            send.return_value = response
            with self.subTest(provider=provider):
                self.assertEqual(call(route(provider), 'prompt', SCHEMA, 'system', 0, 'test', 5), {'ok': True})

    @patch('resume.provider_api.call')
    def test_failed_key_automatically_uses_another_provider(self, call):
        call.side_effect = [llm.LLMAuthError('Rejected ' + KEY1), {'ok': True}, {'ok': True}]
        with llm.credential_scope([route(), route('gemini', KEY2, 'two')]) as session:
            self.assertEqual(llm.ask('prompt', SCHEMA, cache=False)[0], {'ok': True})
            self.assertEqual(llm.ask('another prompt', SCHEMA, cache=False)[0], {'ok': True})
        self.assertEqual([c.args[0]['provider'] for c in call.call_args_list], ['openai', 'gemini', 'gemini'])
        self.assertNotIn(KEY1, json.dumps(session['events']))
        self.assertEqual(session['used'], [{'provider': 'gemini', 'model': 'test-model'}])

    @patch('resume.provider_api.call')
    def test_all_failed_keys_return_safe_fallback_error(self, call):
        call.side_effect = llm.LLMError('Provider echoed ' + KEY1)
        with llm.credential_scope([route()]):
            with self.assertRaises(llm.LLMError) as caught:
                llm.ask('prompt', SCHEMA, cache=False)
            self.assertFalse(llm.have_key())
        self.assertNotIn(KEY1, str(caught.exception))

    @patch('resume.provider_api.call')
    def test_cache_is_provider_scoped_and_never_stores_keys(self, call):
        call.return_value = {'ok': True}
        with tempfile.TemporaryDirectory() as directory:
            for provider, key in [('openai', KEY1), ('gemini', KEY2)]:
                with llm.credential_scope([route(provider, key)], directory):
                    self.assertFalse(llm.ask('same prompt', SCHEMA)[1])
            self.assertEqual(call.call_count, 2)
            with llm.credential_scope([route('openai', KEY2)], directory):
                self.assertTrue(llm.ask('same prompt', SCHEMA)[1])
            for file in Path(directory).glob('*.json'):
                self.assertNotIn(KEY1, file.read_text())
                self.assertNotIn(KEY2, file.read_text())

    @patch('resume.provider_api.call')
    def test_secret_reflected_in_success_response_is_discarded(self, call):
        schema = {'type': 'object', 'properties': {'text': {'type': 'string'}}}
        call.return_value = {'text': KEY1}
        with tempfile.TemporaryDirectory() as directory, llm.credential_scope([route()], directory):
            with self.assertRaises(llm.LLMError):
                llm.ask('prompt', schema)
            self.assertEqual(list(Path(directory).glob('*.json')), [])

    @patch('resume.provider_api.urllib.request.build_opener')
    def test_http_error_body_cannot_expose_keys(self, opener):
        opener.return_value.open.side_effect = urllib.error.HTTPError('https://api.openai.com', 401, 'Unauthorized', {}, io.BytesIO(KEY1.encode()))
        with self.assertRaises(llm.LLMAuthError) as caught:
            send('https://api.openai.com', {'Authorization': KEY1}, {}, 1)
        self.assertNotIn(KEY1, str(caught.exception))
        self.assertNotIn(KEY1, caught.exception.body)

    @patch('resume.provider_api.call')
    def test_invalid_structured_result_tries_next_key(self, call):
        call.side_effect = [{'ok': 'wrong type'}, {'ok': True}]
        with llm.credential_scope([route(), route('gemini', KEY2, 'two')]):
            self.assertEqual(llm.ask('prompt', SCHEMA, cache=False)[0], {'ok': True})
        self.assertEqual(call.call_count, 2)
