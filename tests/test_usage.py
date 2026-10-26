import json
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from resume.usage import UsageStore, usage_scope, record_api, record_agent, record_cache, estimate
from resume import provider_api, llm
from resume.jobs.agents import _run, AgentError


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = UsageStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def total(self):
        return self.store.summary()['total']

    def test_openai_cache_is_subset_and_ledger_survives_restart(self):
        with usage_scope(self.store, 'resume'):
            record_api('openai', 'gpt-4o-mini', {'usage': {'prompt_tokens': 1000, 'completion_tokens': 200,
                       'prompt_tokens_details': {'cached_tokens': 800}}})
            record_cache('openai', 'gpt-4o-mini')
        total = UsageStore(self.root).summary()['total']
        self.assertEqual((total['input_tokens'], total['output_tokens'], total['cached_input_tokens']), (1000, 200, 800))
        self.assertEqual((total['calls'], total['cache_hits']), (1, 1))
        self.assertAlmostEqual(total['cost_usd'], (200 * .15 + 800 * .075 + 200 * .6) / 1e6)
        self.assertEqual(total['subscription_cost_usd'], 0)

    def test_anthropic_cache_counts_and_one_hour_price(self):
        with usage_scope(self.store, 'resume'):
            record_api('anthropic', 'claude-sonnet-5-5', {'usage': dict(input_tokens=100, output_tokens=10,
                cache_read_input_tokens=200, cache_creation_input_tokens=300,
                cache_creation={'ephemeral_1h_input_tokens': 100})})
        self.assertEqual(self.total()['input_tokens'], 600)
        self.assertAlmostEqual(self.total()['cost_usd'], (100 * 2 + 10 * 10 + 200 * .1 + 200 * 2.5 + 100 * 4) / 1e6)

    def test_gemini_thoughts_and_reported_openrouter_cost(self):
        with usage_scope(self.store, 'resume'):
            record_api('gemini', 'gemini-3.8-flash', {'usageMetadata': {
                'promptTokenCount': 100, 'cachedContentTokenCount': 20, 'candidatesTokenCount': 10, 'thoughtsTokenCount': 30}})
            record_api('openrouter', 'custom/unknown', {'usage': {'prompt_tokens': 50, 'completion_tokens': 5, 'cost': .123}})
        self.assertEqual(self.total()['output_tokens'], 45)
        self.assertAlmostEqual(self.total()['cost_usd'], .123 + (80 * .75 + 20 * .075 + 40 * 3.75) / 1e6)
        self.assertEqual(self.total()['unpriced'], 0)

    def test_unknown_counts_and_prices_are_not_free_calls(self):
        with usage_scope(self.store, 'resume'):
            record_api('openai', 'unknown', {'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
            record_api('openai', 'gpt-4o-mini', {})
        self.assertEqual(self.total()['unmeasured'], 1)
        self.assertEqual(self.total()['unpriced'], 2)
        self.assertEqual(self.total()['input_tokens'], 100)

    def test_codex_turns_not_reasoning_or_duplicate_aggregate(self):
        payload = '\n'.join(json.dumps(e) for e in [
            {'type': 'item.completed', 'item': {'text': 'PRIVATE PROMPT'}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 100, 'cached_input_tokens': 80,
                'cache_write_input_tokens': 10, 'output_tokens': 5, 'reasoning_output_tokens': 3}}])
        with usage_scope(self.store, 'jobs'):
            record_agent('codex', payload)
        t = self.total()
        self.assertEqual((t['input_tokens'], t['output_tokens']), (100, 5))
        self.assertAlmostEqual(t['subscription_cost_usd'], (10 + 80 * .05 + 10 * 1.25 + 5 * 5) / 1e6)
        old_cost = t['cost_usd']
        self.store.set_reference('gpt-4o-mini')
        self.assertEqual(self.total()['cost_usd'], old_cost)
        self.assertNotIn(b'PRIVATE PROMPT', self.store.path.read_bytes())

    def test_claude_per_model_does_not_double_count_aggregate(self):
        payload = {'usage': {'input_tokens': 999}, 'total_cost_usd': 20, 'result': 'PRIVATE',
                   'modelUsage': {'claude-sonnet-5-5': dict(inputTokens=100, outputTokens=20,
                     cacheReadInputTokens=50, cacheCreationInputTokens=25, costUSD=.006)}}
        with usage_scope(self.store, 'jobs'):
            record_agent('claude', json.dumps(payload))
        self.assertEqual((self.total()['input_tokens'], self.total()['output_tokens']), (175, 20))
        self.assertEqual(self.total()['subscription_cost_usd'], .006)
        self.assertEqual(self.total()['calls'], 1)

    def test_error_cli_response_still_counts(self):
        proc = subprocess.CompletedProcess([], 1, json.dumps({'usage': {'input_tokens': 10, 'output_tokens': 2},
                                                             'total_cost_usd': .01}), 'failure')
        with usage_scope(self.store, 'jobs'), patch('resume.jobs.agents.subprocess.run', return_value=proc):
            with self.assertRaises(AgentError):
                _run(['claude'], 'PRIVATE', self.root)
        self.assertEqual(self.total()['input_tokens'], 10)
        self.assertEqual(self.total()['subscription_cost_usd'], .01)

    def test_invalid_structured_output_is_still_billed(self):
        route = {'provider': 'openai', 'model': 'gpt-4o-mini', 'key': 'PRIVATE_KEY'}
        response = {'usage': {'prompt_tokens': 100, 'completion_tokens': 10},
                    'choices': [{'message': {'content': 'not json'}, 'finish_reason': 'stop'}]}
        with usage_scope(self.store, 'resume'), patch('resume.provider_api.send', return_value=response):
            with self.assertRaises(llm.LLMError):
                provider_api.call(route, 'PRIVATE_PROMPT', {'type': 'object'}, '', 0, 'test', 5)
        self.assertEqual(self.total()['calls'], 1)
        raw = self.store.path.read_bytes()
        self.assertNotIn(b'PRIVATE_KEY', raw)
        self.assertNotIn(b'PRIVATE_PROMPT', raw)

    def test_failed_network_attempt_unmeasured_and_local_cache_hit(self):
        route = {'provider': 'openai', 'model': 'gpt-4o-mini', 'key': 'PRIVATE_KEY', 'id': 'test'}
        schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False}
        response = {'usage': {'prompt_tokens': 10, 'completion_tokens': 2},
                    'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}
        with usage_scope(self.store, 'resume'), llm.credential_scope([route], self.root / 'cache'), patch('resume.provider_api.send', return_value=response) as send:
            llm.ask('test', schema=schema)
            llm.ask('test', schema=schema)
            self.assertEqual(send.call_count, 1)
        self.assertEqual((self.total()['calls'], self.total()['cache_hits']), (1, 1))
        with usage_scope(self.store, 'resume'), patch('resume.provider_api.send', side_effect=llm.LLMError('failed')):
            with self.assertRaises(llm.LLMError):
                provider_api.call(route, 'test', schema, '', 0, 'test', 5)
        self.assertEqual(self.total()['unmeasured'], 1)

    def test_concurrent_writers_and_isolated_roots(self):
        def write(_):
            UsageStore(self.root).record('openai', 'gpt-4o-mini', 10, 2)
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(write, range(20)))
        self.assertEqual(self.total()['calls'], 20)
        self.assertEqual(UsageStore(self.root / 'other').summary()['total']['calls'], 0)

    def test_bad_reference_and_nonfinite_money_rejected(self):
        with self.assertRaises(ValueError):
            self.store.set_reference('unknown')
        self.store.record('openai', 'unknown', 100, 5, reported=float('nan'))
        self.assertEqual(self.total()['unpriced'], 1)
        self.assertIsNone(estimate('unknown', 1, 1, 0, 0, '2026-10-25'))
        self.assertIsNone(estimate('gpt-6.1-sol', 300000, 1, 0, 0, '2026-10-25'))
        self.assertEqual(estimate('gemini-3.8-flash', 1_000_000, 0, 0, 0, '2027-01-01'), 1.5)

    def test_accounting_write_error_does_not_retry_inference(self):
        with patch.object(self.store, 'connect', side_effect=__import__('sqlite3').OperationalError('locked')):
            self.store.record('openai', 'gpt-4o-mini', 10, 2)
        self.assertTrue(self.store.summary()['accounting_error'])

    def test_documented_resume_cli_entry_point_is_accounted(self):
        from types import SimpleNamespace
        from resume import cli
        profile, jd = self.root / 'profile.json', self.root / 'jd.txt'
        profile.write_text('{}')
        jd.write_text('software role')
        args = SimpleNamespace(profile=str(profile), jd=str(jd), projects=2, job_bullets=2,
                               project_bullets=2, tailorer='llm', model='gpt-4o-mini')
        def fake_tailor(*a, **kw):
            record_api('openai', 'gpt-4o-mini', {'usage': {'prompt_tokens': 50, 'completion_tokens': 10}})
            return {}, {'tailorer': 'rules', 'llm': {'reason': 'test stops before rendering'}}
        with patch.object(cli, 'ROOT', self.root), patch('resume.llm_tailor.tailor_llm', side_effect=fake_tailor):
            self.assertEqual(cli.cmd_make(args), 1)
        self.assertEqual(self.total()['input_tokens'], 50)
