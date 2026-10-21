"""Loopback-only local app. No hosting account or Python packages required."""
from __future__ import annotations

import argparse
import copy
import time
import json
import secrets
import shutil
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .llm import have_key
from .storage import Store, text
from .github import fetch_repository
from .descriptions import import_project, rewrite_projects, budget
from .tailor import tailor
from .workspace import build_preview, export_preview
from .render import RenderError

ROOT = Path(__file__).resolve().parent.parent
MAX_BODY = 2_000_000


class App:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.store = Store(root)
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.drafts = {}

    def state(self):
        return {'profile': self.store.profile(), 'settings': self.store.settings(),
                'example': not self.store.profile_path.exists(), 'ai_available': have_key(),
                'dependencies': {cmd: bool(shutil.which(cmd)) for cmd in ('typst', 'pdfinfo', 'pdftotext')},
                'token': self.token}

    def post(self, path, body):
        with self.lock:
            if path == '/api/profile':
                self.store.save_profile(body)
                return {'saved': True}
            if path == '/api/settings':
                return self.store.save_settings(body)
            if path == '/api/import':
                jd = text(body.get('jd', ''), 'Job description', 30000)
                return import_project(fetch_repository(body.get('url')), jd, self.store.settings())
            if path == '/api/tailor':
                return self.create_draft(body)
            if path in ('/api/preview', '/api/export'):
                draft = self.get_draft(body.get('id'))
                if path == '/api/preview':
                    missing = [cmd for cmd in ('typst', 'pdfinfo', 'pdftotext') if not shutil.which(cmd)]
                    if missing:
                        raise ValueError('Install the PDF tools first: ' + ', '.join(missing) + '. See README setup.')
                    try:
                        return build_preview(draft, body.get('projects'), self.root, body['id'])
                    except RenderError as e:
                        raise ValueError('Could not compile the resume: ' + str(e)) from e
                return export_preview(draft, body.get('revision'), self.store.settings())
            raise ValueError('Unknown action.')


    def get_draft(self, ident):
        draft = self.drafts.get(ident) if isinstance(ident, str) else None
        if not draft or time.monotonic() - draft['created'] > 7200:
            raise ValueError('This draft expired. Create a new tailored draft.')
        return draft

    def create_draft(self, body):
        jd = text(body.get('jd'), 'Job description', 30000, True)
        if len(jd) < 40:
            raise ValueError('Paste a job description of at least 40 characters.')
        if not self.store.profile_path.exists():
            raise ValueError('Save your own profile before creating a resume.')
        profile = self.store.profile()
        chosen = body.get('projects')
        available = {p['id'] for p in profile['projects']}
        if not isinstance(chosen, list) or not chosen or any(not isinstance(x, str) or x not in available for x in chosen):
            raise ValueError('Choose at least one project from your library.')
        profile['projects'] = [p for p in profile['projects'] if p['id'] in chosen]
        settings = self.store.settings()
        if settings['tailorer'] == 'rules':
            data, report = tailor(profile, jd)
            report['tailorer'] = 'rules'
        else:
            from .llm_tailor import tailor_llm
            data, report = tailor_llm(profile, jd, model=settings['model'])
        rewritten, notice = rewrite_projects(data, jd, settings)
        if report.get('tailorer') == 'rules' and settings['tailorer'] == 'auto':
            notice += ' Project selection used the rules fallback.'
        now = time.monotonic()
        self.drafts = {k: v for k, v in self.drafts.items() if now - v['created'] < 7200}
        while len(self.drafts) >= 10:
            self.drafts.pop(next(iter(self.drafts)))
        ident = secrets.token_urlsafe(18)
        self.drafts[ident] = {'created': now, 'source': copy.deepcopy(data), 'data': rewritten,
                              'jd': jd, 'report': report, 'notice': notice}
        return {'id': ident, 'notice': notice, 'gaps': report.get('gap_skills', []),
                'projects': [{'name': p['name'], 'bullets': p['bullets'],
                              'budgets': [budget(b) for b in original['bullets']]}
                             for p, original in zip(rewritten['projects'], data['projects'])]}


def handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Do not log job descriptions, tokens, or personal profile data.
            pass

        def reply(self, status, data, content_type='application/json; charset=utf-8'):
            payload = json.dumps(data, ensure_ascii=False).encode() if content_type.startswith('application/json') else data
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-src 'self'; object-src 'self'; base-uri 'none'; frame-ancestors 'self'")
            self.end_headers()
            self.wfile.write(payload)

        def trusted_host(self):
            return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

        def do_GET(self):
            if not self.trusted_host():
                return self.reply(403, {'error': 'Use the local app URL printed in your terminal.'})
            path = urlsplit(self.path).path
            if path == '/api/state':
                try:
                    with app.lock:
                        return self.reply(200, app.state())
                except (ValueError, OSError) as e:
                    return self.reply(400, {'error': str(e)})
            if path.startswith('/api/pdf/'):
                parts = path.split('/')
                try:
                    if len(parts) != 5:
                        raise ValueError('Invalid PDF URL.')
                    with app.lock:
                        draft = app.get_draft(parts[3])
                        preview = draft.get('preview')
                        if not preview or parts[4] != preview['revision']:
                            raise ValueError('This preview is out of date. Build a new preview.')
                        return self.reply(200, preview['path'].read_bytes(), 'application/pdf')
                except (ValueError, OSError) as e:
                    return self.reply(404, {'error': str(e)})
            files = {'/': ('index.html', 'text/html; charset=utf-8'),
                     '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                     '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if path in files:
                name, mime = files[path]
                return self.reply(200, (ROOT / 'web' / name).read_bytes(), mime)
            return self.reply(404, {'error': 'Not found.'})

        def do_POST(self):
            origin = f'http://127.0.0.1:{self.server.server_port}'
            if not self.trusted_host() or self.headers.get('Origin', origin) != origin or not secrets.compare_digest(self.headers.get('X-App-Token', ''), app.token):
                return self.reply(403, {'error': 'Reload the local app before trying again.'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_BODY:
                    return self.reply(413, {'error': 'Request must be under 2 MB.'})
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('Expected JSON.')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('Expected a JSON object.')
                return self.reply(200, app.post(urlsplit(self.path).path, body))
            except (ValueError, OSError) as e:
                return self.reply(400, {'error': str(e)})
            except Exception:
                return self.reply(500, {'error': 'The operation failed. Your saved profile is unchanged. Check your tools and settings and retry.'})
    return Handler


def main():
    parser = argparse.ArgumentParser(description='Launch your local resume workspace.')
    parser.add_argument('--port', type=int, default=0, help='Local port (default: choose an available port)')
    parser.add_argument('--no-browser', action='store_true', help='Print the URL without opening a browser')
    args = parser.parse_args()
    app = App()
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), handler(app))
    except OSError as e:
        parser.exit(1, f'Could not start the local app: {e}\n')
    server.daemon_threads = True
    url = f'http://127.0.0.1:{server.server_port}'
    print(f'JhalCV is running at {url}\nPress Ctrl+C to stop.', flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
    finally:
        server.server_close()
