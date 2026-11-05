"""Provider-independent structured inference, local key failover, and caching.

The CLI and browser share ask(); provider wire formats live in provider_api.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import threading
from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar

DEFAULT_MODEL = "gpt-4o-mini"
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "llm"



class LLMError(RuntimeError):
    """Any failure to get a valid answer. Callers fall back rather than crash."""

    def __init__(self, message: str, *, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class LLMAuthError(LLMError):
    """Missing or rejected API key. Never retried — retrying cannot help."""


# ── environment ──────────────────────────────────────────────────────────

def load_env(path: str | Path = None) -> None:
    """Minimal .env reader, so python-dotenv isn't a dependency.

    Never overwrites a variable already set in the real environment — an
    explicitly exported key should always win over a stale file.
    """
    path = Path(path or Path(__file__).resolve().parent.parent / ".env")
    if not path.exists():
        return
    try:
        path.chmod(0o600)  # keys live here; keep them user-only like credentials.json
    except OSError:
        pass
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def api_key() -> str:
    load_env()
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise LLMAuthError(
            "OPENAI_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    return key


_SESSION = ContextVar('jhalcv_credentials', default=None)
_ROUTE_HEALTH = {}
_HEALTH_LOCK = threading.Lock()
ROUTE_COOLDOWN = 60


def _health_key(route, session):
    # Scope by workspace and credential identity without retaining a raw key.
    return _cache_key([str(session.get('cache_dir') or CACHE_DIR), route['provider'],
                       route['model'], route.get('key'), route.get('cli')])


def _health(route, session):
    with _HEALTH_LOCK:
        return dict(_ROUTE_HEALTH.get(_health_key(route, session), {}))


def _note_health(route, session, success):
    now = time.monotonic()
    with _HEALTH_LOCK:
        if len(_ROUTE_HEALTH) > 256:
            _ROUTE_HEALTH.clear()
        previous = _ROUTE_HEALTH.get(_health_key(route, session), {})
        _ROUTE_HEALTH[_health_key(route, session)] = {
            'until': 0 if success else now + ROUTE_COOLDOWN,
            'success': now if success else previous.get('success', 0)}


@contextmanager
def credential_scope(routes, cache_dir=None):
    session = {'routes': routes, 'failed': set(), 'preferred': None, 'events': [],
               'used': [], 'cache_dir': cache_dir, 'last_call_failed': False}
    token = _SESSION.set(session)
    try:
        yield session
    finally:
        _SESSION.reset(token)


def have_key() -> bool:
    session = _SESSION.get()
    if session is not None and any(r['id'] not in session['failed'] for r in session['routes']):
        return True
    if session is None:
        from .credentials import CredentialStore
        if CredentialStore().routes():
            return True
    return bool(cli_routes())


def cli_routes() -> list[dict]:
    """Codex first, then Claude Code: the same subscription-CLI order the jobs
    pipeline uses, so tailoring also works with no API key at all. Empty when
    the CLIs are not installed."""
    return [{'id': f'cli:{name}', 'provider': name, 'model': model, 'cli': name}
            for name, model in (('codex', 'codex-default'), ('claude', 'sonnet'))
            if shutil.which(name)]


def _cli_ask(name: str, prompt: str, schema: dict) -> dict:
    from .jobs.agents import RUNNERS, AgentError  # lazy: jobs.agents imports this module
    try:
        with tempfile.TemporaryDirectory(prefix='jhalcv-tailor-') as folder:
            return RUNNERS[name](prompt, schema, Path(folder))
    except (AgentError, LLMError) as e:
        raise LLMError(f'{name} CLI could not answer: {str(e)[:160]}') from None


# ── schema helpers ───────────────────────────────────────────────────────

def strictify(schema: dict) -> dict:
    """Make a JSON schema satisfy OpenAI's `strict: true` rules.

    Strict mode demands that every object sets `additionalProperties: false` and
    lists *every* property in `required`. Forgetting this is the single most
    common reason a structured-output call gets rejected, so it is done here
    rather than left to each caller.
    """
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    if out.get("type") == "object":
        props = out.get("properties", {})
        out["properties"] = {k: strictify(v) for k, v in props.items()}
        out["additionalProperties"] = False
        out["required"] = list(props.keys())
    elif out.get("type") == "array" and "items" in out:
        out["items"] = strictify(out["items"])
    for key in ("anyOf", "oneOf", "allOf"):
        if key in out:
            out[key] = [strictify(s) for s in out[key]]
    return out


# ── cache ────────────────────────────────────────────────────────────────

def _cache_key(payload: dict) -> str:
    """Hash everything that can change the answer. Omit anything and you get
    stale results that look correct, which is far worse than a cache miss."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _cache_read(key: str, directory=None) -> dict | None:
    f = Path(directory or CACHE_DIR) / f"{key}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())["response"]
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def _cache_write(key: str, request: dict, response: dict, directory=None) -> None:
    directory = Path(directory or CACHE_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    # The request is stored alongside for debugging. It never contains the key:
    # auth travels in a header, not the body. Cache files hold prompts, so they
    # are written user-only and atomically, like every other local secret.
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=f'.{key}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(json.dumps({"request": request, "response": response}, indent=2, ensure_ascii=False))
        os.replace(temporary, directory / f"{key}.json")
    finally:
        Path(temporary).unlink(missing_ok=True)


# ── the call ─────────────────────────────────────────────────────────────

def ask(prompt: str, schema: dict, *, model: str = DEFAULT_MODEL,
        system: str | None = None, temperature: float = 0.0,
        schema_name: str = "response", cache: bool = True,
        timeout: int = 60) -> tuple[dict, bool]:
    """Try configured keys in order. Never expose keys in errors or caches."""
    from .credentials import CredentialStore
    from .provider_api import call, validate_output
    session = _SESSION.get()
    if session is None:
        with credential_scope(CredentialStore().routes(openai_model=model)):
            return ask(prompt, schema, model=model, system=system, temperature=temperature,
                       schema_name=schema_name, cache=cache, timeout=timeout)
    routes = sorted((r for r in session['routes'] if r['provider'] != 'zai'),
                    key=lambda r: r['id'] != session['preferred'])
    cli = cli_routes()
    if not routes and not cli:
        raise LLMAuthError('No API keys configured. Add a key in Settings or use rules mode.')
    candidates = routes + cli
    payloads = {}
    for route in candidates:
        payloads[route['id']] = {'version': 3 if route.get('cli') else 2, 'provider': route['provider'], 'model': route['model'],
            'prompt': prompt, 'system': system, 'schema': strictify(schema),
            'temperature': temperature, 'schema_name': schema_name}

    def checked(value, route):
        validate_output(value, payloads[route['id']]['schema'])
        encoded = json.dumps(value, ensure_ascii=False)
        if any(r.get('key') and r['key'] in encoded for r in session['routes']):
            raise LLMError('Provider response contained sensitive data and was discarded.')
        return value

    def used(route):
        session['last_call_failed'] = False
        session['preferred'] = route['id']
        metadata = {'provider': route['provider'], 'model': route['model']}
        if metadata not in session['used']:
            session['used'].append(metadata)

    # Look across all configured routes before launching a provider. A warm
    # fallback answer must not wait behind a cold or failing first provider.
    if cache:
        for route in candidates:
            cached = _cache_read(_cache_key(payloads[route['id']]), session['cache_dir'])
            if cached is None:
                continue
            try:
                result = checked(cached, route)
            except LLMError:
                continue
            from .usage import record_cache
            record_cache(route['provider'], route['model'])
            used(route)
            return result, True

    # Respect the saved API-key order, but prefer the last healthy CLI over an
    # untried one. Failed routes cool down across requests in this process.
    cli.sort(key=lambda route: -_health(route, session).get('success', 0))
    deadline = time.monotonic() + 120
    for route in routes + cli:
        if route['id'] in session['failed'] or _health(route, session).get('until', 0) > time.monotonic():
            continue
        if time.monotonic() >= deadline:
            break
        try:
            if route.get('cli'):
                cli_prompt = ('SYSTEM INSTRUCTIONS\n' + system + '\n\nUNTRUSTED INPUT DATA\n' + prompt) if system else prompt
                raw = _cli_ask(route['cli'], cli_prompt, payloads[route['id']]['schema'])
            else:
                raw = call(route, prompt, schema, system, temperature, schema_name,
                           max(1, min(timeout, 30, deadline - time.monotonic())))
            result = checked(raw, route)
        except (LLMError, OSError):
            _note_health(route, session, False)
            session['failed'].add(route['id'])
            session['events'].append({'provider': route['provider'], 'status': 'failed'})
            continue
        _note_health(route, session, True)
        used(route)
        if cache:
            try:
                _cache_write(_cache_key(payloads[route['id']]), payloads[route['id']], result, session['cache_dir'])
            except OSError:
                pass  # Never discard a successful inference because caching failed.
        return result, False
    session['last_call_failed'] = True
    raise LLMError('Configured AI providers are unavailable or cooling down. Using rules mode.')


if __name__ == "__main__":
    # Smoke test: python3 -m resume.llm
    import sys
    schema = {
        "type": "object",
        "properties": {
            "language": {"type": "string"},
            "confidence": {"type": "number"},
        },
    }
    if not have_key():
        print("no OPENAI_API_KEY found — copy .env.example to .env and add one")
        print("\nstrictify() works without a key:")
        print(json.dumps(strictify(schema), indent=2))
        sys.exit(0)
    out, hit = ask("What language is 'bonjour tout le monde'?", schema)
    print(json.dumps(out, indent=2), f"\n(cache hit: {hit})")
