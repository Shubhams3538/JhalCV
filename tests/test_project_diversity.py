import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from resume.combined_tailor import tailor_once
from resume.llm_tailor import ANGLES
from resume.storage import validate_profile
from resume.tailor import diversify, tailor, term_weights

ROOT = Path(__file__).resolve().parents[1]
JD = ('Systems engineer for low-level performance, latency, concurrency, memory and kernel work in C++. '
      'Also required: Python, FastAPI and PostgreSQL.')


def project(ident, stack, text, angles=('systems',)):
    return {'id': ident, 'name': ident.title(), 'stack': list(stack), 'angles': list(angles),
            'bullets': [{'id': f'{ident}-{i}', 'text': f'{text} part {i}.', 'angles': list(angles)} for i in range(3)]}


TCP = project('tcp', ['C++', 'C'], 'Wrote a TCP stack in C++')
VECTOR = project('vector', ['C++'], 'Built a vector index in C++')
API = project('api', ['Python', 'FastAPI', 'PostgreSQL'], 'Served a FastAPI backend on PostgreSQL', ('backend',))


class Diversify(unittest.TestCase):
    def test_second_slot_prefers_new_requirements_over_a_repeated_stack(self):
        terms = term_weights(['C++', 'Python', 'FastAPI', 'PostgreSQL'])
        chosen, swaps = diversify([TCP, VECTOR, API], terms, 2)
        self.assertEqual([p['id'] for p in chosen], ['tcp', 'api'])
        self.assertEqual(swaps[0]['instead_of']['id'], 'vector')
        self.assertEqual(swaps[0]['adds'], ['FastAPI', 'PostgreSQL', 'Python'])

    def test_rank_order_stays_when_nothing_adds_more(self):
        chosen, swaps = diversify([TCP, VECTOR, API], term_weights(['C++']), 2)
        self.assertEqual([p['id'] for p in chosen], ['tcp', 'vector'])
        self.assertEqual(swaps, [])

    def test_required_terms_outweigh_preferred_ones(self):
        rust = project('rust', ['Rust'], 'Wrote a Rust daemon')
        go = project('go', ['Go', 'gRPC'], 'Wrote a Go service')
        terms = term_weights(keywords=[{'term': 'Rust', 'importance': 'required'},
                                       {'term': 'Go', 'importance': 'preferred'}])
        self.assertEqual(diversify([API, go, rust], terms, 2)[0][1]['id'], 'rust')

    def test_the_top_pick_is_never_replaced(self):
        self.assertEqual(diversify([VECTOR, API], term_weights(['Python', 'FastAPI']), 1)[0], [VECTOR])


class Tailoring(unittest.TestCase):
    def setUp(self):
        self.profile = validate_profile(json.loads((ROOT / 'profile.example.json').read_text()))
        self.profile['projects'] = copy.deepcopy([TCP, VECTOR, API])
        self.profile['_locked']['max_projects_on_page'] = 2

    def test_rules_tailoring_spreads_projects_across_requirements(self):
        data, report = tailor(self.profile, JD)
        self.assertEqual([p['_project_id'] for p in data['projects']], ['tcp', 'api'])
        self.assertTrue(any(d.get('instead_of') == 'Vector' for d in report['decisions']))

    @patch('resume.llm.have_key', return_value=True)
    @patch('resume.llm.ask')
    def test_ai_picking_two_projects_with_one_stack_is_corrected(self, ask, _):
        ask.return_value = ({'analysis': {'summary': 'x', 'seniority': 'junior', 'gap_skills': [], 'optional_skills': [],
                                          'matched_skills': ['C++', 'Python', 'FastAPI', 'PostgreSQL'],
                                          'angle_scores': {a: float(a == 'systems') for a in ANGLES}},
                             'projects': [{'id': p['id'], 'angle': 'systems', 'bullet_ids': [b['id'] for b in p['bullets']],
                                           'why': 'x', 'rewrites': [{'id': b['id'], 'text': b['text']} for b in p['bullets']]}
                                          for p in (TCP, VECTOR)],
                             'skills': [], 'jd_keywords': []}, False)
        source, report, rewritten, notice = tailor_once(self.profile, JD, model='m')
        self.assertEqual([p['_project_id'] for p in source['projects']], ['tcp', 'api'])
        self.assertEqual(len(source['projects'][1]['bullets']), 3)
        self.assertEqual(report['llm']['rejected_rewrites'], 0)
        self.assertIn('Chose Api instead of Vector', notice)
        self.assertEqual(rewritten['projects'][1]['bullets'], source['projects'][1]['bullets'])
