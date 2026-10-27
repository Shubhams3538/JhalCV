"""Validate two-column company CSV files without fetching sites or using AI."""
import csv
import io
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .fetch import check_url

MAX_ROWS = 500
MAX_CSV_BYTES = 500_000


def name_key(value):
    return ' '.join(value.split()).casefold()


def url_key(value):
    parts = urlsplit(value)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/'),
                       urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True))), ''))


def validate_entry(company, url):
    if not isinstance(company, str) or not isinstance(url, str):
        raise ValueError('Company and URL must be text.')
    company = ' '.join(company.split())
    if not company or len(company) > 100:
        raise ValueError('Company name must contain 1–100 characters.')
    return {'company': company, 'url': check_url(url)}


def preview_csv(content, existing):
    if not isinstance(content, str) or len(content.encode('utf-8')) > MAX_CSV_BYTES:
        raise ValueError('Choose a CSV file under 500 KB.')
    try:
        reader = csv.reader(io.StringIO(content.lstrip('\ufeff'), newline=''), strict=True)
        header = next(reader, [])
        aliases = {'company': 'company', 'name': 'company', 'url': 'url', 'link': 'url'}
        columns = [aliases.get(value.strip().casefold()) for value in header]
        if len(columns) != 2 or set(columns) != {'company', 'url'}:
            raise ValueError('Use two columns named company,url (or name,link).')
        names = {name_key(s['name']) for s in existing}
        urls = {url_key(s['url']) for s in existing}
        rows = []
        for values in reader:
            if not any(value.strip() for value in values):
                continue
            if len(rows) >= MAX_ROWS:
                raise ValueError(f'Import at most {MAX_ROWS} companies at a time.')
            row = {'row': reader.line_num, 'company': '', 'url': '', 'status': 'invalid', 'message': ''}
            try:
                if len(values) != 2:
                    raise ValueError('This row must contain exactly two values.')
                entry = validate_entry(**dict(zip(columns, values)))
                row.update(entry)
                if name_key(entry['company']) in names or url_key(entry['url']) in urls:
                    row.update(status='duplicate', message='Company or careers URL is already in the app or this file.')
                else:
                    row['status'] = 'ready'
                    names.add(name_key(entry['company']))
                    urls.add(url_key(entry['url']))
            except ValueError as error:
                row['company'] = row['company'] or (values[columns.index('company')] if len(values) > columns.index('company') else '')[:100]
                row['message'] = str(error)
            rows.append(row)
    except csv.Error:
        raise ValueError('This CSV has broken quoting. Export it as CSV UTF-8 and try again.') from None
    if not rows:
        raise ValueError('The CSV contains no companies.')
    return {'rows': rows, 'ready': sum(row['status'] == 'ready' for row in rows),
            'duplicates': sum(row['status'] == 'duplicate' for row in rows),
            'invalid': sum(row['status'] == 'invalid' for row in rows)}
