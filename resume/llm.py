"""The only module in this project that touches the network.

Deliberately dependency-free: one POST to the OpenAI REST API via urllib, so the
project keeps its "brew install typst poppler, no Python packages" property. If
this ever needs streaming, batching, or Azure, swap it for the `openai` SDK —
`ask()` is the only symbol anything else imports.

Nothing here knows what a resume is.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "llm"

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4


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


def have_key() -> bool:
    """Cheap check so callers can pick a tailorer without catching exceptions."""
    try:
        api_key()
        return True
    except LLMAuthError:
        return False


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


def _cache_read(key: str) -> dict | None:
    f = CACHE_DIR / f"{key}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())["response"]
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def _cache_write(key: str, request: dict, response: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # The request is stored alongside for debugging. It never contains the key:
    # auth travels in a header, not the body.
    (CACHE_DIR / f"{key}.json").write_text(
        json.dumps({"request": request, "response": response}, indent=2, ensure_ascii=False)
    )


# ── the call ─────────────────────────────────────────────────────────────

def _post(body: dict, key: str, timeout: int) -> dict:
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400]
            if e.code in (401, 403):
                raise LLMAuthError(f"OpenAI rejected the API key ({e.code}).") from e
            if e.code not in RETRY_STATUS or attempt == MAX_ATTEMPTS - 1:
                raise LLMError(f"OpenAI HTTP {e.code}: {detail}",
                               status=e.code, body=detail) from e
            # Honour Retry-After when the server sends it; otherwise back off.
            wait = float(e.headers.get("Retry-After") or 0) or 2 ** attempt
            last_error = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if attempt == MAX_ATTEMPTS - 1:
                raise LLMError(f"OpenAI request failed: {e}") from e
            wait = 2 ** attempt
            last_error = str(e)
        time.sleep(wait + random.uniform(0, 0.4))  # jitter, so retries don't sync up
    raise LLMError(f"exhausted retries: {last_error}")


def ask(prompt: str, schema: dict, *, model: str = DEFAULT_MODEL,
        system: str | None = None, temperature: float = 0.0,
        schema_name: str = "response", cache: bool = True,
        timeout: int = 60) -> tuple[dict, bool]:
    """Ask for one structured answer.

    Returns (parsed_response, cache_hit). The response is guaranteed to match
    `schema` — OpenAI enforces it server-side in strict mode, so there is no
    prose to parse and no malformed JSON to defend against.

    Raises LLMError (or LLMAuthError) on any failure. Callers are expected to
    catch and fall back, never to crash.
    """
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True,
                            "schema": strictify(schema)},
        },
    }

    key_hash = _cache_key(body)
    if cache:
        hit = _cache_read(key_hash)
        if hit is not None:
            return hit, True

    try:
        raw = _post(body, api_key(), timeout)
    except LLMError as e:
        # Reasoning models reject `temperature` outright rather than ignoring it.
        # Rather than maintaining a list of which families do that, drop the
        # offending parameter once and retry. Determinism is lost, but a correct
        # answer at the default beats no answer at all.
        if e.status == 400 and "temperature" in e.body and "unsupported" in e.body.lower():
            body.pop("temperature", None)
            key_hash = _cache_key(body)
            if cache:
                hit = _cache_read(key_hash)
                if hit is not None:
                    return hit, True
            raw = _post(body, api_key(), timeout)
        else:
            raise
    try:
        choice = raw["choices"][0]["message"]
        if choice.get("refusal"):
            raise LLMError(f"model refused: {choice['refusal']}")
        parsed = json.loads(choice["content"])
    except (KeyError, IndexError, json.JSONDecodeError, TypeError) as e:
        raise LLMError(f"unexpected response shape: {e}") from e

    if cache:
        _cache_write(key_hash, body, parsed)
    return parsed, False


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
