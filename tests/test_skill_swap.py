import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from resume.combined_tailor import tailor_once
from resume.llm_tailor import ANGLES
from resume.skill_swap import expand, job_terms, notice, plan_swaps, summarize, without
from resume.storage import validate_profile
from resume.tailor import printed_skills, tailor
from resume.verify import extract_text
from resume.web import App
from resume.workspace import build_preview

ROOT = Path(__file__).resolve().parents[1]
JD = ('We need Python, Redis, Postgres and Terraform. Strong communication and problem solving. '
      'Kafka is a bonus. Our sales team uses Salesforce.')
SKILLS = [{'label': 'Languages', 'items': ['Python', 'Java', 'Ruby']},
          {'label': 'Databases', 'items': ['PostgreSQL', 'MySQL']},
          {'label': 'Infrastructure', 'items': ['Docker', 'Git']}]
GROUPS = [group['label'] for group in SKILLS]


def term(name, group='Infrastructure', importance='required'):
    return {'term': name, 'group': group, 'importance': importance}


class TermRules(unittest.TestCase):
    def test_a_term_must_be_in_the_job_and_be_a_skill(self):
        kept = job_terms([term('Redis'), term('Rust'), term('Strong communication'), term('problem solving'),
                          term('Terraform', importance='preferred'), term('terraform')], JD, GROUPS)
        self.assertEqual([item['term'] for item in kept], ['Redis', 'Terraform'])
        self.assertEqual(kept[1]['importance'], 'required')

    def test_technical_concepts_survive_the_generic_word_filter(self):
        jd = 'Experience with distributed systems, system design, CI/CD and C#.'
        terms = [term('distributed systems'), term('system design'), term('CI/CD'), term('C#')]
        self.assertEqual(len(job_terms(terms, jd, GROUPS)), 4)

    def test_malformed_model_entries_are_ignored(self):
        self.assertEqual(job_terms('Redis', JD, GROUPS), [])
        self.assertEqual(job_terms([None, {'term': 7}, {'group': 'Languages'}], JD, GROUPS), [])

    def test_an_unknown_group_never_creates_one(self):
        plan = plan_swaps({}, SKILLS, [term('Redis', 'Caching')], JD)
        self.assertEqual(plan['additions'], [])
        self.assertEqual([group['label'] for group in expand(SKILLS, plan)[0]], GROUPS)


class Planning(unittest.TestCase):
    def setUp(self):
        self.profile = {'_locked': {'pinned_skills': ['ruby']}}
        self.plan = plan_swaps(self.profile, SKILLS, [term('Redis', 'Databases'), term('Kafka', importance='preferred'),
                                                      term('Postgres', 'Databases')], JD, ['Python'])

    def test_other_spelling_is_added_beside_the_saved_one(self):
        self.assertEqual(self.plan['renames'], [{'group': 'Databases', 'from': 'PostgreSQL', 'to': 'PostgreSQL (Postgres)'}])
        self.assertIn('PostgreSQL (Postgres)', expand(SKILLS, self.plan)[0][1]['items'])

    def test_pinned_and_job_skills_are_never_removal_candidates(self):
        _, removals = expand(SKILLS, self.plan)
        gone = {removal['skill'] for removal in removals}
        self.assertFalse(gone & {'Ruby', 'Python', 'PostgreSQL', 'PostgreSQL (Postgres)'})
        self.assertLess([r['skill'] for r in removals].index('Git'), [r['skill'] for r in removals].index('Kafka'))
        self.assertLess([r['skill'] for r in removals].index('Kafka'), [r['skill'] for r in removals].index('Redis'))

    def test_removing_everything_allowed_keeps_pins_and_one_item_per_group(self):
        full, removals = expand(SKILLS, self.plan)
        result = without(full, removals)
        self.assertEqual([group['label'] for group in result], GROUPS)
        self.assertIn('Ruby', result[0]['items'])
        self.assertEqual(len(without(full, removals)[2]['items']), 1)

    def test_summary_counts_coverage_and_changes(self):
        full, removals = expand(SKILLS, self.plan)
        after = without(full, [r for r in removals if r['skill'] == 'Git'])
        summary = summarize(self.plan, {}, SKILLS, after)
        self.assertEqual(sorted(summary['added']), ['Kafka', 'Redis'])
        self.assertEqual(summary['removed'], ['Git'])
        self.assertEqual(summary['coverage_before']['required'], {'covered': 1, 'total': 2, 'missing': ['Redis']})
        self.assertEqual(summary['coverage_after']['required']['covered'], 2)
        self.assertIn('Skills removed to make room: Git.', notice(summary))

    def test_group_cap_cannot_cut_a_pinned_skill(self):
        items = [f'Skill{i}' for i in range(10)] + ['Pinned']
        self.assertIn('Pinned', printed_skills(items, ['pinned']))


class Page(unittest.TestCase):
    def setUp(self):
        self.profile = validate_profile(json.loads((ROOT / 'profile.example.json').read_text()))
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / 'templates').mkdir()
        (self.root / 'templates' / 'classic.typ').write_text((ROOT / 'templates' / 'classic.typ').read_text())
        (self.root / 'fonts').mkdir()
        self.source, self.report = tailor(self.profile, JD)
        self.addCleanup(self.directory.cleanup)

    def preview(self, plan):
        self.report['skill_plan'] = plan
        draft = {'source': self.source, 'report': self.report, 'notice': '', 'jd': JD}
        edits = [{'bullets': project['bullets']} for project in self.source['projects']]
        return build_preview(draft, edits, self.root, 'draft'), draft

    def test_added_skills_reach_the_pdf_without_costing_a_bullet(self):
        groups = [group['label'] for group in self.source['skills']]
        plan = plan_swaps(self.profile, self.source['skills'], [term('Redis', groups[0]), term('Terraform', groups[0])], JD)
        result, draft = self.preview(plan)
        text = extract_text(draft['preview']['path'])
        self.assertIn('Redis', text)
        self.assertEqual(result['pages'], 1)
        self.assertEqual(self.report['skill_plan'], plan)
        saved = json.loads((draft['preview']['path'].parent / 'resume.report.json').read_text())
        self.assertIn('Terraform', saved['skill_swap']['added'])
        self.assertFalse(saved['fit']['dropped'])

    def test_terms_that_cannot_fit_leave_a_one_page_resume(self):
        groups = [group['label'] for group in self.source['skills']]
        words = ' '.join(f'Tool{i}' for i in range(15))
        plan = plan_swaps(self.profile, self.source['skills'],
                          [term(f'Tool{i}', groups[0]) for i in range(15)], JD + ' ' + words)
        self.assertEqual(self.preview(plan)[0]['pages'], 1)

    @patch('resume.llm.have_key', return_value=True)
    @patch('resume.llm.ask')
    def test_the_combined_request_produces_a_plan_from_terms_in_the_job(self, ask, _):
        groups = [k for k, v in self.profile['skills'].items() if not k.startswith('_') and isinstance(v, list)]
        projects = self.profile['projects'][:2]
        ask.return_value = ({'analysis': {'summary': 'x', 'seniority': 'junior', 'matched_skills': [],
                                          'angle_scores': {angle: float(angle == 'backend') for angle in ANGLES},
                                          'gap_skills': [], 'optional_skills': []},
                             'projects': [{'id': p['id'], 'angle': p['angles'][0],
                                           'bullet_ids': [b['id'] for b in p['bullets'][:3]], 'why': 'x', 'rewrites': []}
                                          for p in projects],
                             'skills': [], 'jd_keywords': [term('Redis', groups[0]), term('Invented', groups[0])]}, False)
        source, report, _, _ = tailor_once(copy.deepcopy(self.profile), JD, model='m')
        self.assertEqual([item['term'] for item in report['skill_plan']['additions']], ['Redis'])
        self.assertIn('jd_keywords', ask.call_args.args[1]['properties'])


class Storage(unittest.TestCase):
    def test_pinned_skills_are_validated(self):
        profile = json.loads((ROOT / 'profile.example.json').read_text())
        profile.setdefault('_locked', {})['pinned_skills'] = ['Python']
        self.assertEqual(validate_profile(profile)['_locked']['pinned_skills'], ['Python'])
        profile['_locked']['pinned_skills'] = 'Python'
        with self.assertRaises(ValueError):
            validate_profile(profile)
