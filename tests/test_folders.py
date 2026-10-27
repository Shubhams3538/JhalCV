import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from resume.folders import choose_folder


class FolderTests(unittest.TestCase):
    @patch('resume.folders.platform.system', return_value='Darwin')
    @patch('resume.folders.subprocess.run')
    def test_picker_returns_selected_folder_and_passes_paths_as_arguments(self, run, _):
        with tempfile.TemporaryDirectory(prefix='resume folder ') as directory:
            run.return_value = SimpleNamespace(returncode=0, stdout=directory + '/\n', stderr='')
            self.assertEqual(choose_folder(Path(directory) / 'not-created-yet'), str(Path(directory).resolve()))
            command = run.call_args.args[0]
            self.assertEqual(command[-1], str(Path(directory).resolve()))
            self.assertNotIn(directory, command[2])

    @patch('resume.folders.platform.system', return_value='Linux')
    @patch('resume.folders.shutil.which', return_value='/usr/bin/zenity')
    @patch('resume.folders.subprocess.run')
    def test_picker_preserves_spaces_in_the_selected_folder_name(self, run, *_):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            selected = parent / 'resumes '
            sibling = parent / 'resumes'
            selected.mkdir(); sibling.mkdir()
            run.return_value = SimpleNamespace(returncode=0, stdout=str(selected) + '\n', stderr='')
            self.assertEqual(choose_folder(parent), str(selected))

    @patch('resume.folders.platform.system', return_value='Darwin')
    @patch('resume.folders.subprocess.run')
    def test_cancel_does_not_change_folder(self, run, _):
        run.return_value = SimpleNamespace(returncode=1, stdout='', stderr='User canceled. (-128)')
        self.assertIsNone(choose_folder(Path.home()))

    @patch('resume.folders.platform.system', return_value='Darwin')
    @patch('resume.folders.subprocess.run', side_effect=subprocess.TimeoutExpired('osascript', 300))
    def test_picker_timeout_explains_manual_fallback(self, run, _):
        with self.assertRaisesRegex(ValueError, 'enter the path manually'):
            choose_folder(Path.home())

    @patch('resume.folders.platform.system', return_value='Linux')
    @patch('resume.folders.shutil.which', return_value=None)
    def test_missing_picker_explains_manual_fallback(self, *_):
        with self.assertRaisesRegex(ValueError, 'Enter the destination path manually'):
            choose_folder(Path.home())
