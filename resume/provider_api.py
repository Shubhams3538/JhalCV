"""Provider-specific request/response shapes. Secrets travel only in headers."""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request

from .llm import LLMError, LLMAuthError, strictify


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_output(value, schema):
    """Validate the subset of JSON Schema used by this app, before using/cache."""
    kind = schema.get('type')
    if isinstance(kind, list):
        if not any(valid_type(value, k) for k in kind):
            raise LLMError('Provider response has an invalid value type.')
    elif kind and not valid_type(value, kind):
        raise LLMError('Provider response has an invalid value type.')
    if 'enum' in schema and value not in schema['enum']:
        raise LLMError('Provider response contains an unsupported selection.')
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        if any(k not in value for k in schema.get('required', [])):
            raise LLMError('Provider response is missing required fields.')
        if schema.get('additionalProperties') is False and set(value) - set(properties):
            raise LLMError('Provider response contains unexpected fields.')
        for key, child in properties.items():
            if key in value:
                validate_output(value[key], child)
    if isinstance(value, list) and 'items' in schema:
        for child in value:
            validate_output(child, schema['items'])


def valid_type(value, kind):
    return {'object': isinstance(value, dict), 'array': isinstance(value, list),
            'string': isinstance(value, str), 'integer': type(value) is int,
            'number': type(value) is int or (type(value) is float and math.isfinite(value)),
            'boolean': type(value) is bool, 'null': value is None}.get(kind, False)


def request_shape(route, prompt, schema, system, temperature, schema_name):
    provider, model = route['provider'], route['model']
    headers = {'Content-Type': 'application/json'}
    schema = strictify(schema)
    if provider in ('openai', 'openrouter'):
        url = 'https://api.openai.com/v1/chat/completions' if provider == 'openai' else 'https://openrouter.ai/api/v1/chat/completions'
        headers['Authorization'] = 'Bearer ' + route['key']
        body = {'model': model, 'messages': ([{'role': 'system', 'content': system}] if system else []) + [{'role': 'user', 'content': prompt}],
                'temperature': temperature, 'response_format': {'type': 'json_schema', 'json_schema': {'name': schema_name, 'strict': True, 'schema': schema}}}
        if provider == 'openrouter':
            body['provider'] = {'require_parameters': True}
    elif provider == 'gemini':
        url = 'https://generativelanguage.googleapis.com/v1beta/models/' + urllib.parse.quote(model, safe='') + ':generateContent'
        headers['x-goog-api-key'] = route['key']
        body = {'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
                'generationConfig': {'temperature': temperature, 'responseMimeType': 'application/json', 'responseJsonSchema': schema}}
        if system:
            body['systemInstruction'] = {'parts': [{'text': system}]}
    elif provider == 'anthropic':
        url = 'https://api.anthropic.com/v1/messages'
        headers.update({'x-api-key': route['key'], 'anthropic-version': '2023-06-01'})
        body = {'model': model, 'max_tokens': 8192, 'messages': [{'role': 'user', 'content': prompt}],
                'output_config': {'format': {'type': 'json_schema', 'schema': schema}}}
        if system:
            body['system'] = system
    else:
        raise LLMError('Unsupported AI provider.')
    return url, headers, body


def send(url, headers, body, timeout):
    request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(), headers=headers, method='POST')
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise LLMError('Provider response exceeded the size limit.')
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        # Never expose response bodies or exception text: providers can echo
        # request headers, and neither reports nor browser errors may contain keys.
        unsupported_temperature = False
        if e.code == 400:
            detail = e.read(4000).decode(errors='replace').lower()
            unsupported_temperature = 'temperature' in detail and ('unsupported' in detail or 'not support' in detail)
        if e.code in (401, 403):
            raise LLMAuthError('Provider rejected this API key.', status=e.code) from None
        raise LLMError(f'Provider request failed (HTTP {e.code}).', status=e.code,
                       body='unsupported temperature' if unsupported_temperature else '') from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise LLMError('Provider connection failed or returned invalid JSON.') from None


def call(route, prompt, schema, system, temperature, schema_name, timeout):
    url, headers, body = request_shape(route, prompt, schema, system, temperature, schema_name)
    try:
        response = send(url, headers, body, timeout)
    except LLMError as e:
        if e.status == 400 and e.body == 'unsupported temperature' and 'temperature' in body:
            body.pop('temperature')
            response = send(url, headers, body, timeout)
        else:
            raise
    try:
        if route['provider'] in ('openai', 'openrouter'):
            choice = response['choices'][0]
            message = choice['message']
            if message.get('refusal') or choice.get('finish_reason') not in (None, 'stop'):
                raise LLMError('Provider refused or truncated this response.')
            content = message['content']
        elif route['provider'] == 'gemini':
            candidate = response['candidates'][0]
            if candidate.get('finishReason') not in (None, 'STOP'):
                raise LLMError('Provider refused or truncated this response.')
            content = ''.join(p['text'] for p in candidate['content']['parts'] if 'text' in p and not p.get('thought'))
        else:
            if response.get('stop_reason') not in (None, 'end_turn'):
                raise LLMError('Provider refused or truncated this response.')
            content = ''.join(p['text'] for p in response['content'] if p.get('type') == 'text')
        result = json.loads(content)
        validate_output(result, strictify(schema))
        return result
    except (KeyError, IndexError, TypeError, ValueError):
        raise LLMError('Provider returned an invalid structured response.') from None
