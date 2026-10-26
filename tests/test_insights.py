import tempfile
import unittest
from pathlib import Path

from resume.insights import Insights, add_skill, canonical
from resume.tailor import MAX_SKILLS_PER_GROUP, tailor

PROFILE = {'skills': {'_comment': 'x', 'Languages': ['Python', 'C++'], 'Infrastructure': ['Docker']},
           'experience': [{'stack': ['Redis']}], 'projects': [{'stack': ['FastAPI']}]}


class CanonicalTests(unittest.TestCase):
    def test_spellings_collapse_to_one_skill(self):
        for group in (['C++', 'c++', 'cpp', ' CPP '], ['k8s', 'Kubernetes'], ['Node', 'nodejs', 'Node.js', 'node-js'],
                      ['Postgres', 'PostgreSQL'], ['golang', 'Go'], ['CI/CD', 'ci-cd']):
            with self.subTest(group=group):
                self.assertEqual(len({canonical(s) for s in group}), 1)


class InsightsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.insights = Insights(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_ranks_missing_and_asked_skills_by_share_of_jobs(self):
        self.insights.record('job one', ['Python', 'k8s', 'Kafka', 'cpp'])
        self.insights.record('job two', ['Kubernetes', 'C++', 'redis'])
        self.insights.record('job three', ['kubernetes', 'Go'])
        summary = self.insights.summary(PROFILE)
        self.assertEqual(summary['jobs'], 3)
        self.assertEqual([(m['skill'], m['count'], m['percent']) for m in summary['missing']],
                         [('Kubernetes', 3, 100), ('Go', 1, 33), ('Kafka', 1, 33)])
        asked = {a['skill']: a for a in summary['asked']}
        self.assertEqual(asked['C++']['count'], 2)
        self.assertTrue(asked['C++']['owned'])
        self.assertTrue(asked['Redis']['owned'])  # experience stacks count as having the skill
        self.assertFalse(asked['Kubernetes']['owned'])

    def test_same_job_counts_once(self):
        self.insights.record('Backend  role\nwith Kafka', ['Kafka'])
        self.insights.record('backend role with kafka', ['Kafka', 'Rust'])
        summary = self.insights.summary(PROFILE)
        self.assertEqual(summary['jobs'], 1)
        self.assertEqual({m['skill'] for m in summary['missing']}, {'Kafka', 'Rust'})

    def test_adding_a_skill_removes_it_from_missing(self):
        self.insights.record('job', ['k8s', 'Kafka'])
        profile = add_skill({**PROFILE, 'skills': {k: list(v) if isinstance(v, list) else v for k, v in PROFILE['skills'].items()}},
                            'k8s', 'Infrastructure')
        self.assertEqual(profile['skills']['Infrastructure'], ['Docker', 'Kubernetes'])
        self.assertEqual([m['skill'] for m in self.insights.summary(profile)['missing']], ['Kafka'])

    def test_add_skill_refuses_duplicates_and_unknown_groups(self):
        with self.assertRaisesRegex(ValueError, 'already'):
            add_skill({'skills': {'Languages': ['C++']}}, 'cpp', 'Languages')
        with self.assertRaisesRegex(ValueError, 'skill groups'):
            add_skill({'skills': {'Languages': []}}, 'Rust', '_comment')

    def test_hide_unhide_and_reset(self):
        self.insights.record('job', ['Kafka', 'Rust'])
        self.insights.hide('kafka')
        summary = self.insights.summary(PROFILE)
        self.assertEqual([m['skill'] for m in summary['missing']], ['Rust'])
        self.assertEqual(summary['hidden'], 1)
        self.insights.unhide_all()
        self.assertEqual(len(self.insights.summary(PROFILE)['missing']), 2)
        self.insights.reset()
        self.assertEqual(self.insights.summary(PROFILE)['jobs'], 0)


class SkillCapTests(unittest.TestCase):
    def test_rules_tailor_caps_groups_with_job_skills_first(self):
        extra = [f'Tool{i}' for i in range(12)]
        profile = {'basics': {}, 'experience': [], 'projects': [], 'education': [],
                   'skills': {'Tools': extra + ['Kafka']}}
        data, report = tailor(profile, 'We need Kafka experience for streaming services.')
        items = data['skills'][0]['items']
        self.assertEqual(len(items), MAX_SKILLS_PER_GROUP)
        self.assertEqual(items[0], 'Kafka')
        self.assertIn('Kafka', report['matched_skills'])


if __name__ == '__main__':
    unittest.main()
