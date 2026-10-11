import copy
import json
import tempfile
import unittest
from pathlib import Path

from resume.storage import Store, validate_profile

ROOT = Path(__file__).resolve().parents[1]


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.profile = json.loads((ROOT / 'profile.example.json').read_text())

    def test_atomic_save_and_custom_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'profile.example.json').write_text(json.dumps(self.profile))
            store = Store(root)
            self.assertEqual(store.profile()['basics']['name'], 'Ada Lovelace')
            store.save_profile(self.profile)
            self.assertTrue(store.profile_path.exists())
            store.save_settings({'output_dir': 'my resumes', 'filename': 'shubham_Resume', 'model': 'gpt-5.5', 'tailorer': 'rules'})
            self.assertEqual(store.settings()['output_dir'], str((root / 'my resumes').resolve()))
            self.assertEqual(store.settings()['filename'], 'shubham_Resume')

    def test_invalid_profile_does_not_replace_saved_data(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.save_profile(self.profile)
            before = store.profile_path.read_bytes()
            bad = copy.deepcopy(self.profile)
            bad['projects'][0]['link']['url'] = 'javascript:alert(1)'
            with self.assertRaises(ValueError):
                store.save_profile(bad)
            self.assertEqual(before, store.profile_path.read_bytes())

    def test_planned_bullets_do_not_count_as_evidence(self):
        self.profile['projects'][0]['bullets'] = self.profile['projects'][0]['bullets'][:2]
        self.profile['projects'][0]['bullets'][1]['status'] = 'planned'
        with self.assertRaisesRegex(ValueError, 'two confirmed'):
            validate_profile(self.profile)

    def test_filename_cannot_escape_destination(self):
        with self.assertRaises(ValueError):
            Store(ROOT).validate_settings({'filename': '../profile.json', 'output_dir': 'build', 'model': 'gpt-5.5', 'tailorer': 'rules'})


if __name__ == '__main__':
    unittest.main()
