"""Local credentials. Public metadata deliberately contains no key fragments."""
from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path

from .storage import atomic_json, text

ROOT = Path(__file__).resolve().parents[1]
MASK = '••••••••'
PROVIDERS = {
    'openai': {'name': 'OpenAI', 'default_model': 'gpt-5.4-mini', 'env': 'OPENAI_API_KEY'},
    'gemini': {'name': 'Gemini', 'default_model': 'gemini-3.8-flash', 'env': 'GEMINI_API_KEY'},
    'anthropic': {'name': 'Anthropic', 'default_model': 'claude-sonnet-5-5', 'env': 'ANTHROPIC_API_KEY'},
    'openrouter': {'name': 'OpenRouter', 'default_model': 'openai/gpt-5.4-mini', 'env': 'OPENROUTER_API_KEY'},
    # The Z.AI GLM Coding Plan, through the same Anthropic-compatible endpoint ZCode uses.
    'zai': {'name': 'Z.AI Coding Plan (GLM)', 'default_model': 'GLM-5.3-Flash', 'env': 'ZAI_API_KEY'},
}

MODEL_CHOICES = {
    'openai': [('gpt-5.4-mini', 'GPT-5.4 Mini'), ('gpt-5.5', 'GPT-5.5'), ('gpt-4o-mini', 'GPT-4o Mini (legacy)')],
    'gemini': [('gemini-3.8-flash', 'Gemini 3.8 Flash'), ('gemini-3.5-flash-lite', 'Gemini 3.5 Flash-Lite')],
    'anthropic': [('claude-sonnet-5-5', 'Claude Sonnet 5.5'), ('claude-haiku-5-5', 'Claude Haiku 5.5'), ('claude-opus-5-5', 'Claude Opus 5.5')],
    'openrouter': [('openai/gpt-5.4-mini', 'OpenAI GPT-5.4 Mini'), ('openai/gpt-5.5', 'OpenAI GPT-5.5'),
                   ('anthropic/claude-sonnet-5.5', 'Claude Sonnet 5.5'), ('openai/gpt-4o-mini', 'OpenAI GPT-4o Mini (legacy)')],
    'zai': [('GLM-5.3-Flash', 'GLM-5.3 Flash'), ('GLM-5.3', 'GLM-5.3')],
}


def validate_key(value):
    if not isinstance(value, str):
        raise ValueError('Enter an API key.')
    key = value.strip()
    if not 8 <= len(key) <= 1024 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError('Enter a valid API key without spaces or line breaks.')
    if key.lower() in ('your-api-key', 'your_api_key', 'sk-placeholder') or '...' in key or '***' in key:
        raise ValueError('Enter the actual API key, not a placeholder.')
    return key


def validate_model(value):
    value = text(value, 'Model ID', 150, True)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]*', value) or '..' in value or '://' in value:
        raise ValueError('Enter the provider model ID, not a URL.')
    return value


class CredentialStore:
    def __init__(self, root=ROOT, *, include_env=True):
        self.path = Path(root) / '.local' / 'credentials.json'
        self.include_env = include_env

    def _read(self):
        if not self.path.exists():
            return {'keys': [], 'disabled_env': [], 'preferred': None, 'dismiss_reminder': False}
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(value, dict) or not isinstance(value.get('keys'), list):
                raise ValueError()
            return value
        except (ValueError, OSError) as e:
            raise ValueError('Could not read local API-key settings. Check .local/credentials.json.') from e

    def _write(self, value):
        atomic_json(self.path, value)  # mkstemp creates the credentials file with mode 0600.

    def routes(self, openai_model=None):
        saved = self._read()
        routes = []
        for item in saved['keys']:
            if item.get('provider') not in PROVIDERS:
                continue
            routes.append({k: item[k] for k in ('id', 'provider', 'label', 'model', 'key')} | {'source': 'saved'})
        if self.include_env:
            from .llm import load_env
            load_env()
            for provider, info in PROVIDERS.items():
                ident = 'env:' + provider
                if ident in saved.get('disabled_env', []):
                    continue
                try:
                    key = validate_key(os.environ.get(info['env'], ''))
                except ValueError:
                    continue
                if any(r['provider'] == provider and secrets.compare_digest(r['key'], key) for r in routes):
                    continue
                model = saved.get('env_models', {}).get(ident) or (openai_model if provider == 'openai' and openai_model else info['default_model'])
                routes.append({'id': ident, 'provider': provider, 'label': info['name'] + ' environment key',
                               'model': model, 'key': key, 'source': 'environment'})
        known_keys = [route['key'] for route in routes]
        for route in routes:
            if any(key in route['label'] for key in known_keys):
                route['label'] = PROVIDERS[route['provider']]['name'] + ' key'
            if any(key in route['model'] for key in known_keys):
                route['model'] = PROVIDERS[route['provider']]['default_model']
        preferred = saved.get('preferred')
        routes.sort(key=lambda item: item['id'] != preferred)
        return routes

    def public(self, openai_model=None):
        saved = self._read()
        routes = self.routes(openai_model)
        return {'providers': [{'id': ident, 'name': info['name'], 'default_model': info['default_model'],
                               'models': [{'id': model, 'name': name} for model, name in MODEL_CHOICES[ident]]}
                              for ident, info in PROVIDERS.items()],
                'keys': [{'id': route['id'], 'provider': route['provider'], 'label': route['label'],
                          'model': route['model'], 'mask': MASK, 'source': route['source'], 'first': i == 0}
                         for i, route in enumerate(routes)],
                'reminder_dismissed': bool(saved.get('dismiss_reminder', False)),
                'show_reminder': not routes and not saved.get('dismiss_reminder', False)}

    def add(self, body):
        provider = body.get('provider')
        if provider not in PROVIDERS:
            raise ValueError('Choose a supported AI provider.')
        key = validate_key(body.get('key'))
        model = validate_model(body.get('model') or PROVIDERS[provider]['default_model'])
        label = text(body.get('label') or PROVIDERS[provider]['name'] + ' key', 'Key label', 80, True)
        # Even accidentally pasting the credential into a display field must
        # never turn public metadata into a route for returning the secret.
        known_keys = [r['key'] for r in self.routes()] + [key]
        if any(secret in label or secret in model for secret in known_keys):
            raise ValueError('Keep API keys out of labels and model IDs.')
        saved = self._read()
        if len(saved['keys']) >= 20:
            raise ValueError('You can save up to 20 API keys. Remove an unused key first.')
        if any(r['provider'] == provider and secrets.compare_digest(r['key'], key) for r in self.routes()):
            raise ValueError('That key is already configured for this provider.')
        # A newly added secret may previously have been entered as another
        # entry's display text. Remove it from retained metadata as well.
        for item in saved['keys']:
            if key in item['label']:
                item['label'] = PROVIDERS[item['provider']]['name'] + ' key'
            if key in item['model']:
                item['model'] = PROVIDERS[item['provider']]['default_model']
        saved['keys'].append({'id': secrets.token_hex(12), 'provider': provider, 'label': label, 'model': model, 'key': key})
        self._write(saved)

    def remove(self, ident):
        ident = text(ident, 'Key ID', 100, True)
        saved = self._read()
        if ident.startswith('env:') and ident[4:] in PROVIDERS:
            saved['disabled_env'] = list(dict.fromkeys(saved.get('disabled_env', []) + [ident]))
        else:
            saved['keys'] = [r for r in saved['keys'] if r['id'] != ident]
        if saved.get('preferred') == ident:
            saved['preferred'] = None
        self._write(saved)

    def prefer(self, ident):
        if not isinstance(ident, str) or ident not in {r['id'] for r in self.routes()}:
            raise ValueError('Choose an existing API-key entry.')
        saved = self._read()
        saved['preferred'] = ident
        self._write(saved)

    def set_model(self, ident, model):
        routes = self.routes()
        if not isinstance(ident, str) or ident not in {r['id'] for r in routes}:
            raise ValueError('Choose an existing API-key entry.')
        model = validate_model(model)
        if any(r['key'] in model for r in routes):
            raise ValueError('Keep API keys out of model IDs.')
        saved = self._read()
        if ident.startswith('env:'):
            saved.setdefault('env_models', {})[ident] = model
        else:
            next(r for r in saved['keys'] if r['id'] == ident)['model'] = model
        self._write(saved)

    def reminder(self, dismissed):
        if type(dismissed) is not bool:
            raise ValueError('Choose whether to show the API-key reminder.')
        saved = self._read()
        saved['dismiss_reminder'] = dismissed
        self._write(saved)
