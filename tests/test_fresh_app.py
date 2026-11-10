import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_app


class FreshAppTests(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'POSIX process group cleanup')
    def test_stop_cleans_up_a_provider_subprocess_too(self):
        grandchild_code = 'import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(60)'
        parent_code = ('import subprocess,sys,time; '
                       f'p=subprocess.Popen([sys.executable,"-c",{grandchild_code!r}]); '
                       'print(p.pid,flush=True); time.sleep(60)')
        child = subprocess.Popen([sys.executable, '-c', parent_code], start_new_session=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            provider_pid = int(child.stdout.readline())
            test_app.stop_child(child)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                result = subprocess.run(['ps', '-p', str(provider_pid), '-o', 'stat='], capture_output=True, text=True)
                if result.returncode or result.stdout.strip().startswith('Z'):
                    break
                time.sleep(.03)
            else:
                self.fail('Provider subprocess survived test instance shutdown.')
            self.assertIsNotNone(child.poll())
        finally:
            test_app.stop_child(child)
            child.stdout.close()

    def test_workspace_copies_only_public_assets_and_cleans_up(self):
        with test_app.fresh_workspace() as first:
            self.assertTrue((first / 'app.py').exists())
            self.assertTrue((first / 'templates' / 'classic.typ').exists())
            self.assertTrue((first / 'data' / 'companies.json').exists())
            for name in ('profile.json', '.env', '.local', '.cache', 'build', '.git'):
                self.assertFalse((first / name).exists(), name)
            self.assertIn('FRESH TEST', (first / 'web' / 'index.html').read_text())
            (first / 'profile.json').write_text('{"private":"test edit"}')
            with test_app.fresh_workspace() as second:
                self.assertNotEqual(first, second)
                self.assertFalse((second / 'profile.json').exists())
            self.assertFalse(second.exists())
        self.assertFalse(first.exists())

    def test_environment_is_copied_and_credentials_removed_without_mutating_parent(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-sentinel-secret', 'GH_TOKEN': 'test-github-secret', 'JHALCV_TEST_SENTINEL': 'preserved'}):
            env = test_app.child_environment()
            self.assertNotIn('OPENAI_API_KEY', env)
            self.assertNotIn('GH_TOKEN', env)
            self.assertEqual(env['JHALCV_TEST_SENTINEL'], 'preserved')
            self.assertEqual(os.environ['OPENAI_API_KEY'], 'test-sentinel-secret')

    def test_http_smoke_launcher_exits_and_removes_its_workspace(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-sentinel-secret', 'GEMINI_API_KEY': 'test-gemini-secret'}):
            result = subprocess.run([sys.executable, str(test_app.ROOT / 'test_app.py'), '--check'],
                                    capture_output=True, text=True, timeout=35)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Fresh-instance check passed', result.stdout)
        self.assertNotIn('test-sentinel-secret', result.stdout + result.stderr)
        line = next(line for line in result.stdout.splitlines() if line.startswith('Temporary data and default PDF output: '))
        self.assertFalse(Path(line.split(': ', 1)[1]).exists())


if __name__ == '__main__': unittest.main()
