import unittest

from resume.llm_tailor import _catalog, validate_selection


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

    def test_prompt_asks_for_exact_count(self):
        self.assertIn('choose exactly 2', _catalog(PROFILE, 3, 2))
        self.assertIn('choose exactly 1', _catalog(PROFILE, 1, 2))


if __name__ == '__main__':
    unittest.main()
