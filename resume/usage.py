"""Local usage ledger. Stores counts and prices, never prompts or credentials."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3

CHECKED = '2026-10-25'
SOURCES = {'openai': 'https://developers.openai.com/api/docs/pricing',
           'anthropic': 'https://platform.claude.com/docs/en/about-claude/pricing',
           'gemini': 'https://ai.google.dev/gemini-api/docs/pricing',
           'openrouter': 'https://openrouter.ai/docs/cookbook/administration/usage-accounting'}
# Standard text rates in USD per million: fresh input, output, cache read,
# cache write (default TTL). Historical events retain their computed valuation.
RATES = {
    'gpt-6.1-sol': (1, 5, .05, 1.25),
    'gpt-5.4-mini': (.75, 4.5, .075, .75),
    'gpt-5.5': (5, 30, .5, 5),
    'gpt-4o-mini': (.15, .6, .075, .15),
    'claude-sonnet-5-5': (2, 10, .1, 2.5),
    'claude-opus-5-5': (4, 20, .2, 5),
    'claude-haiku-5-5': (.1, .5, .01, .125),
    'claude-sonnet-4-6': (3, 15, .3, 3.75),
    'claude-sonnet-4-5': (3, 15, .3, 3.75),
    'claude-haiku-4-5': (1, 5, .1, 1.25),
    'gemini-3.8-flash': (.75, 3.75, .075, .75),
    'gemini-3.5-flash-lite': (.3, 2.5, .03, .3),
}
REFERENCES = [m for m in RATES if m.startswith('gpt-')]
_SCOPE = ContextVar('usage_scope', default=None)


def count(value):
    return value if type(value) is int and value >= 0 else None


def money(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def model_key(model):
    model = model.split('/')[-1]
    model = re.sub(r'-(?:\d{8}|\d{4}-\d{2}-\d{2})$', '', model)
    return model.replace('5.5', '5-5') if model.startswith('claude-') else model


def estimate(model, inp, out, read, write, at, write_1h=0):
    key = model_key(model)
    rates = RATES.get(key)
    if rates is None or inp is None or out is None or read + write > inp:
        return None
    # Unknown long-context tiers are left unpriced rather than undercounted.
    if key == 'gpt-6.1-sol' and inp > 272000:
        return None
    if key == 'gpt-5.5' and inp > 272000:
        rates = tuple(v * f for v, f in zip(rates, (2, 1.5, 2, 2)))
    if key == 'claude-haiku-5-5' and inp > 100000:
        rates = (.5, 2.5, .05, .625)
    if key == 'gemini-3.8-flash' and at[:10] >= '2027-01-01':
        rates = (1.5, 7.5, .15, 1.5)
    fresh, output, cached, creation = rates
    return ((inp - read - write) * fresh + out * output + read * cached
            + (write - write_1h) * creation + write_1h * fresh * 2) / 1_000_000


@contextmanager
def usage_scope(store, purpose):
    token = _SCOPE.set((store, purpose) if store else None)
    try:
        yield
    finally:
        _SCOPE.reset(token)


class UsageStore:
    def __init__(self, root):
        self.path = Path(root) / '.local' / 'usage.db'
        self.error = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at TEXT NOT NULL, data TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO meta VALUES (?, ?)', ('started_at', self.now()))
            db.execute('INSERT OR IGNORE INTO meta VALUES (?, ?)', ('codex_reference', 'gpt-6.1-sol'))
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def reference(self):
        with self.connect() as db:
            return db.execute("SELECT value FROM meta WHERE key='codex_reference'").fetchone()[0]

    def set_reference(self, model):
        if model not in REFERENCES:
            raise ValueError('Choose a listed valuation model.')
        with self.connect() as db:
            db.execute("UPDATE meta SET value=? WHERE key='codex_reference'", (model,))
        return self.summary()

    def record(self, provider, model, inp=None, out=None, read=0, write=0,
               *, channel='api', purpose='resume', reported=None, basis=None, write_1h=0, cache_hit=False):
        try:
            at = self.now()
            inp, out = count(inp), count(out)
            read, write, write_1h = count(read) or 0, count(write) or 0, count(write_1h) or 0
            if inp is not None and (read + write > inp or write_1h > write):
                inp = None
            valuation = self.reference() if provider == 'codex' else model
            cost = money(reported)
            cost_basis = basis if cost is not None else 'estimated'
            if cost is None:
                cost = estimate(valuation, inp, out, read, write, at, write_1h)
            event = dict(provider=provider, model=model, valuation_model=valuation,
                         channel=channel, purpose=purpose, input_tokens=inp, output_tokens=out,
                         cached_input_tokens=read, cache_write_tokens=write, cost_usd=cost,
                         cost_basis=cost_basis if cost is not None else 'unknown',
                         cache_hit=cache_hit, rates_checked=CHECKED)
            with self.connect() as db:
                db.execute('INSERT INTO events(at,data) VALUES (?,?)', (at, json.dumps(event)))
        except (OSError, sqlite3.Error):
            # Accounting must never cause a paid inference to be retried.
            self.error = True

    def summary(self):
        with self.connect() as db:
            meta = dict(db.execute('SELECT key,value FROM meta'))
            rows = db.execute('SELECT at,data FROM events ORDER BY id').fetchall()
        def empty():
            return dict(input_tokens=0, output_tokens=0, cached_input_tokens=0, cache_write_tokens=0,
                        cost_usd=0., api_cost_usd=0., subscription_cost_usd=0., calls=0,
                        cache_hits=0, unmeasured=0, unpriced=0)
        total, today, groups = empty(), empty(), {}
        for at, data in rows:
            e = json.loads(data)
            key = (e['provider'], e['model'], e['channel'], e['valuation_model'])
            if key not in groups:
                groups[key] = dict(empty(), provider=key[0], model=key[1], channel=key[2], valuation_model=key[3])
            targets = [total, groups[key]]
            if datetime.fromisoformat(at).astimezone().date() == datetime.now().astimezone().date():
                targets.append(today)
            for target in targets:
                if e['cache_hit']:
                    target['cache_hits'] += 1
                    continue
                target['calls'] += 1
                target['unmeasured'] += int(e['input_tokens'] is None or e['output_tokens'] is None)
                target['unpriced'] += int(e['cost_usd'] is None)
                for field in ('input_tokens', 'output_tokens', 'cached_input_tokens', 'cache_write_tokens'):
                    target[field] += e[field] or 0
                cost = e['cost_usd'] or 0
                target['cost_usd'] += cost
                target['subscription_cost_usd' if e['channel'] == 'subscription' else 'api_cost_usd'] += cost
        return dict(started_at=meta['started_at'], codex_reference=meta['codex_reference'],
                    reference_models=REFERENCES, rates_checked=CHECKED, sources=SOURCES,
                    total=total, today=today, groups=list(groups.values()), accounting_error=self.error)


def record_api(provider, model, response):
    scope = _SCOPE.get()
    if not scope:
        return
    store, purpose = scope
    data = response if isinstance(response, dict) else {}
    u = data.get('usageMetadata' if provider == 'gemini' else 'usage') or {}
    if not isinstance(u, dict):
        u = {}
    inp = out = None
    read = write = hour = 0
    if provider in ('openai', 'openrouter'):
        inp, out = count(u.get('prompt_tokens')), count(u.get('completion_tokens'))
        details = u.get('prompt_tokens_details') or {}
        if isinstance(details, dict):
            read = details.get('cached_tokens', 0)
            write = details.get('cache_write_tokens', 0)
    elif provider == 'anthropic':
        inp, out = count(u.get('input_tokens')), count(u.get('output_tokens'))
        read, write = count(u.get('cache_read_input_tokens')) or 0, count(u.get('cache_creation_input_tokens')) or 0
        if inp is not None:
            inp += read + write
        detail = u.get('cache_creation') or {}
        hour = count(detail.get('ephemeral_1h_input_tokens')) or 0 if isinstance(detail, dict) else 0
    elif provider == 'gemini':
        inp, out = count(u.get('promptTokenCount')), count(u.get('candidatesTokenCount'))
        if inp is not None:
            inp += count(u.get('toolUsePromptTokenCount')) or 0
        if out is not None:
            out += count(u.get('thoughtsTokenCount')) or 0
        read = u.get('cachedContentTokenCount', 0)
    actual = data.get('modelVersion' if provider == 'gemini' else 'model')
    # Only accept model-like metadata; never persist arbitrary response content.
    actual = actual if isinstance(actual, str) and model_key(actual) in RATES else model
    store.record(provider, actual, inp, out, read, write, purpose=purpose, write_1h=hour,
                 reported=u.get('cost') if provider == 'openrouter' else None, basis='reported')


def record_cache(provider, model):
    scope = _SCOPE.get()
    if scope:
        scope[0].record(provider, model, 0, 0, purpose=scope[1], reported=0, basis='local cache', cache_hit=True)


def record_agent(name, stdout):
    scope = _SCOPE.get()
    if not scope:
        return
    store, purpose = scope
    if isinstance(stdout, bytes):
        stdout = stdout.decode(errors='replace')
    if name == 'codex':
        found = False
        for line in (stdout or '').splitlines():
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict) or event.get('type') != 'turn.completed':
                continue
            u = event.get('usage') or {}
            if not isinstance(u, dict):
                u = {}
            store.record('codex', 'codex-default', u.get('input_tokens'), u.get('output_tokens'),
                         u.get('cached_input_tokens', 0), u.get('cache_write_input_tokens', 0),
                         channel='subscription', purpose=purpose)
            found = True
        if not found:
            store.record('codex', 'codex-default', channel='subscription', purpose=purpose)
    else:
        try:
            data = json.loads(stdout or '')
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        models = data.get('modelUsage')
        if isinstance(models, dict) and models:
            for model, u in models.items():
                if not isinstance(u, dict):
                    continue
                model = model if isinstance(model, str) and re.fullmatch(r'claude-[a-z0-9.-]{1,100}', model) else 'unreported model'
                inp = count(u.get('inputTokens'))
                read, write = count(u.get('cacheReadInputTokens')) or 0, count(u.get('cacheCreationInputTokens')) or 0
                store.record('claude', model, None if inp is None else inp + read + write,
                             u.get('outputTokens'), read, write, channel='subscription', purpose=purpose,
                             reported=u.get('costUSD'), basis='CLI estimate')
        else:
            u = data.get('usage') or {}
            if not isinstance(u, dict):
                u = {}
            inp = count(u.get('input_tokens'))
            read, write = count(u.get('cache_read_input_tokens')) or 0, count(u.get('cache_creation_input_tokens')) or 0
            store.record('claude', 'sonnet (unreported model)', None if inp is None else inp + read + write,
                         u.get('output_tokens'), read, write, channel='subscription', purpose=purpose,
                         reported=data.get('total_cost_usd'), basis='CLI estimate')
