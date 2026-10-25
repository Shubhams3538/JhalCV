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
        self.assertEqual(Path(saved['path']).name, 'shubham_Resume.pdf')
        self.assertTrue(Path(saved['path']).read_bytes().startswith(b'%PDF'))
        self.assertEqual(Path(saved['archived']).read_bytes(), b'keep existing file')
        self.assertEqual(existing.read_bytes(), self.draft['preview']['path'].read_bytes())
        self.assertEqual(export_preview(self.draft, result['revision'], settings)['path'], saved['path'])
        self.assertEqual(len(list((folder / 'Archive').glob('*.pdf'))), 1)
        with self.assertRaisesRegex(ValueError, 'latest PDF'):
            export_preview(self.draft, 'stale', settings)

    def fake_preview(self, name, content):
        source = self.root / name
        source.write_bytes(content)
        return {'preview': {'path': source, 'revision': name}}

    def test_newest_is_current_and_previous_versions_remain_in_archive(self):
        settings = {'output_dir': str(self.root / 'resumes'), 'filename': 'shubham_Resume'}
        first = self.fake_preview('first', b'first resume')
        second = self.fake_preview('second', b'second resume')
        export_preview(first, 'first', settings)
        saved = export_preview(second, 'second', settings)
        folder = Path(settings['output_dir'])
        self.assertEqual((folder / 'shubham_Resume.pdf').read_bytes(), b'second resume')
        self.assertEqual(Path(saved['archived']).read_bytes(), b'first resume')
        self.assertEqual(len(list(folder.glob('*.pdf'))), 1)
        self.assertGreaterEqual((folder / 'shubham_Resume.pdf').stat().st_mtime_ns, Path(saved['archived']).stat().st_mtime_ns)
        # Saving the earlier preview again must actually restore it, not return
        # an obsolete path that now contains another draft's PDF.
        export_preview(first, 'first', settings)
        self.assertEqual((folder / 'shubham_Resume.pdf').read_bytes(), b'first resume')
        self.assertEqual(len(list((folder / 'Archive').glob('*.pdf'))), 2)

    def test_archive_failure_leaves_current_resume_untouched(self):
        folder = self.root / 'resumes'; folder.mkdir()
        current = folder / 'shubham_Resume.pdf'; current.write_bytes(b'previous')
        draft = self.fake_preview('new', b'new resume')
        with patch('resume.workspace.shutil.copyfileobj', side_effect=PermissionError('Cannot archive')):
            with self.assertRaises(PermissionError):
                export_preview(draft, 'new', {'output_dir': str(folder), 'filename': 'shubham_Resume'})
        self.assertEqual(current.read_bytes(), b'previous')
        self.assertNotIn('exported', draft['preview'])
        self.assertEqual(list(folder.glob('.jhalcv-*.tmp')), [])

    def test_current_file_is_untouched_when_replacement_fails(self):
        folder = self.root / 'resumes'; folder.mkdir()
        current = folder / 'shubham_Resume.pdf'; current.write_bytes(b'previous')
        draft = self.fake_preview('new', b'new resume')
        with patch('resume.workspace.os.replace', side_effect=PermissionError('Cannot replace')):
            with self.assertRaises(PermissionError):
                export_preview(draft, 'new', {'output_dir': str(folder), 'filename': 'shubham_Resume'})
        self.assertEqual(current.read_bytes(), b'previous')
        self.assertNotIn('exported', draft['preview'])

    def test_parallel_exports_cannot_replace_each_others_current_file(self):
        from resume.workspace import folder_lock
        folder = self.root / 'resumes'; folder.mkdir()
        draft = self.fake_preview('new', b'new resume')
        with folder_lock(folder):
            with self.assertRaisesRegex(ValueError, 'Another resume is being saved'):
                export_preview(draft, 'new', {'output_dir': str(folder), 'filename': 'shubham_Resume'})
        self.assertFalse((folder / 'shubham_Resume.pdf').exists())
        saved = export_preview(draft, 'new', {'output_dir': str(folder), 'filename': 'shubham_Resume'})
        self.assertEqual(Path(saved['path']).read_bytes(), b'new resume')

    def test_saving_same_preview_to_new_folder_uses_new_destination(self):
        draft = self.fake_preview('new', b'resume')
        first = export_preview(draft, 'new', {'output_dir': str(self.root / 'one'), 'filename': 'shubham_Resume'})
        second = export_preview(draft, 'new', {'output_dir': str(self.root / 'two'), 'filename': 'shubham_Resume'})
        self.assertNotEqual(first['path'], second['path'])
        self.assertEqual(Path(second['path']).read_bytes(), b'resume')

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
