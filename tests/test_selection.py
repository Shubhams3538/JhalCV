import unittest
from unittest.mock import patch

from resume.llm_tailor import _catalog, _select_prompt, parse_jd_llm, validate_selection


def project(pid, angle):
    return {'id': pid, 'name': pid.title(), 'stack': ['Python'], 'angles': [angle],
            'bullets': [{'id': f'{pid}-{i}', 'angles': [angle], 'text': f'{pid} bullet {i}'} for i in range(3)]}


PROFILE = {'projects': [project('server', 'backend'), project('agent', 'agentic')],
           'skills': {'Languages': ['Python', 'C++']}}
ANALYSIS = {'angle_scores': {'backend': 0.7, 'agentic': 0.3}, 'matched_skills': ['Python']}


class SelectionTests(unittest.TestCase):
    def test_every_checked_project_is_kept_when_it_fits_the_page(self):
        raw = {'projects': [{'id': 'server', 'angle': 'backend', 'bullet_ids': ['server-0']}], 'skills': []}
        selection, problems = validate_selection(raw, PROFILE, ANALYSIS, max_projects=2, bullets_per_project=2)
        self.assertEqual([p['name'] for p in selection['projects']], ['Server', 'Agent'])
        self.assertEqual(selection['projects'][1]['bullets'], ['agent bullet 0', 'agent bullet 1'])
        self.assertTrue(any('omitted by the model' in p for p in problems))

    def test_page_cap_still_limits_checked_projects(self):
        raw = {'projects': [{'id': 'server', 'angle': 'backend', 'bullet_ids': ['server-0']}], 'skills': []}
        selection, _ = validate_selection(raw, PROFILE, ANALYSIS, max_projects=1, bullets_per_project=2)
        self.assertEqual([p['name'] for p in selection['projects']], ['Server'])

    def test_short_bullet_answers_are_topped_up(self):
        raw = {'projects': [{'id': 'server', 'angle': 'backend', 'bullet_ids': ['server-2']},
                            {'id': 'agent', 'angle': 'agentic', 'bullet_ids': ['agent-0', 'agent-1', 'agent-2']}], 'skills': []}
        selection, problems = validate_selection(raw, PROFILE, ANALYSIS, max_projects=2, bullets_per_project=3)
        self.assertEqual(selection['projects'][0]['bullets'], ['server bullet 0', 'server bullet 1', 'server bullet 2'])
        self.assertTrue(any('fewer than 3 bullets' in p for p in problems))
        self.assertTrue(all(m['score'] > 0 for m in selection['projects'][0]['_bullet_meta']))

    def test_prompt_asks_for_exact_count(self):
        self.assertIn('choose exactly 2, and for each one exactly 2 bullets', _catalog(PROFILE, 3, 2))
        self.assertIn('choose exactly 1', _catalog(PROFILE, 1, 2))


    def test_selection_prompt_includes_the_job_description(self):
        prompt = _select_prompt(PROFILE, {**ANALYSIS, 'summary': 's'}, 2, 3, 'We stream events over WebSockets.')
        self.assertIn('We stream events over WebSockets.', prompt)


class OptionalSkillTests(unittest.TestCase):
    @patch('resume.llm_tailor.llm.ask')
    def test_nice_to_have_and_background_skills_are_not_missing(self, ask):
        ask.return_value = ({'summary': '', 'seniority': 'mid',
                             'angle_scores': {'backend': 1.0}, 'matched_skills': ['Python'],
                             'gap_skills': ['redis', 'kafka'], 'optional_skills': ['Kafka', 'k8s']}, False)
        jd = 'Required: Python and Redis. Nice to have: Kafka and Kubernetes.'
        result, _ = parse_jd_llm(jd, PROFILE)
        self.assertEqual(result['gap_skills'], ['redis'])

if __name__ == '__main__':
    unittest.main()
