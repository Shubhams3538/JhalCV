"""Run Codex or Claude Code headlessly on the user's own subscription.

Codex is tried first and Claude Code second; whichever fails (missing, logged
out, usage limit, bad output) is skipped for the rest of the run. Each agent
runs in an empty temporary folder with no tools, and API-key environment
variables are removed so a call can never be billed to an API account.
"""
from __future__ import annotations

import json
import hashlib
import threading
import time
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..usage import record_agent, usage_scope, record_cache
from .fetch import FetchError, acquire_slot, remaining
from ..llm import strictify
from ..provider_api import validate_output
from ..llm import LLMError

ORDER = ('codex', 'claude')
TIMEOUT = 75
# Answers are keyed by the exact prompt (page text or job text), so an unchanged
# page can reuse its answer for as long as refreshes are days apart.
CACHE_SECONDS = 14 * 86400
AI_SLOTS = 3
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
                              env=_env(), timeout=remaining(TIMEOUT))
    except subprocess.TimeoutExpired as e:
        record_agent(cmd[0], e.stdout)
        remaining()  # Company deadline exhaustion is not a provider outage.
        raise AgentError('timed out') from None
    except OSError as e:
        record_agent(cmd[0], '')
        raise AgentError(f'could not start ({e.strerror})') from None
    record_agent(cmd[0], proc.stdout)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or '').strip().splitlines()
        raise AgentError(detail[-1][:200] if detail else f'exit code {proc.returncode}')
    return proc.stdout


def _codex(prompt: str, schema: dict, folder: Path):
    schema_path, out_path = folder / 'schema.json', folder / 'answer.json'
    schema_path.write_text(json.dumps(schema))
    _run(['codex', 'exec', '--json', '--skip-git-repo-check', '--ephemeral', '--ignore-user-config', '--ignore-rules',
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

    def __init__(self, order=ORDER, *, usage_store=None, cache_dir=None):
        self.usage_store = usage_store
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._slots = threading.BoundedSemaphore(AI_SLOTS)
        self._state_lock = threading.Lock()
        self._local = threading.local()
        self.order = [name for name in order if shutil.which(name)]
        self.failed: dict[str, str] = {}
        self.used: list[str] = []

    @property
    def last_used(self):
        return getattr(self._local, 'provider', None)

    def ask(self, prompt: str, schema: dict) -> dict:
        schema = strictify(schema)
        key = hashlib.sha256(json.dumps([prompt, schema], sort_keys=True).encode()).hexdigest()
        path = self.cache_dir / (key + '.json') if self.cache_dir else None
        if path:
            try:
                entry = json.loads(path.read_text())
                if time.time() - entry['at'] < CACHE_SECONDS and entry['provider'] in self.order:
                    validate_output(entry['answer'], schema)
                    self._used(entry['provider'])
                    with usage_scope(self.usage_store, 'jobs'):
                        record_cache(entry['provider'], 'local agent cache')
                    return entry['answer']
            except (OSError, ValueError, TypeError, KeyError, LLMError):
                pass
        remaining(TIMEOUT)
        try:
            acquire_slot(self._slots, 'AI is busy; this company will retry next refresh.')
        except FetchError as e:
            raise AgentError(str(e)) from None
        try:
            remaining(TIMEOUT)
            answer = self._ask(prompt, schema)
        finally:
            self._slots.release()
        if path:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
                    tmp = Path(stream.name)
                    json.dump({'at': time.time(), 'provider': self.last_used, 'answer': answer}, stream)
                os.replace(tmp, path)
            except OSError:
                pass
        return answer

    def _used(self, name):
        self._local.provider = name
        with self._state_lock:
            if name not in self.used:
                self.used.append(name)

    def _ask(self, prompt, schema):
        errors = {}
        for name in self.order:
            with self._state_lock:
                if name in self.failed:
                    continue
            with tempfile.TemporaryDirectory(prefix='jhalcv-agent-') as tmp:
                try:
                    with usage_scope(self.usage_store, 'jobs'):
                        answer = RUNNERS[name](prompt, schema, Path(tmp))
                    validate_output(answer, schema)
                except (AgentError, LLMError) as e:
                    errors[name] = str(e)
                    # Bad page output and slow calls are local failures. Only an
                    # unavailable account/installation should disable a provider globally.
                    if any(word in str(e).lower() for word in
                           ('usage limit', 'rate limit', 'quota', 'logged out', 'login',
                            'not authenticated', 'authentication', 'unauthorized',
                            'credit balance', 'could not start')):
                        with self._state_lock:
                            self.failed[name] = str(e)
                    continue
            self._used(name)
            return answer
        if not self.order:
            raise AgentError('Neither Codex nor Claude Code is installed.')
        with self._state_lock:
            failures = dict(self.failed, **errors)
            raise AgentError('; '.join(f'{NAMES[n]}: {why}' for n, why in failures.items()))
