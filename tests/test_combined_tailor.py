import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from resume import llm
from resume.combined_tailor import tailor_once
from resume.llm_tailor import ANGLES
from resume.storage import validate_profile
from resume.web import App

ROOT = Path(__file__).resolve().parents[1]
JD = 'Build Python backend services and REST APIs. Redis is required. Kafka is nice to have.'


class CombinedTailorTests(unittest.TestCase):
    def setUp(self):
        self.profile = validate_profile(json.loads((ROOT / 'profile.example.json').read_text()))
        first = self.profile['projects'][0]
        self.profile['projects'] = [dict(copy.deepcopy(first), id='project-' + str(i), name='Project ' + str(i)) for i in range(3)]
        for i, project in enumerate(self.profile['projects']):
            for j, bullet in enumerate(project['bullets']):
                bullet['id'] = f'p{i}-{j}'
        self.profile['projects'][0]['bullets'][0]['text'] = 'Built an **HTTP server** with a fixed **worker pool**.'

    def answer(self):
        return {'analysis': {'summary': 'Backend role', 'seniority': 'junior',
                'angle_scores': {angle: float(angle == 'backend') for angle in ANGLES},
                'matched_skills': ['Python'], 'gap_skills': ['redis', 'kafka'], 'optional_skills': ['Kafka']},
            'projects': [{'id': p['id'], 'angle': p['angles'][0], 'bullet_ids': [b['id'] for b in p['bullets'][:3]],
                          'why': 'Relevant implementation',
                          'rewrites': [{'id': b['id'], 'text': b['text']} for b in p['bullets'][:3]]}
                         for p in self.profile['projects'][:2]],
            'skills': [{'label': label, 'ordered_items': list(reversed(items))}
                       for label, items in self.profile['skills'].items() if isinstance(items, list) and not label.startswith('_')]}

    @patch('resume.llm.have_key', return_value=True)
    @patch('resume.llm.ask')
    def test_one_request_returns_selection_wording_and_analysis_with_frozen_sections(self, ask, _):
        ask.return_value = (self.answer(), False)
        before = copy.deepcopy(self.profile)
        source, report, rewritten, notice = tailor_once(self.profile, JD, model='test-model')
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(ask.call_args.kwargs['schema_name'], 'tailored_resume_v1')
        self.assertEqual(report['llm']['request_count'], 1)
        self.assertNotIn('kafka', report['gap_skills'])
        self.assertEqual(report['tailorer'], 'llm')
        self.assertEqual(source['projects'][0]['_project_id'], 'project-0')
        for section in ('basics', 'experience', 'education', 'coding_profiles'):
            self.assertEqual(source[section], rewritten[section])
        self.assertEqual(self.profile, before)
        sent = json.loads(ask.call_args.args[0])
        self.assertEqual(len(sent['projects']), 3)
        self.assertIn('max_words', sent['projects'][0]['bullets'][0])
        self.assertNotIn('basics', sent)

    @patch('resume.llm.have_key', return_value=True)
    @patch('resume.llm.ask')
    def test_invalid_numbers_bold_and_cross_project_rewrites_keep_original(self, ask, _):
        raw = self.answer()
        raw['projects'][0]['rewrites'][0]['text'] = 'Built an HTTP server.'
        raw['projects'][0]['rewrites'][1]['text'] = 'Served 999999 users.'
        raw['projects'][0]['rewrites'].append({'id': 'p1-0', 'text': 'Wrong owner.'})
        ask.return_value = (raw, False)
        source, report, rewritten, _ = tailor_once(self.profile, JD, model='test-model')
        self.assertEqual(source['projects'][0]['bullets'], rewritten['projects'][0]['bullets'])
        self.assertGreaterEqual(report['llm']['rejected_rewrites'], 2)
        self.assertEqual(ask.call_count, 1)

    @patch('resume.llm.have_key', return_value=True)
    @patch('resume.llm.ask', side_effect=llm.LLMError('unavailable'))
    def test_failure_uses_local_fallback_without_second_inference(self, ask, _):
        source, report, rewritten, notice = tailor_once(self.profile, JD, model='test-model')
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(source, rewritten)
        self.assertEqual(report['tailorer'], 'rules')
        self.assertIn('preserved', notice)

    @patch('resume.llm.have_key', return_value=True)
    @patch('resume.llm.ask')
    def test_web_draft_uses_one_inference_and_records_combined_timing(self, ask, _):
        ask.return_value = (self.answer(), True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'profile.example.json').write_text(json.dumps(self.profile))
            app = App(root, include_env=False)
            app.store.save_profile(self.profile)
            result = app.post('/api/tailor', {'jd': JD})
            self.assertEqual(ask.call_count, 1)
            report = app.drafts[result['id']]['report']
            self.assertTrue(report['llm']['cached']['combined'])
            self.assertIn('inference', [entry['id'] for entry in result['timings']['stages']])
            self.assertNotIn('preview', app.drafts[result['id']])
