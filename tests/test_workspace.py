import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from resume.tailor import tailor
from resume.workspace import build_preview, export_preview

ROOT = Path(__file__).resolve().parents[1]


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        (self.root / 'templates').mkdir()
        (self.root / 'templates' / 'classic.typ').write_bytes((ROOT / 'templates' / 'classic.typ').read_bytes())
        data, report = tailor(json.loads((ROOT / 'profile.example.json').read_text()), 'Backend Python APIs')
        self.draft = {'source': data, 'data': copy.deepcopy(data), 'jd': 'Backend Python APIs', 'report': report, 'notice': 'Rules'}
        self.edits = [{'bullets': p['bullets'][:]} for p in data['projects']]

    def tearDown(self):
        self.temp.cleanup()

    def test_longer_edit_is_rejected_before_render(self):
        self.edits[0]['bullets'][0] += ' invented extra content ' * 20
        with self.assertRaisesRegex(ValueError, 'budget'):
            build_preview(self.draft, self.edits, self.root, 'test')

    @unittest.skipUnless(all(shutil.which(x) for x in ('typst', 'pdfinfo', 'pdftotext')), 'PDF tools not installed')
    def test_real_pdf_custom_folder_collision_and_revision(self):
        result = build_preview(self.draft, self.edits, self.root, 'test')
        self.assertEqual(result['pages'], 1)
        self.assertEqual(result['checks'], [])
        settings = {'output_dir': str(self.root / 'custom folder'), 'filename': 'shubham_Resume'}
        folder = Path(settings['output_dir']); folder.mkdir()
        existing = folder / 'shubham_Resume.pdf'; existing.write_bytes(b'keep existing file')
        saved = export_preview(self.draft, result['revision'], settings)
        self.assertEqual(Path(saved['path']).name, 'shubham_Resume_2.pdf')
        self.assertTrue(Path(saved['path']).read_bytes().startswith(b'%PDF'))
        self.assertEqual(existing.read_bytes(), b'keep existing file')
        self.assertEqual(export_preview(self.draft, result['revision'], settings), saved)
        with self.assertRaisesRegex(ValueError, 'latest PDF'):
            export_preview(self.draft, 'stale', settings)

    @patch('resume.workspace.fit')
    def test_overflow_never_exports(self, fit):
        fit.return_value = {'pages': 2, 'dropped': []}
        with self.assertRaisesRegex(ValueError, 'cannot fit'):
            build_preview(self.draft, self.edits, self.root, 'test')
        self.assertNotIn('preview', self.draft)

    @unittest.skipUnless(all(shutil.which(x) for x in ('typst', 'pdfinfo', 'pdftotext')), 'PDF tools not installed')
    def test_ai_wrapping_overflow_restores_baseline_layout(self):
        from resume.render import page_count
        real_count = page_count
        calls = 0
        def simulated_count(path):
            nonlocal calls
            if path.name == 'resume.pdf':
                return 2
            return real_count(path)
        with patch('resume.workspace.page_count', side_effect=simulated_count):
            result = build_preview(self.draft, self.edits, self.root, 'test')
        self.assertIn('Restored original', result['notices'][0])
        self.assertEqual(real_count(self.draft['preview']['path']), 1)
        self.assertEqual(self.draft['preview']['path'].read_bytes(), (self.draft['preview']['path'].parent / 'baseline.pdf').read_bytes())


if __name__ == '__main__':
    unittest.main()
