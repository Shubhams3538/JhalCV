"""Find the hiring platform behind a company careers page.

Most careers pages are a thin layer over a hiring platform (Greenhouse, Lever,
Ashby, Workday, SmartRecruiters) whose public feed lists every job with no
browser and no AI. Reading the feed directly is seconds instead of minutes, so
a company whose page reveals its platform is upgraded to that feed.

Two kinds of evidence are accepted:
- a platform link found on the company's own page (trusted: the company put it there);
- a guessed board name that the platform confirms belongs to this company.
"""
from __future__ import annotations

import html
import re
from datetime import date, timedelta
from urllib.parse import unquote, urlsplit

from .fetch import FetchError, get_json, get_text, render_page

_SKIP = {'embed', 'v1', 'v0', 'jobs', 'job', 'api', 'js', 'job_board', 'careers', 'posting-api', 'en', 'www'}

PATTERNS = [
    ('greenhouse', r'(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/embed/job_board(?:/js)?\?(?:[^"\'\s<>]*&(?:amp;)?)?for=([A-Za-z0-9_-]+)'),
    ('greenhouse', r'boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)'),
    ('greenhouse', r'(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/([A-Za-z0-9_-]+)'),
    ('lever', r'api(?:\.eu)?\.lever\.co/v0/postings/([A-Za-z0-9_.-]+)'),
    ('lever', r'jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_.-]+)'),
    ('ashby', r'api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.%-]+)'),
    ('ashby', r'jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)'),
    ('smartrecruiters', r'api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]+)'),
    ('smartrecruiters', r'(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)'),
]
_WORKDAY = re.compile(r'([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)')


def board_url(kind: str, token: str) -> str:
    return {'greenhouse': f'https://job-boards.greenhouse.io/{token}',
            'lever': f'https://jobs.lever.co/{token}',
            'ashby': f'https://jobs.ashbyhq.com/{token}',
            'smartrecruiters': f'https://careers.smartrecruiters.com/{token}'}[kind]


def found_in_page(page: str) -> list[str]:
    """Platform boards referenced anywhere in the page (links, scripts, data)."""
    text = html.unescape(unquote(page.replace('\\/', '/')))
    out = []
    for kind, pattern in PATTERNS:
        for token in re.findall(pattern, text):
            if token.lower() not in _SKIP and not token.lower().endswith(('.js', '.css', '.png')):
                url = board_url(kind, token)
                if url not in out:
                    out.append(url)
    for tenant, pod, site in _WORKDAY.findall(text):
        if site.lower() not in _SKIP and site.lower() not in ('wday', 'cxs'):
            url = f'https://{tenant}.{pod}.myworkdayjobs.com/{site}'
            if url not in out:
                out.append(url)
    return out


def _norm(value: str) -> str:
    return re.sub(r'[^a-z0-9]', '', value.lower())


def guesses(name: str, url: str) -> list[str]:
    host = (urlsplit(url).hostname or '').lower()
    labels = [l for l in host.split('.') if l not in ('www', 'careers', 'jobs', 'career', 'com', 'in', 'io', 'ai', 'co', 'net', 'org')]
    words = re.findall(r'[a-z0-9]+', name.lower().split('/')[0])
    out = []
    for value in [''.join(words), *(labels[:1]), words[0] if words else '', '-'.join(words)]:
        if value and len(value) >= 3 and value not in out:
            out.append(value)
    return out


RECENT_DAYS = 60


def _board(kind: str, token: str) -> tuple[int, str, str]:
    """(job count, platform's own company name, newest posting date) for a board."""
    from .sources import _date
    try:
        if kind == 'greenhouse':
            meta = get_json(f'https://boards-api.greenhouse.io/v1/boards/{token}')
            jobs = get_json(f'https://boards-api.greenhouse.io/v1/boards/{token}/jobs').get('jobs') or []
            dates = [_date(j.get('first_published') or j.get('updated_at')) for j in jobs]
            return len(jobs), str(meta.get('name') or ''), max(dates, default='')
        if kind == 'lever':
            jobs = get_json(f'https://api.lever.co/v0/postings/{token}?mode=json') or []
            return len(jobs), '', max((_date(j.get('createdAt')) for j in jobs), default='')
        if kind == 'ashby':
            jobs = get_json(f'https://api.ashbyhq.com/posting-api/job-board/{token}').get('jobs') or []
            return len(jobs), '', max((_date(j.get('publishedAt')) for j in jobs), default='')
        if kind == 'smartrecruiters':
            data = get_json(f'https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100')
            jobs = data.get('content') or []
            names = {str((j.get('company') or {}).get('name') or '') for j in jobs}
            return (int(data.get('totalFound') or 0), names.pop() if len(names) == 1 else '',
                    max((_date(j.get('releasedDate')) for j in jobs), default=''))
    except (FetchError, ValueError, TypeError, AttributeError):
        pass
    return 0, '', ''


def _count(kind: str, token: str) -> tuple[int, str]:
    """(job count, board name) for a live board; (0, '') when absent or abandoned.

    Test and abandoned boards are common on these platforms (a single 2021
    "test" posting, say), so a board needs a posting from the last 60 days.
    """
    count, name, newest = _board(kind, token)
    if not count or not newest or newest < (date.today() - timedelta(days=RECENT_DAYS)).isoformat():
        return 0, ''
    return count, name


def _facet_values(facets, label):
    """(facet parameter, [value ids]) for a location facet naming `label`.

    Sites expose either a country value ("India") or only city values
    ("Pune, India"). A country value wins; otherwise every city value that names
    the country as a word ("Indiana" does not count) is selected.
    """
    from .sources import INDIA_PLACE
    word = INDIA_PLACE if label.casefold() == 'india' else re.compile(r'\b' + re.escape(label) + r'\b', re.I)
    for facet in facets or []:
        param = facet.get('facetParameter') or ''
        values = facet.get('values') or []
        exact = [v['id'] for v in values if str(v.get('descriptor') or '').casefold() == label.casefold() and v.get('id')]
        if exact and re.search('location|country', param, re.I):
            return param, exact[:1]
        cities = [v['id'] for v in values if word.search(str(v.get('descriptor') or '')) and v.get('id')]
        if cities and re.search('location|country', param, re.I):
            return param, cities
        for value in values:
            if value.get('values'):
                nested = _facet_values([value], label)
                if nested:
                    return nested
    return None


def workday_with_country(url: str, country: str = 'India') -> tuple[str, int] | None:
    """The Workday search URL with the site's own country filter applied, and its job count."""
    from .fetch import post_json
    from .sources import _workday_base
    try:
        base, parts = _workday_base(url)
        data = post_json(base + '/jobs', {'appliedFacets': {}, 'limit': 1, 'offset': 0, 'searchText': ''})
        found = _facet_values(data.get('facets'), country)
        if not found or not found[0]:
            return None
        param, ids = found
        filtered = post_json(base + '/jobs', {'appliedFacets': {param: ids}, 'limit': 1, 'offset': 0, 'searchText': ''})
        query = '&'.join(f'{param}={ident}' for ident in ids)
        return f'https://{parts.hostname}{parts.path.rstrip("/")}?{query}', int(filtered.get('total') or 0)
    except (FetchError, ValueError, TypeError, AttributeError, KeyError):
        return None


def _kind(url: str) -> str:
    from .sources import detect
    return detect(url)


def oracle_from_page(page: str, country: str = 'India') -> tuple[str, int] | None:
    """An Oracle Recruiting search URL (with the country filter) referenced by the page."""
    hosts = re.findall(r'([a-z0-9-]+(?:\.[a-z0-9-]+)*\.oraclecloud\.com)', page)
    sites = re.findall(r'\b(CX_\d+)\b', page)
    for host in dict.fromkeys(hosts):
        for site in dict.fromkeys(sites):
            try:
                data = get_json(f'https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true'
                                f'&finder=findReqs;siteNumber={site},facetsList=LOCATIONS,limit=1')
            except FetchError:
                continue
            block = (data.get('items') or [{}])[0]
            place = next((l for l in block.get('locationsFacet') or [] if str(l.get('Name')) == country), None)
            if block.get('TotalJobsCount') and place:
                return (f'https://{host}/hcmUI/CandidateExperience/en/sites/{site}/jobs?locationId={place["Id"]}',
                        int(place.get('TotalCount') or 0))
    return None


def jibe_from_page(url: str, page: str) -> tuple[str, int] | None:
    """The careers site itself when it serves a Jibe /api/jobs feed."""
    if not re.search(r'jibe|icims', page, re.I):
        return None
    host = urlsplit(url).hostname
    try:
        data = get_json(f'https://{host}/api/jobs?limit=1')
    except FetchError:
        return None
    if isinstance(data, dict) and int(data.get('totalCount') or 0) and isinstance(data.get('jobs'), list):
        return f'https://{host}/jobs', int(data['totalCount'])
    return None


def resolve(name: str, url: str, *, render: bool = True, guess: bool = True, country: str | None = 'India') -> dict | None:
    """{'url', 'kind', 'jobs', 'evidence'} for the company's own board, or None."""
    page = ''
    try:
        page = get_text(url)
    except FetchError:
        pass
    for finder, kind in ((lambda: oracle_from_page(page, country) if country else None, 'oracle'), (lambda: jibe_from_page(url, page), 'jibe')):
        got = finder() if page else None
        if got and got[1]:
            return {'url': got[0], 'kind': kind, 'jobs': got[1], 'evidence': 'linked from the careers page'}
    hits = found_in_page(page)
    if not hits and render:
        try:
            hits = found_in_page(render_page(url))
        except FetchError:
            pass
    company = _norm(name.split('/')[0])
    for hit in hits:
        kind = _kind(hit)
        if kind == 'workday':
            filtered = workday_with_country(hit, country) if country else (hit, 1)
            if filtered:
                return {'url': filtered[0], 'kind': kind, 'jobs': filtered[1], 'evidence': 'linked from the careers page'}
            continue
        token = urlsplit(hit).path.strip('/').split('/')[0]
        count, _ = _count(kind, token)
        if count:
            return {'url': hit, 'kind': kind, 'jobs': count, 'evidence': 'linked from the careers page'}
    if not guess:
        return None
    for token in guesses(name, url):
        for kind in ('greenhouse', 'lever', 'ashby', 'smartrecruiters'):
            count, board_name = _count(kind, token)
            if not count:
                continue
            # A guessed board must clearly be this company's, not a namesake.
            # When the platform names the board, that name must match; otherwise
            # only an exact company-name token is trusted.
            if (_norm(board_name) in (company, _norm(name))) if board_name else _norm(token) == company:
                return {'url': board_url(kind, token), 'kind': kind, 'jobs': count, 'evidence': 'confirmed board name'}
    return None
