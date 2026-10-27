"""Provider-independent structured inference, local key failover, and caching.

The CLI and browser share ask(); provider wire formats live in provider_api.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
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
    if session is not None:
        return any(r['id'] not in session['failed'] for r in session['routes'])
    from .credentials import CredentialStore
    return bool(CredentialStore().routes())


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
    # auth travels in a header, not the body.
    (directory / f"{key}.json").write_text(
        json.dumps({"request": request, "response": response}, indent=2, ensure_ascii=False)
    )


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
    routes = sorted(session['routes'], key=lambda r: r['id'] != session['preferred'])
    if not routes:
        raise LLMAuthError('No API keys configured. Add a key in Settings or use rules mode.')
    deadline = time.monotonic() + 120
    for route in routes:
        if route['id'] in session['failed']:
            continue
        if time.monotonic() >= deadline:
            break
        payload = {'version': 2, 'provider': route['provider'], 'model': route['model'],
                   'prompt': prompt, 'system': system, 'schema': strictify(schema),
                   'temperature': temperature, 'schema_name': schema_name}
        cache_key = _cache_key(payload)
        def checked(value):
            validate_output(value, payload['schema'])
            encoded = json.dumps(value, ensure_ascii=False)
            if any(r['key'] in encoded for r in routes):
                raise LLMError('Provider response contained sensitive data and was discarded.')
            return value
        try:
            result, hit = None, False
            if cache:
                cached = _cache_read(cache_key, session['cache_dir'])
                if cached is not None:
                    try:
                        result, hit = checked(cached), True
                        from .usage import record_cache
                        record_cache(route['provider'], route['model'])
                    except LLMError:
                        pass
            if result is None:
                result = checked(call(route, prompt, schema, system, temperature, schema_name,
                                      max(1, min(timeout, 30, deadline - time.monotonic()))))
                if cache:
                    try:
                        _cache_write(cache_key, payload, result, session['cache_dir'])
                    except OSError:
                        pass  # Cache failures must not waste a successful API call.
            session['last_call_failed'] = False
            session['preferred'] = route['id']
            used = {'provider': route['provider'], 'model': route['model']}
            if used not in session['used']:
                session['used'].append(used)
            return result, hit
        except (LLMError, OSError):
            session['failed'].add(route['id'])
            session['events'].append({'provider': route['provider'], 'status': 'failed'})
    session['last_call_failed'] = True
    raise LLMError('Configured API keys were unavailable or returned invalid responses. Using rules mode.')


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
