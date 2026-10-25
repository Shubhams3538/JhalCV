"""HTTP helpers for career sites: public pages only, bounded size and time."""
from __future__ import annotations

import html
import ipaddress
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit

MAX_BYTES = 8_000_000
MAX_JSON_BYTES = 60_000_000  # whole job boards with descriptions can be large
TIMEOUT = 30
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/126.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}


class FetchError(ValueError):
    pass


def check_url(url: str) -> str:
    """Accept only public http(s) URLs: never localhost or private addresses."""
    if not isinstance(url, str) or len(url) > 3000:
        raise FetchError('Paste a careers page URL.')
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise FetchError('Use a full http(s) careers page URL.')
    host = parts.hostname.lower()
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
        raise FetchError('Only public career sites can be added.')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url
    if not ip.is_global:
        raise FetchError('Only public career sites can be added.')
    return url


def _open(request: urllib.request.Request, limit: int = MAX_BYTES) -> bytes:
    check_url(request.full_url)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            final = response.geturl()
            check_url(final)  # redirects must stay public too
            if re.search(r'maintenance|outage', urlsplit(final).path, re.I):
                raise FetchError('The site is down for maintenance. It will be retried on the next refresh.')
            data = response.read(limit + 1)
    except urllib.error.HTTPError as e:
        raise FetchError(f'The site returned HTTP {e.code}.') from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError('Could not reach the site. Check the URL and your connection.') from None
    if len(data) > limit:
        raise FetchError('The page is too large to read.')
    return data


def get_text(url: str, headers: dict | None = None, limit: int = MAX_BYTES) -> str:
    request = urllib.request.Request(url, headers={**HEADERS, **(headers or {})})
    return _open(request, limit).decode('utf-8', errors='replace')


def get_json(url: str):
    try:
        return json.loads(get_text(url, {'Accept': 'application/json'}, MAX_JSON_BYTES))
    except json.JSONDecodeError:
        raise FetchError('The site did not return readable job data.') from None


def post_json(url: str, body: dict):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                     headers={**HEADERS, 'Accept': 'application/json',
                                              'Content-Type': 'application/json'})
    try:
        return json.loads(_open(request, MAX_JSON_BYTES).decode('utf-8', errors='replace'))
    except json.JSONDecodeError:
        raise FetchError('The site did not return readable job data.') from None


_BLOCKS = re.compile(r'<(script|style|noscript|svg|template)\b[^>]*>.*?</\1\s*>', re.S | re.I)
_BREAKS = re.compile(r'<\s*(br|/p|/div|/li|/h[1-6]|/tr|li)\b[^>]*>', re.I)


def html_to_text(value: str) -> str:
    """Readable plain text from HTML: keeps line structure, drops markup."""
    if not value:
        return ''
    value = _BLOCKS.sub(' ', value)
    value = _BREAKS.sub('\n', value)
    value = re.sub(r'<[^>]+>', ' ', value)
    value = html.unescape(value)
    lines = [' '.join(line.split()) for line in value.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def page_with_links(value: str, base: str) -> tuple[str, set[str]]:
    """Page text in which every link keeps its absolute URL, plus the set of those URLs."""
    links = set()

    def anchor(m):
        href = html.unescape(m.group(1)).strip()
        if href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            return m.group(2)
        absolute = urljoin(base, href)
        links.add(absolute)
        return f'{m.group(2)} [link: {absolute}] '

    value = _BLOCKS.sub(' ', value)
    value = re.sub(r'<a\b[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a\s*>', anchor, value, flags=re.S | re.I)
    return html_to_text(value), links
