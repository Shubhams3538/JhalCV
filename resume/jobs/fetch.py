"""HTTP helpers for career sites: public pages only, bounded size and time."""
from __future__ import annotations

import html
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit, urlunsplit

from .metrics import timed, measure

MAX_BYTES = 8_000_000
MAX_JSON_BYTES = 60_000_000  # whole job boards with descriptions can be large
TIMEOUT = 12
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/126.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}


class FetchError(ValueError):
    pass


_DEADLINE = ContextVar('job_fetch_deadline', default=None)
_BROWSERS = threading.BoundedSemaphore(4)


QUEUE_SECONDS = 600  # longest wait for a shared browser/AI slot


@contextmanager
def fetch_budget(deadline):
    # A one-item list so time spent queueing for a shared slot can be handed back.
    token = _DEADLINE.set(None if deadline is None else [deadline])
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def acquire_slot(semaphore, busy_message, stage=None):
    """Wait for a shared slot without charging the wait to the company's budget.

    Companies queue for a few browser and AI slots. Counting that queue against
    each company's time budget made companies fail before doing any work.
    """
    started = time.monotonic()
    try:
        with measure(stage or 'ai_queue'):
            got = semaphore.acquire(timeout=QUEUE_SECONDS)
            if not got:
                raise FetchError(busy_message)
    finally:
        box = _DEADLINE.get()
        if box is not None:
            box[0] += time.monotonic() - started


def current_deadline():
    box = _DEADLINE.get()
    return None if box is None else box[0]


def remaining(cap=TIMEOUT):
    box = _DEADLINE.get()
    seconds = min(cap, box[0] - time.monotonic()) if box is not None else cap
    if seconds <= 0:
        raise FetchError('Company time budget reached; remaining work will retry next refresh.')
    return max(.01, seconds)


_DNS_CACHE: dict[str, tuple[bool, float]] = {}
_DNS_TTL = 300.0


def _resolves_public(host: str) -> bool:
    """Whether every address the host answers with is public. Cached briefly:
    refreshes hit the same hosts many times, and the cached verdict also bounds
    how long a rebind can hide from re-checks."""
    cached = _DNS_CACHE.get(host)
    now = time.monotonic()
    if cached and cached[1] > now:
        return cached[0]
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True  # transient DNS failure fails open; the fetch still has to connect
    ok = True
    for info in infos:
        try:
            resolved = ipaddress.ip_address(info[4][0].split('%')[0])
        except ValueError:
            continue
        if not resolved.is_global:
            ok = False
            break
    _DNS_CACHE[host] = (ok, now + _DNS_TTL)
    return ok


def check_url(url: str) -> str:
    """Accept only public http(s) URLs: never localhost or private addresses.

    Returns the normalized URL, so control characters can never ride back into
    a request. Hostnames are resolved too: a name answering with a private
    address is rejected, while a transient DNS failure fails open because the
    fetch itself still has to reach the site.
    """
    if not isinstance(url, str) or len(url) > 3000:
        raise FetchError('Paste a careers page URL.')
    parts = urlsplit(url.strip())
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise FetchError('Use a full http(s) careers page URL.')
    host = parts.hostname.lower()
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
        raise FetchError('Only public career sites can be added.')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if not _resolves_public(host):
            raise FetchError('Only public career sites can be added.') from None
    else:
        if not ip.is_global:
            raise FetchError('Only public career sites can be added.')
    return urlunsplit(parts)


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must stay on the public web, checked before it is followed.
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_GuardedRedirects())


@timed('http')
def _open(request: urllib.request.Request, limit: int = MAX_BYTES) -> bytes:
    check_url(request.full_url)
    try:
        with _OPENER.open(request, timeout=remaining()) as response:
            final = response.geturl()
            check_url(final)  # redirects must stay public too
            if re.search(r'maintenance|outage', urlsplit(final).path, re.I):
                raise FetchError('The site is down for maintenance. It will be retried on the next refresh.')
            data = response.read(limit + 1)
    except urllib.error.HTTPError as e:
        # Several career sites answer urllib with 403/406/429 while serving a
        # browser-shaped client normally; one curl attempt covers both cases.
        try:
            return _curl(request, limit)
        except FetchError:
            raise FetchError(f'The site returned HTTP {e.code}.') from None
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as e:
        # Bot filters drop urllib's TLS handshake (a handshake timeout, not a
        # refused connection). curl negotiates HTTP/2 like a real browser.
        try:
            return _curl(request, limit)
        except FetchError:
            raise FetchError('Could not reach the site. Check the URL and your connection.') from None
    if len(data) > limit:
        raise FetchError('The page is too large to read.')
    return data


def _curl(request: urllib.request.Request, limit: int) -> bytes:
    """Fetch through curl: a browser-grade TLS/HTTP2 handshake that plain
    urllib cannot produce, which career-site bot filters check."""
    exe = shutil.which('curl')
    if not exe:
        raise FetchError('curl unavailable')
    seconds = remaining()
    args = [exe, '-sS', '-L', '--proto-redir', '=https', '--http2', '--compressed', '-m', str(seconds),
            '--max-filesize', str(limit + 1),
            '-w', '\nJHALCV_META %{http_code} %{url_effective}']
    for key, value in request.header_items():
        if key.lower() not in ('accept-encoding', 'user-agent', 'content-length', 'host'):
            args += ['-H', f'{key}: {value}']
    if request.data:
        args += ['-X', request.get_method(), '--data-binary', '@-']
    try:
        proc = subprocess.run(args + [request.full_url], input=request.data, capture_output=True,
                              timeout=seconds + 2)
    except subprocess.TimeoutExpired:
        raise FetchError('curl timed out') from None
    except OSError:
        raise FetchError('curl could not run') from None
    body, sep, meta = proc.stdout.rpartition(b'\nJHALCV_META ')
    if proc.returncode == 63 or len(body) > limit:
        raise FetchError('The page is too large to read.')
    if proc.returncode != 0 or not sep:
        raise FetchError(f'curl exit {proc.returncode}')
    code, _, final = meta.partition(b' ')
    check_url(final.decode('utf-8', 'replace').strip())
    if re.search(r'maintenance|outage', urlsplit(final.decode('utf-8', 'replace')).path, re.I):
        raise FetchError('The site is down for maintenance. It will be retried on the next refresh.')
    status = int(code or 0)
    if status >= 400:
        raise FetchError(f'HTTP {status}')
    return body


def get_text(url: str, headers: dict | None = None, limit: int = MAX_BYTES) -> str:
    request = urllib.request.Request(url, headers={**HEADERS, **(headers or {})})
    return _open(request, limit).decode('utf-8', errors='replace')


CHROME_CANDIDATES = (
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
    'google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'msedge',
    os.path.expandvars(r'%ProgramFiles%\Google\Chrome\Application\chrome.exe'),
    os.path.expandvars(r'%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe'),
)
RENDER_TIMEOUT = 80


def browser() -> str | None:
    """An installed Chrome-family browser that can render pages headlessly."""
    for candidate in CHROME_CANDIDATES:
        found = candidate if os.path.isfile(candidate) else shutil.which(candidate)
        if found:
            return found
    return None


def render_page(url: str) -> str:
    """The page as a browser sees it after its JavaScript has run.

    Many career sites (Meta, for example) load their listings with JavaScript
    or reject plain requests. A headless browser with a throwaway profile
    reads them the way the user's own browser does.
    """
    check_url(url)
    chrome = browser()
    if not chrome:
        raise FetchError('This page needs a browser to load its jobs. Install Google Chrome and refresh.')
    remaining(RENDER_TIMEOUT)
    acquire_slot(_BROWSERS, 'Browser busy; this company will retry next refresh.', 'browser_queue')
    try:
        return _render_page(chrome, url)
    finally:
        _BROWSERS.release()


@timed('browser')
def _render_page(chrome, url):
    with tempfile.TemporaryDirectory(prefix='jhalcv-chrome-') as profile:
        try:
            proc = subprocess.run(
                [chrome, '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
                 '--disable-extensions', '--mute-audio', f'--user-data-dir={profile}',
                 # The default headless agent says "HeadlessChrome", which bot
                 # filters (Cloudflare, Uber, SAP, Akamai) reject before any
                 # job HTML is served; send the agent a real tab would, and
                 # hide the automation flag headless mode otherwise sets.
                 f'--user-agent={HEADERS["User-Agent"]}',
                 '--disable-blink-features=AutomationControlled',
                 # Bound the load so a page that keeps polling analytics still
                 # dumps its DOM instead of hanging until the hard timeout.
                 '--virtual-time-budget=9000', '--timeout=60000', '--dump-dom', url],
                capture_output=True, timeout=remaining(RENDER_TIMEOUT))
        except subprocess.TimeoutExpired:
            raise FetchError('The page took too long to load in the browser.') from None
        except OSError:
            raise FetchError('Could not start Chrome to load this page.') from None
    page = proc.stdout[:MAX_BYTES].decode('utf-8', errors='replace')
    if proc.returncode != 0 or len(page) < 500:
        raise FetchError('Chrome could not load this page.')
    return page


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


# Per-candidate-start scanning lets a crafted page pin a worker's CPU: every
# unclosed tag rescans to the end of the page. Elements, tags and anchors are
# therefore removed with single forward passes.
_BLOCKS_OPEN = re.compile(r'<(script|style|noscript|svg|template)\b[^>]*>', re.I)
_BLOCK_CLOSE = {name: re.compile(r'</' + name + r'\s*>', re.I)
                for name in ('script', 'style', 'noscript', 'svg', 'template')}
_ANCHOR_OPEN = re.compile(r'<a\b[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>', re.I)
_ANCHOR_CLOSE = re.compile(r'</a\s*>', re.I)
_BREAKS = re.compile(r'<\s*(br|/p|/div|/li|/h[1-6]|/tr|li)\b[^>]*>', re.I)


def _strip_blocks(value: str) -> str:
    """Drop script/style/... elements in one forward pass. Like the old regex,
    an element with no closing tag keeps its content."""
    out, pos = [], 0
    for m in _BLOCKS_OPEN.finditer(value):
        if m.start() < pos:
            continue
        close = _BLOCK_CLOSE[m.group(1).lower()].search(value, m.end())
        if close is None:
            continue
        out.append(value[pos:m.start()])
        pos = close.end()
    out.append(value[pos:])
    return ''.join(out)


def _strip_tags(value: str) -> str:
    """Remove tags in one pass: no tag can cross the last '>' in the text."""
    last = value.rfind('>')
    if last < 0:
        return value
    return re.sub(r'<[^>]+>', ' ', value[:last + 1]) + value[last + 1:]


def html_to_text(value: str) -> str:
    """Readable plain text from HTML: keeps line structure, drops markup."""
    if not value:
        return ''
    value = _strip_blocks(value)
    value = _BREAKS.sub('\n', value)
    value = _strip_tags(value)
    value = html.unescape(value)
    lines = [' '.join(line.split()) for line in value.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def page_with_links(value: str, base: str) -> tuple[str, set[str]]:
    """Page text in which every link keeps its absolute URL, plus the set of those URLs."""
    links = set()
    value = _strip_blocks(value)
    out, pos = [], 0
    for m in _ANCHOR_OPEN.finditer(value):
        close = _ANCHOR_CLOSE.search(value, m.end())
        if close is None:
            continue
        href = html.unescape(m.group(1)).strip()
        if href.startswith(('#', 'javascript:', 'mailto:', 'tel:', 'data:')):
            continue
        absolute = urljoin(base, href)
        links.add(absolute)
        out.append(value[pos:m.start()])
        out.append(f'{value[m.end():close.start()]} [link: {absolute}] ')
        pos = close.end()
    out.append(value[pos:])
    return html_to_text(''.join(out)), links
