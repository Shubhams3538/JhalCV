import json
import copy
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from contextlib import nullcontext

from resume.web import App, handler
from resume.timing import stage

ROOT = Path(__file__).resolve().parents[1]


class WebTests(unittest.TestCase):
    def test_slow_tailoring_publishes_timings_without_blocking_state_or_duplicate_work(self):
        self.app.store.save_profile(self.app.store.profile())
        self.app.store.save_settings({**self.app.store.settings(), 'tailorer': 'rules'})
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        original = self.app.tailor_profile
        def blocked(profile, jd, settings):
            with stage('test_wait', 'Waiting for test inference'):
                entered.set()
                release.wait(3)
            return original(profile, jd, settings)
        headers = {'Content-Type': 'application/json', 'X-App-Token': self.app.token}
        body = {'jd': 'Backend engineer building reliable Python REST APIs and database services.'}
        def request():
            try:
                results.append(self.request('/api/tailor', body, headers))
            except Exception as error:
                errors.append(error)
        with patch.object(self.app, 'tailor_profile', side_effect=blocked):
            thread = threading.Thread(target=request)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                progress = self.request('/api/tailor/progress')
                self.assertTrue(progress['running'])
                self.assertEqual(progress['stage'], 'Waiting for test inference')
                self.assertFalse(self.request('/api/state')['example'])
                with self.assertRaises(urllib.error.HTTPError) as duplicate:
                    self.request('/api/tailor', body, headers)
                self.assertEqual(duplicate.exception.code, 400)
            finally:
                release.set()
                thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        timing = results[0]['timings']
        self.assertFalse(timing['running'])
        self.assertGreaterEqual(timing['elapsed_seconds'], 0)
        self.assertIn('test_wait', [entry['id'] for entry in timing['stages']])
        self.assertNotIn(body['jd'], json.dumps(timing))
        self.assertEqual(self.app.drafts[results[0]['id']]['report']['timings'], timing)

    def test_tailor_automatically_considers_full_library_without_project_selection(self):
        profile, _, headers = self.swap_fixture()
        for mode in ('rules', 'auto'):
            self.app.store.save_settings({**self.app.store.settings(), 'tailorer': mode})
            with self.subTest(mode=mode), \
                    patch.object(self.app, 'tailor_profile', wraps=self.app.tailor_profile) as tailoring, \
                    patch('resume.llm.have_key', return_value=False):
                result = self.request('/api/tailor', {
                    'jd': 'Backend engineer building Python and Rust REST APIs and reliable database services.'}, headers)
                self.assertEqual([p['id'] for p in tailoring.call_args.args[0]['projects']],
                                 [p['id'] for p in profile['projects']])
                self.assertEqual(len(result['projects']), 2)
                self.assertEqual(len(result['library']), 3)
                self.assertEqual(self.app.store.profile(), profile)

    def test_automatic_tailoring_without_projects_has_actionable_error(self):
        profile = self.app.store.profile()
        profile['projects'] = []
        self.app.store.save_profile(profile)
        with self.assertRaisesRegex(ValueError, 'Add a project'):
            self.app.post('/api/tailor', {'jd': 'Backend engineer building reliable Python services and REST APIs.'})

    def swap_fixture(self):
        profile = self.app.store.profile()
        first = profile['projects'][0]
        profile['projects'] = [dict(copy.deepcopy(first), id=f'project-{i}', name=f'Project {i}') for i in range(3)]
        profile['projects'][2]['stack'].append('Rust')
        self.app.store.save_profile(profile)
        self.app.store.save_settings({**self.app.store.settings(), 'tailorer': 'rules'})
        headers = {'Content-Type': 'application/json', 'X-App-Token': self.app.token}
        draft = self.request('/api/tailor', {
            'jd': 'Backend engineer building Python and Rust REST APIs and reliable database services.',
            'projects': ['project-0', 'project-1']}, headers)
        return profile, draft, headers

    def test_swap_replaces_one_project_preserves_edits_and_requires_new_preview(self):
        profile, original, headers = self.swap_fixture()
        old = copy.deepcopy(self.app.drafts[original['id']])
        self.app.drafts[original['id']]['preview'] = {'revision': 'old-preview'}
        edits = [{'bullets': p['bullets'][:]} for p in original['projects']]
        edits[1]['bullets'][0] = 'Built an API.'
        # Swapping must remain local even when the normal tailoring mode is AI.
        self.app.store.save_settings({**self.app.store.settings(), 'tailorer': 'auto'})
        with patch('resume.web.App.tailor_profile', side_effect=AssertionError('No tailoring on swap')), \
                patch('resume.web.rewrite_projects', side_effect=AssertionError('No rewriting on swap')), \
                patch('resume.web.llm.credential_scope', side_effect=AssertionError('No credentials on swap')), \
                patch('resume.llm.ask', side_effect=AssertionError('No AI on swap')):
            swapped = self.request('/api/drafts/swap', {
                'id': original['id'], 'project': original['projects'][0]['id'],
                'replacement': 'project-2', 'projects': edits}, headers)
        self.assertNotEqual(swapped['id'], original['id'])
        self.assertEqual([p['id'] for p in swapped['projects']], ['project-2', original['projects'][1]['id']])
        self.assertEqual(swapped['projects'][1]['bullets'], edits[1]['bullets'])
        self.assertIn('rust', original['gaps'])
        self.assertEqual(swapped['gaps'], original['gaps'])
        count = len(original['projects'][0]['bullets'])
        self.assertEqual(swapped['projects'][0]['bullets'], [b['text'] for b in profile['projects'][2]['bullets'][:count]])
        new = self.app.drafts[swapped['id']]
        for key in ('basics', 'experience', 'education', 'coding_profiles', 'skills'):
            self.assertEqual(new['source'][key], old['source'][key])
        self.assertEqual(new['jd'], old['jd'])
        self.assertNotIn('preview', new)
        self.assertEqual(self.app.drafts[original['id']]['data'], old['data'])
        self.assertEqual(self.app.store.profile(), profile)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/export', {'id': swapped['id'], 'revision': 'old-preview'}, headers)
        self.assertEqual(error.exception.code, 400)
        restored = self.request('/api/drafts/swap', {
            'id': swapped['id'], 'project': 'project-2', 'replacement': original['projects'][0]['id'],
            'projects': [{'bullets': p['bullets']} for p in swapped['projects']]}, headers)
        self.assertEqual([p['id'] for p in restored['projects']], [p['id'] for p in original['projects']])
        self.assertIn('rust', restored['gaps'])

    def test_invalid_swaps_leave_draft_unchanged(self):
        profile, draft, headers = self.swap_fixture()
        before = copy.deepcopy(self.app.drafts[draft['id']])
        body = {'id': draft['id'], 'project': draft['projects'][0]['id'], 'replacement': 'project-2',
                'projects': [{'bullets': p['bullets'][:]} for p in draft['projects']]}
        long_edits = copy.deepcopy(body['projects'])
        long_edits[1]['bullets'][0] += ' too long ' * 100
        for changes in ({'replacement': 'project-1'}, {'replacement': 'unknown'},
                        {'project': 'missing'}, {'projects': []}, {'projects': long_edits}, {'id': 'expired'}):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.request('/api/drafts/swap', {**body, **changes}, headers)
                self.assertEqual(error.exception.code, 400)
                self.assertEqual(self.app.drafts[draft['id']], before)
        self.assertEqual(self.app.store.profile(), profile)

    def test_tailored_draft_survives_cli_failover_during_job_search(self):
        profile = self.app.store.profile()
        self.app.store.save_profile(profile)
        self.app.store.save_settings({**self.app.store.settings(), 'tailorer': 'rules'})
        self.app.jobs.progress['running'] = True
        headers = {'Content-Type': 'application/json', 'X-App-Token': self.app.token}
        for provider, name in [('claude', 'Claude Code'), ('codex', 'Codex'), ('openai', 'OpenAI')]:
            with self.subTest(provider=provider):
                session = {'events': [{'provider': 'openai', 'status': 'failed'}],
                           'used': [{'provider': provider, 'model': 'test'}], 'last_call_failed': False}
                with patch('resume.web.llm.credential_scope', return_value=nullcontext(session)), \
                        patch('resume.web.rewrite_projects', side_effect=lambda data, jd, settings: (data, '')):
                    result = self.request('/api/tailor', {
                        'jd': 'Backend engineer building Python REST APIs and reliable database services.',
                        'projects': [profile['projects'][0]['id']]}, headers)
                self.assertIn(name, result['notice'])
                self.assertTrue(result['projects'])
                self.assertEqual(self.app.drafts[result['id']]['report']['providers_used'], session['used'])
        self.assertEqual(self.app.store.profile(), profile)
        self.assertTrue(self.app.jobs.progress['running'])

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

    def test_progress_endpoint_returns_run_state_without_the_collection(self):
        self.app.jobs.progress.update(running=True, message='5/9 companies completed')
        light = self.request('/api/jobs/progress')
        self.assertEqual(light['progress']['message'], '5/9 companies completed')
        self.assertNotIn('jobs', light)
        self.assertNotIn('sources', light)

    def test_usage_persistence_and_authenticated_reference_setting(self):
        usage = self.request('/api/usage')
        self.assertEqual(usage['total']['calls'], 0)
        self.app.usage.record('openai', 'gpt-4o-mini', 100, 20)
        self.assertEqual(self.request('/api/usage')['total']['input_tokens'], 100)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/usage/settings', {'model': 'gpt-4o-mini'}, {'Content-Type': 'application/json'})
        self.assertEqual(error.exception.code, 403)
        headers = {'Content-Type': 'application/json', 'X-App-Token': self.app.token}
        saved = self.request('/api/usage/settings', {'model': 'gpt-4o-mini'}, headers)
        self.assertEqual(saved['codex_reference'], 'gpt-4o-mini')
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/usage/settings', {'model': 'unknown'}, headers)
        self.assertEqual(error.exception.code, 400)
        self.assertNotIn('key', saved)

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

    def test_edit_job_source_link_preserves_saved_jobs(self):
        source = self.app.jobs.add_source('Acme', 'https://acme.test/careers')
        headers = {'Content-Type': 'application/json', 'X-App-Token': self.app.token}
        result = self.request('/api/jobs/sources/url', {'id': source['id'], 'url': 'https://job-boards.greenhouse.io/acme'}, headers)
        saved = next(src for src in result['sources'] if src['id'] == source['id'])
        self.assertEqual(saved['kind'], 'greenhouse')
        self.assertEqual(saved['original_url'], 'https://acme.test/careers')
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/jobs/sources/url', {'id': source['id'], 'url': 'http://127.0.0.1/'}, headers)
        self.assertEqual(error.exception.code, 400)

    def test_review_saved_jobs_endpoint_authentication_and_mode(self):
        body = {}
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/jobs/review', body, {'Content-Type': 'application/json'})
        self.assertEqual(error.exception.code, 403)
        headers = {'Content-Type': 'application/json', 'X-App-Token': self.app.token}
        with patch.object(self.app.jobs, 'start', return_value=True) as start:
            result = self.request('/api/jobs/review', body, headers)
        start.assert_called_once_with(self.app.store.profile(), recheck_only=True)
        self.assertIn('unreviewed_count', result)
        with patch.object(self.app.jobs, 'start', return_value=False):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/jobs/review', body, headers)
        self.assertEqual(error.exception.code, 400)

    def test_benchmark_report_endpoint_and_invalid_run(self):
        self.assertIsNone(self.request('/api/benchmarks')['report'])
        with self.app.jobs.benchmarks.run('review'):
            pass
        report = self.request('/api/benchmarks')['report']
        self.assertEqual(report['mode'], 'review')
        self.assertEqual(self.request('/api/benchmarks?run='+report['id'])['report']['id'], report['id'])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/benchmarks?run=../credentials')
        self.assertEqual(error.exception.code, 400)

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

    def test_manual_job_eligibility_endpoint(self):
        headers = {'Content-Type': 'application/json', 'X-App-Token': self.app.token}
        source = self.app.jobs.store.add_source('Manual', 'https://jobs.lever.co/manual', 'lever')
        ident = self.app.jobs.store.save_listing(source['id'], [dict(external_id='1', title='Engineer', posted='', location='India', url='https://example.com/jobs/1', requirements='', description='')])[0]
        result = self.request('/api/jobs/eligibility', {'id': ident, 'eligible': 'no'}, headers)
        self.assertEqual(result['jobs'][0]['category'], 'no')
        result = self.request('/api/jobs/eligibility', {'id': ident, 'eligible': None}, headers)
        self.assertEqual(result['jobs'][0]['category'], 'review')
        for body in ({'id': ident}, {'id': ident, 'eligible': 'yes'}, {'id': ident + 1, 'eligible': 'no'}):
            with self.assertRaises(urllib.error.HTTPError):
                self.request('/api/jobs/eligibility', body, headers)

    def test_company_csv_preview_and_import_endpoints(self):
        headers = {'Content-Type': 'application/json', 'X-App-Token': self.app.token}
        preview = self.request('/api/jobs/sources/preview',
                               {'csv': 'company,url\nExample,https://jobs.lever.co/example\n'}, headers)
        self.assertEqual(preview['ready'], 1)
        self.assertEqual(self.app.jobs.store.sources(), [])
        imported = self.request('/api/jobs/sources/import', {'rows': preview['rows'], 'locations': 'India'}, headers)
        self.assertEqual(imported['imported'], 1)
        self.assertEqual(imported['state']['sources'][0]['locations'], 'India')
        self.assertFalse(imported['state']['progress']['running'])
        repeated = self.request('/api/jobs/sources/import', {'rows': preview['rows']}, headers)
        self.assertEqual((repeated['imported'], repeated['skipped']), (0, 1))

    def test_saved_source_location_filter_updates_without_removing_applied_jobs(self):
        headers = {'Content-Type': 'application/json', 'X-App-Token': self.app.token}
        state = self.request('/api/jobs/sources/add', {'name': 'Acme', 'url': 'https://job-boards.greenhouse.io/acme',
                                                       'locations': 'India'}, headers)
        source = state['sources'][0]
        self.assertEqual(source['locations'], 'India')
        rows = [dict(external_id=str(i), title='Engineer', location=place,
                     url=f'https://x.test/{i}', posted='', requirements='', description='')
                for i, place in [(1, 'Gurgaon, Haryana, India'), (2, 'Singapore')]]
        ids = self.app.jobs.store.save_listing(source['id'], rows)
        self.assertEqual([j['id'] for j in self.request('/api/jobs')['jobs']], [ids[0]])
        self.request('/api/jobs/status', {'id': ids[1], 'status': 'applied'}, headers)
        state = self.request('/api/jobs/sources/locations', {'id': source['id'], 'locations': 'Singapore'}, headers)
        self.assertEqual(state['sources'][0]['locations'], 'Singapore')
        self.assertEqual({j['id'] for j in state['jobs']}, {ids[1]})

    def test_saved_job_delete_endpoint_prevents_recollection(self):
        headers = {'Content-Type': 'application/json', 'X-App-Token': self.app.token}
        store = self.app.jobs.store
        source = store.add_source('Acme', 'https://x.test/jobs', 'greenhouse')
        item = dict(external_id='1', title='Engineer', location='India', url='https://x.test/job/1',
                    posted='', requirements='', description='Build software')
        ident = store.save_listing(source['id'], [item])[0]
        state = self.request('/api/jobs/delete', {'id': ident}, headers)
        self.assertEqual(state['jobs'], [])
        self.assertEqual(store.save_listing(source['id'], [item]), [])
        with self.assertRaises(urllib.error.HTTPError):
            self.request('/api/jobs/delete', {'id': ident}, headers)
        with self.assertRaises(urllib.error.HTTPError):
            self.request('/api/jobs/detail', {'id': ident}, headers)

    def test_bulk_delete_keeps_other_categories(self):
        headers = {'Content-Type': 'application/json', 'X-App-Token': self.app.token}
        store = self.app.jobs.store
        source = store.add_source('Acme', 'https://x.test/jobs', 'greenhouse')
        items = [dict(external_id=str(i), title='Engineer', location='India', url=f'https://x.test/job/{i}',
                      posted='', requirements='', description='Build software') for i in (1,2)]
        ids = store.save_listing(source['id'], items)
        store.set_status(ids[0], 'applied')
        state = self.request('/api/jobs/delete-all', {'category':'applied'}, headers)
        self.assertEqual([j['id'] for j in state['jobs']], [ids[1]])
        with self.assertRaises(urllib.error.HTTPError):
            self.request('/api/jobs/delete-all', {'category':'all'}, headers)

    def test_refresh_validates_chosen_companies(self):
        headers = {'Content-Type': 'application/json', 'X-App-Token': self.app.token}
        src = self.request('/api/jobs/sources/add', {'name': 'Acme', 'url': 'https://job-boards.greenhouse.io/acme'}, headers)['sources'][0]
        for bad in ([], [999], ['1'], [True], 'all'):
            with self.subTest(bad=bad), self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/jobs/refresh', {'sources': bad}, headers)
            self.assertEqual(error.exception.code, 400)
        with patch.object(self.app.jobs, 'start', return_value=True) as start:
            self.request('/api/jobs/refresh', {'sources': [src['id']]}, headers)
            self.request('/api/jobs/refresh', {}, headers)
        self.assertEqual(start.call_args_list[0].kwargs['source_ids'], {src['id']})
        self.assertIsNone(start.call_args_list[1].kwargs['source_ids'])

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
