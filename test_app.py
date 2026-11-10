#!/usr/bin/env python3
"""Launch a disposable, fresh-install copy of JhalCV for manual testing."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import urllib.request

ROOT = Path(__file__).resolve().parent
# Explicit allowlist: never copy personal profiles, .env, caches, databases,
# settings, saved keys, generated files, Git metadata, or other untracked data.
PUBLIC_FILES = ('app.py', 'profile.example.json')
PUBLIC_DIRECTORIES = ('resume', 'web', 'templates', 'data', 'fonts')
SECRET_ENV = {
    'OPENAI_API_KEY', 'CODEX_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
    'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENROUTER_API_KEY', 'ZAI_API_KEY',
    'ZHIPUAI_API_KEY', 'GITHUB_TOKEN', 'GH_TOKEN', 'ZAI_BASE_URL',
    'ZHIPUAI_BASE_URL', 'ANTHROPIC_BASE_URL', 'OPENAI_BASE_URL',
    'PYTHONPATH', 'PYTHONHOME',
}


@contextmanager
def fresh_workspace(source=ROOT):
    """Own and remove only the unique directory allocated for this run."""
    with tempfile.TemporaryDirectory(prefix='jhalcv-fresh-test-') as directory:
        root = Path(directory)
        source = Path(source)
        for name in PUBLIC_FILES:
            shutil.copy2(source / name, root / name)
        for name in PUBLIC_DIRECTORIES:
            shutil.copytree(source / name, root / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'),
                            symlinks=False)
        # The copied UI clearly identifies its disposable data, including after
        # setup completes and the user reaches the regular workspace.
        index = root / 'web' / 'index.html'
        html = index.read_text(encoding='utf-8')
        html = html.replace('<title>JhalCV', '<title>TEST — JhalCV')
        html = html.replace('● LOCAL WORKSPACE', '● FRESH TEST · DISPOSABLE DATA')
        index.write_text(html, encoding='utf-8')
        yield root


def child_environment():
    """Keep normal tools/PATH, but don't inherit personal service credentials."""
    return {key: value for key, value in os.environ.items() if key not in SECRET_ENV}


def stop_child(child):
    if os.name == 'nt':
        if child.poll() is not None:
            return
        # /T includes CLI subprocesses started while testing a provider.
        subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                       capture_output=True, timeout=10)
        child.wait(timeout=5)
        return
    try:
        # This PID is the session/group leader we created with
        # start_new_session=True, never the user's terminal or real app.
        os.killpg(child.pid, signal.SIGINT)
    except ProcessLookupError:
        child.wait(timeout=5)
        return
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # A CLI can ignore SIGINT or outlive its parent. Clear any remaining
        # members of this owned group before deleting the test workspace.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=5)


def smoke_check(child, root):
    """Check actual HTTP state, not merely whether the process launched."""
    lines = queue.Queue()
    def read_output():
        try:
            for line in child.stdout:
                lines.put(line)
        except (OSError, ValueError):
            pass  # The owning process may close its pipe during failed startup.
        lines.put(None)
    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    line = lines.get(timeout=20)
    if line is None or 'JhalCV is running at ' not in line:
        raise RuntimeError('The fresh test app did not start successfully.')
    url = line.split('JhalCV is running at ', 1)[1].strip()
    def get(path):
        with urllib.request.urlopen(url + path, timeout=10) as response:
            return json.load(response)
    state, jobs, usage = get('/api/state'), get('/api/jobs'), get('/api/usage')
    checks = {
        'onboarding_required': state['setup']['required'] is True,
        'blank_setup_profile': state['setup']['profile']['basics']['name'] == '',
        'no_saved_credentials': state['credentials']['keys'] == [],
        'no_saved_jobs': jobs['jobs'] == [],
        'no_saved_companies': jobs['sources'] == [],
        'starter_company_catalog': len(state['setup']['companies']) > 0,
        'empty_usage_ledger': usage['total']['calls'] == 0,
        'isolated_output_folder': Path(state['settings']['output_dir']).resolve().is_relative_to(root.resolve()),
    }
    if not all(checks.values()):
        raise RuntimeError('Fresh-install checks failed: ' + json.dumps(checks))
    print('Fresh-instance check passed: blank onboarding, no saved keys/jobs/companies, starter catalog available, isolated output.', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=0, help='Test port; default chooses a free port')
    parser.add_argument('--no-browser', action='store_true', help='Print the test URL without opening a browser')
    parser.add_argument('--check', action='store_true', help='Run an HTTP fresh-install check, then stop and remove the test instance')
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('--port must be between 0 and 65535')
    try:
        with fresh_workspace() as root:
            command = [sys.executable, str(root / 'app.py'), '--port', str(args.port)]
            if args.no_browser or args.check:
                command.append('--no-browser')
            print('Starting a FRESH TEST instance. Your real workspace is unchanged.', flush=True)
            print(f'Temporary data and default PDF output: {root}', flush=True)
            print('Test data is deleted when stopped. Run this script again to reset everything.', flush=True)
            print('Installed AI tools are still detected. Selecting/testing one uses its real login and quota. API keys must be added in this test instance.', flush=True)
            child = subprocess.Popen(command, cwd=root, env=child_environment(),
                                     stdout=subprocess.PIPE if args.check else None,
                                     stderr=subprocess.STDOUT if args.check else None,
                                     text=True, start_new_session=os.name != 'nt')
            try:
                if args.check:
                    smoke_check(child, root)
                else:
                    child.wait()
                    if child.returncode:
                        return child.returncode
            except KeyboardInterrupt:
                print('\nStopping the test instance…', flush=True)
            finally:
                stop_child(child)
                if child.stdout:
                    child.stdout.close()
        print('Test instance stopped; temporary workspace removed.', flush=True)
        return 0
    except (OSError, RuntimeError, queue.Empty, ValueError) as error:
        print(f'Could not run the fresh test instance: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
