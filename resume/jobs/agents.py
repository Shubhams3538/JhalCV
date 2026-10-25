"""Run Codex or Claude Code headlessly on the user's own subscription.

Codex is tried first and Claude Code second; whichever fails (missing, logged
out, usage limit, bad output) is skipped for the rest of the run. Each agent
runs in an empty temporary folder with no tools, and API-key environment
variables are removed so a call can never be billed to an API account.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..llm import strictify
from ..provider_api import validate_output
from ..llm import LLMError

ORDER = ('codex', 'claude')
TIMEOUT = 300
NAMES = {'codex': 'Codex', 'claude': 'Claude Code'}
_SECRETS = ('OPENAI_API_KEY', 'CODEX_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
            'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'OPENROUTER_API_KEY')


class AgentError(RuntimeError):
    pass


def available() -> list[str]:
    return [name for name in ORDER if shutil.which(name)]


def _env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in _SECRETS}


def _run(cmd, prompt, cwd):
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd,
                              env=_env(), timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise AgentError('timed out') from None
    except OSError as e:
        raise AgentError(f'could not start ({e.strerror})') from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or '').strip().splitlines()
        raise AgentError(detail[-1][:200] if detail else f'exit code {proc.returncode}')
    return proc.stdout


def _codex(prompt: str, schema: dict, folder: Path):
    schema_path, out_path = folder / 'schema.json', folder / 'answer.json'
    schema_path.write_text(json.dumps(schema))
    _run(['codex', 'exec', '--skip-git-repo-check', '--ephemeral', '--ignore-user-config', '--ignore-rules',
          '--sandbox', 'read-only', '-C', str(folder), '--output-schema', str(schema_path),
          '-o', str(out_path), '-'], prompt, folder)
    try:
        return json.loads(out_path.read_text())
    except (OSError, json.JSONDecodeError):
        raise AgentError('returned no structured answer') from None


def _claude(prompt: str, schema: dict, folder: Path):
    out = _run(['claude', '-p', '--output-format', 'json', '--json-schema', json.dumps(schema),
                '--no-session-persistence', '--tools', '', '--strict-mcp-config', '--model', 'sonnet'],
               prompt, folder)
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise AgentError('returned unreadable output') from None
    if data.get('is_error') or not isinstance(data.get('structured_output'), dict):
        raise AgentError(str(data.get('result') or 'returned no structured answer')[:200])
    return data['structured_output']


RUNNERS = {'codex': _codex, 'claude': _claude}


class Agents:
    """One per refresh: remembers which agents already failed in this run."""

    def __init__(self, order=ORDER):
        self.order = [name for name in order if shutil.which(name)]
        self.failed: dict[str, str] = {}
        self.used: list[str] = []

    def ask(self, prompt: str, schema: dict) -> dict:
        schema = strictify(schema)
        for name in self.order:
            if name in self.failed:
                continue
            with tempfile.TemporaryDirectory(prefix='jhalcv-agent-') as tmp:
                try:
                    answer = RUNNERS[name](prompt, schema, Path(tmp))
                    validate_output(answer, schema)
                except (AgentError, LLMError) as e:
                    self.failed[name] = str(e)
                    continue
            if name not in self.used:
                self.used.append(name)
            return answer
        if not self.order:
            raise AgentError('Neither Codex nor Claude Code is installed.')
        raise AgentError('; '.join(f'{NAMES[n]}: {why}' for n, why in self.failed.items()))
