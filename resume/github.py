"""Read repository evidence from GitHub's API, never from arbitrary URLs."""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .llm import load_env


class GitHubError(ValueError):
    pass


def repository(value):
    if not isinstance(value, str):
        raise GitHubError('Paste a GitHub repository URL.')
    try:
        url = urlsplit(value.strip())
        parts = url.path.strip('/').split('/')
        port = url.port
    except ValueError as e:
        raise GitHubError('Invalid repository URL.') from e
    if url.scheme != 'https' or url.hostname != 'github.com' or port or url.username or url.password or url.query or url.fragment or len(parts) != 2:
        raise GitHubError('Use a repository URL such as https://github.com/owner/repo (no branch or file path).')
    owner, repo = parts
    repo = repo.removesuffix('.git') if hasattr(repo, 'removesuffix') else repo[:-4] if repo.endswith('.git') else repo
    if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})', owner) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', repo) or repo in ('.', '..'):
        raise GitHubError('Invalid GitHub owner or repository name.')
    return owner, repo


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # A renamed repository should be entered using its new canonical URL.
    # Never forward an optional token to a redirect destination.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(path):
    load_env()
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'JhalCV-local', 'X-GitHub-Api-Version': '2022-11-28'}
    token = os.environ.get('GITHUB_TOKEN', '').strip()
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request('https://api.github.com' + path, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response:
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise GitHubError('This repository response is too large. Add the project manually.')
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise GitHubError('Repository or README not found. Check the URL; private repositories need GITHUB_TOKEN in .env.') from e
        if e.code in (401, 403, 429):
            raise GitHubError('GitHub denied the request or its rate limit was reached. Check GITHUB_TOKEN or try later.') from e
        if e.code in (301, 302, 307, 308):
            raise GitHubError('This repository moved. Paste its current GitHub URL.') from e
        raise GitHubError(f'GitHub returned HTTP {e.code}. Try again later.') from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise GitHubError('Could not reach GitHub. Check your connection and try again.') from e


def fetch_repository(url):
    owner, repo = repository(url)
    metadata = request(f'/repos/{owner}/{repo}')
    readme = request(f'/repos/{owner}/{repo}/readme')
    if readme.get('encoding') != 'base64':
        raise GitHubError('GitHub did not return readable README content. Add this project manually.')
    try:
        content = base64.b64decode(readme['content']).decode('utf-8', errors='replace')
    except (KeyError, ValueError, binascii.Error) as e:
        raise GitHubError('Could not decode this README.') from e
    if not content.strip():
        raise GitHubError('The README is empty. Add the project manually with confirmed facts.')
    return {'name': metadata['name'], 'url': f'https://github.com/{owner}/{repo}',
            'description': metadata.get('description') or '', 'language': metadata.get('language'),
            'readme': content[:30000], 'truncated': len(content) > 30000}
