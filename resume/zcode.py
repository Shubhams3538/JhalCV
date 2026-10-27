"""GLM bridge: ZCode's own Z.AI login, used with the user's standing consent.

ZCode stores its Z.AI OAuth access token AES-256-GCM encrypted in
~/.zcode/v2/credentials.json, with the key derived from machine-local values.
Decrypted in memory, that token authenticates against the same Coding Plan
endpoint the Z.AI provider already uses (verified October 26, 2026: a wrong
token is rejected with HTTP 401, this token only throttled with HTTP 429).

The token never reaches disk, logs, reports, or the browser: a helper process
decrypts it into this process's memory for one short cache window at a time.
Consent lives in .local/zcode_glm.json: while it reads {"enabled": true} the
bridge is on, and deleting the file or setting false turns GLM off again.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .credentials import PROVIDERS

ROOT = Path(__file__).resolve().parents[1]
CONSENT_PATH = ROOT / '.local' / 'zcode_glm.json'
CACHE_SECONDS = 600

# Mirrors ZCode's bundled credential resolver: values starting with enc:v1: are
# AES-256-GCM, key = SHA-256 of the resolved secret (env override, else the
# machine-local fallback). Prints only the access token on stdout, never any
# other field or error detail.
_HELPER = """
const fs = require('node:fs'), os = require('node:os'), crypto = require('node:crypto');
let data;
try { data = JSON.parse(fs.readFileSync(os.homedir() + '/.zcode/v2/credentials.json', 'utf8')); } catch { process.exit(0); }
const secret = (process.env.ZCODE_CREDENTIAL_SECRET || '').trim()
  || `zcode-credential-fallback:${os.platform()}:${os.homedir()}:${os.userInfo().username}`;
const key = crypto.createHash('sha256').update(secret).digest();
let found = null;
(function walk(node) {
  for (const field of Object.keys(node)) {
    const value = node[field];
    if (typeof value === 'string' && value.startsWith('enc:v1:')) {
      const [iv, tag, ciphertext] = value.slice(7).split('.');
      try {
        const decipher = crypto.createDecipheriv('aes-256-gcm', key, Buffer.from(iv, 'base64url'));
        decipher.setAuthTag(Buffer.from(tag, 'base64url'));
        const plain = Buffer.concat([decipher.update(Buffer.from(ciphertext, 'base64url')), decipher.final()]).toString('utf-8');
        if (field === 'oauth:zai:access_token') found = plain;
      } catch {}
    } else if (value && typeof value === 'object') {
      walk(value);
    }
  }
})(data);
if (found !== null) process.stdout.write(found);
"""

_lock = threading.Lock()
_cache = (0.0, None)  # (monotonic deadline, token or None when unusable)


def _node() -> str | None:
    found = shutil.which('node')
    if found:
        return found
    versions = sorted(Path.home().glob('.nvm/versions/node/*/bin/node'),
                      key=lambda path: [int(part) for part in path.parts[-3][1:].split('.') if part.isdigit()])
    return str(versions[-1]) if versions else None


def access_token() -> str | None:
    """ZCode's decrypted Z.AI access token, or None. Held in memory only."""
    global _cache
    now = time.monotonic()
    with _lock:
        if _cache[0] > now:
            return _cache[1]
        token = None
        node = _node()
        if node:
            try:
                proc = subprocess.run([node, '-e', _HELPER], capture_output=True, text=True, timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                proc = None
            if proc is not None:
                candidate = proc.stdout.strip()
                if 20 <= len(candidate) <= 4096 and all(32 < ord(c) < 127 for c in candidate):
                    token = candidate
        _cache = (now + CACHE_SECONDS, token)
        return token


def allowed() -> bool:
    """True while .local/zcode_glm.json opts the bridge in with {"enabled": true}."""
    try:
        saved = json.loads(CONSENT_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return isinstance(saved, dict) and saved.get('enabled') is True


def route() -> dict | None:
    """A Z.AI route backed by ZCode's login, or None. The key stays in this process."""
    if not allowed():
        return None
    token = access_token()
    if token is None:
        return None
    return {'id': 'zcode:zai', 'provider': 'zai', 'label': 'ZCode login (bridge)',
            'model': PROVIDERS['zai']['default_model'], 'key': token, 'source': 'zcode'}
