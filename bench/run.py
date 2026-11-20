#!/usr/bin/env python3
"""Benchmark JhalCV implementations side by side.

    python3 bench/run.py                 every benchmark, every implementation
    python3 bench/run.py --only http     one group (startup, http, tailor,
                                         preview, refresh, micro, footprint)
    python3 bench/run.py --impl go,python

Each implementation runs against its own disposable copy of this workspace
(your profile, settings and job collection), in rules mode, so nothing here
spends AI quota or changes your real files. Results go to
bench/results/<date>/results.json; bench/report.py turns them into a table.

Adding the Rust port later only needs an entry in IMPLEMENTATIONS.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / 'bench'

# How to build and start each implementation. {root} is its workspace and
# {port} its port. 'cwd' is where the server command runs.
IMPLEMENTATIONS = {
    'python': {
        'build': None,
        'server': [sys.executable, 'app.py', '--no-browser', '--port', '{port}'],
        'cwd': '{root}',
    },
    'go': {
        'build': ['go', 'build', '-C', str(ROOT / 'jhalCV_go'), '-trimpath', '-o', 'bin/jhalcv', './cmd/jhalcv'],
        'server': [str(ROOT / 'jhalCV_go' / 'bin' / 'jhalcv'), '--no-browser', '--port', '{port}', '--root', '{root}'],
        'cwd': '{root}',
        'binary': ROOT / 'jhalCV_go' / 'bin' / 'jhalcv',
        'sources': ROOT / 'jhalCV_go',
        'ext': '.go',
    },
}

# Direct-feed companies a refresh benchmark reads: real network, no AI.
REFRESH_SOURCES = ['https://job-boards.eu.greenhouse.io/groww', 'https://jobs.lever.co/100ms',
                   'https://adobe.wd5.myworkdayjobs.com/external_experienced?locationCountry=c4f78be1a8f14da0ab49ce1162348a5e']
JD = (ROOT / 'jds' / 'backend_systems.txt').read_text()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def make_workspace(parent: Path, name: str) -> Path:
    """A private copy of everything the apps read, set to rules mode."""
    ws = parent / name
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    for item in ('web', 'templates', 'fonts', 'data', 'resume', 'app.py', 'profile.json', 'profile.example.json'):
        src = ROOT / item
        if src.is_dir():
            shutil.copytree(src, ws / item)
        elif src.exists():
            shutil.copy2(src, ws / item)
    local = ws / '.local'
    local.mkdir()
    for item in ('settings.json', 'jobs.db', 'insights.json'):
        if (ROOT / '.local' / item).exists():
            shutil.copy2(ROOT / '.local' / item, local / item)
    settings = json.loads((local / 'settings.json').read_text()) if (local / 'settings.json').exists() else {
        'filename': 'Bench_Resume', 'model': 'gpt-5.5'}
    settings.update(tailorer='rules', output_dir=str(ws / 'out'))
    (local / 'settings.json').write_text(json.dumps(settings))
    (local / 'setup.json').write_text(json.dumps({'provider': 'rules', 'completed': True}))
    return ws


class Server:
    """One running implementation."""

    def __init__(self, impl: str, ws: Path):
        self.impl, self.ws, self.port = impl, ws, free_port()
        spec = IMPLEMENTATIONS[impl]
        fill = lambda v: v.format(root=ws, port=self.port)
        self.base = f'http://127.0.0.1:{self.port}'
        self.started = time.perf_counter()
        self.proc = subprocess.Popen([fill(a) for a in spec['server']], cwd=fill(spec['cwd']),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.ready_seconds = self.wait()
        self.token = self.get('/api/state')['token']

    def wait(self, timeout=30.0) -> float:
        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            try:
                urllib.request.urlopen(self.base + '/', timeout=1).read()
                return time.perf_counter() - self.started
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.005)
        raise RuntimeError(f'{self.impl} did not start')

    def get(self, path):
        return json.load(urllib.request.urlopen(self.base + path, timeout=120))

    def post(self, path, body, timeout=300):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method='POST',
                                     headers={'Content-Type': 'application/json', 'X-App-Token': self.token})
        try:
            return json.load(urllib.request.urlopen(req, timeout=timeout))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f'{self.impl} {path}: HTTP {e.code} {e.read()[:200]!r}') from None

    def rss_mb(self) -> float:
        out = subprocess.run(['ps', '-o', 'rss=', '-p', str(self.proc.pid)], capture_output=True, text=True).stdout
        return int(out.strip() or 0) / 1024

    def stop(self):
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            os.killpg(self.proc.pid, signal.SIGKILL)


def summary(samples):
    samples = sorted(samples)
    return {'n': len(samples), 'mean': statistics.fmean(samples), 'median': statistics.median(samples),
            'min': samples[0], 'max': samples[-1],
            'stdev': statistics.stdev(samples) if len(samples) > 1 else 0.0}


# ── benchmarks ──

def bench_startup(impl, tmp):
    """Process launch until the first HTTP response, via hyperfine."""
    ws = make_workspace(tmp, f'startup-{impl}')
    script = tmp / f'start-{impl}.sh'
    spec = IMPLEMENTATIONS[impl]
    port = free_port()
    cmd = ' '.join(f"'{a.format(root=ws, port=port)}'" for a in spec['server'])
    script.write_text(f"""#!/bin/sh
cd '{spec['cwd'].format(root=ws, port=port)}'
{cmd} >/dev/null 2>&1 &
pid=$!
until curl -s -o /dev/null http://127.0.0.1:{port}/; do :; done
kill $pid; wait $pid 2>/dev/null
exit 0
""")
    script.chmod(0o755)
    out = tmp / f'startup-{impl}.json'
    subprocess.run(['hyperfine', '--warmup', '2', '--runs', '15', '--export-json', str(out), '--style', 'none', str(script)],
                   check=True, stdout=subprocess.DEVNULL)
    result = json.loads(out.read_text())['results'][0]
    if 'times' in result:  # hyperfine 1.x
        times = result['times']
    else:  # hyperfine 2.x
        times = [m['time_wall_clock']['value'] for m in result['measurements']]
    return {'seconds': summary(times)}


def oha(url, *, n, c, method='GET', headers=(), body=None):
    cmd = ['oha', '-n', str(n), '-c', str(c), '--no-tui', '--output-format', 'json', '-m', method]
    for h in headers:
        cmd += ['-H', h]
    if body is not None:
        cmd += ['-d', body]
    data = json.loads(subprocess.run(cmd + [url], capture_output=True, text=True, check=True).stdout)
    s, p = data['summary'], data['latencyPercentiles']
    return {'requests_per_second': s['requestsPerSec'], 'success_rate': s['successRate'],
            'p50_ms': p['p50'] * 1000, 'p95_ms': p['p95'] * 1000, 'p99_ms': p['p99'] * 1000}


def bench_http(impl, tmp):
    """Read endpoints under concurrent load, and memory before and after."""
    server = Server(impl, make_workspace(tmp, f'http-{impl}'))
    try:
        idle = server.rss_mb()
        results = {}
        # Light (in-memory progress), medium (profile + settings + keys +
        # setup), heavy (every saved job, filtered and categorized).
        for name, path, n in (('progress', '/api/jobs/progress', 5000), ('state', '/api/state', 2000), ('jobs', '/api/jobs', 300)):
            oha(server.base + path, n=max(20, n // 10), c=8)  # warm-up
            results[name] = oha(server.base + path, n=n, c=8)
        results['memory_mb'] = {'idle': idle, 'after_load': server.rss_mb()}
        return results
    finally:
        server.stop()


def bench_tailor(impl, tmp):
    """Rules tailoring through the API, one request at a time (the app
    allows a single tailoring run at a time)."""
    server = Server(impl, make_workspace(tmp, f'tailor-{impl}'))
    try:
        body = json.dumps({'jd': JD})
        headers = ['Content-Type: application/json', f'X-App-Token: {server.token}']
        oha(server.base + '/api/tailor', n=10, c=1, method='POST', headers=headers, body=body)
        return oha(server.base + '/api/tailor', n=200, c=1, method='POST', headers=headers, body=body)
    finally:
        server.stop()


def bench_preview(impl, tmp):
    """Build a one-page PDF preview: mostly Typst and Poppler time, so it
    shows how much the language adds around external tools."""
    server = Server(impl, make_workspace(tmp, f'preview-{impl}'))
    try:
        times = []
        for i in range(8):
            draft = server.post('/api/tailor', {'jd': JD})
            edits = [{'bullets': p['bullets']} for p in draft['projects']]
            start = time.perf_counter()
            server.post('/api/preview', {'id': draft['id'], 'projects': edits})
            if i:  # The first build warms Typst's font cache.
                times.append(time.perf_counter() - start)
        return {'seconds': summary(times)}
    finally:
        server.stop()


def run_refresh(impl, tmp, label):
    ws = make_workspace(tmp, f'refresh-{impl}-{label}')
    server = Server(impl, ws)
    try:
        saved = {s['url'] for s in server.get('/api/jobs')['sources']}
        for i, url in enumerate(REFRESH_SOURCES):  # A fresh workspace gets them from the catalog.
            if url not in saved:
                server.post('/api/jobs/sources/add', {'name': f'Bench {i + 1}', 'url': url})
        ids = [s['id'] for s in server.get('/api/jobs')['sources'] if s['url'] in REFRESH_SOURCES]
        start = time.perf_counter()
        server.post('/api/jobs/refresh', {'sources': ids})
        peak = server.rss_mb()
        while True:
            progress = server.get('/api/jobs/progress')['progress']
            peak = max(peak, server.rss_mb())
            if not progress['running']:
                break
            time.sleep(0.05)
        return time.perf_counter() - start, peak, progress.get('message') or progress.get('error')
    finally:
        server.stop()


def bench_refresh(impls, tmp, rounds=3):
    """A real refresh of three direct-feed companies (network bound).
    Implementations alternate each round so network drift hits both."""
    results = {impl: {'times': [], 'peak_mb': []} for impl in impls}
    for r in range(rounds):
        for impl in (impls if r % 2 == 0 else list(reversed(impls))):
            seconds, peak, message = run_refresh(impl, tmp, str(r))
            results[impl]['times'].append(seconds)
            results[impl]['peak_mb'].append(peak)
            results[impl]['message'] = message
    return {impl: {'seconds': summary(v['times']), 'peak_mb': max(v['peak_mb']), 'message': v['message']}
            for impl, v in results.items()}


def bench_micro(impl, tmp):
    """Pure CPU work on identical inputs (bench/fixtures.py)."""
    fixtures = tmp / 'fixtures'
    subprocess.run([sys.executable, str(BENCH / 'fixtures.py'), str(fixtures)], check=True)
    if impl == 'python':
        out = subprocess.run([sys.executable, str(BENCH / 'micro.py'), str(fixtures)], cwd=ROOT,
                             capture_output=True, text=True, check=True, env={**os.environ, 'PYTHONPATH': str(ROOT)}).stdout
        return json.loads(out)
    if impl == 'go':
        out = subprocess.run(['go', 'test', '-run', '^$', '-bench', '.', '-benchmem', '-count', '5', './bench/'],
                             cwd=ROOT / 'jhalCV_go', capture_output=True, text=True, check=True,
                             env={**os.environ, 'JHALCV_FIXTURES': str(fixtures)}).stdout
        (tmp / 'go-micro.txt').write_text(out)
        runs = {}
        for line in out.splitlines():
            if line.startswith('Benchmark'):
                parts = line.split()
                name = parts[0].split('/')[0].removeprefix('Benchmark').split('-')[0]
                runs.setdefault(name, []).append(float(parts[2]) / 1e6)  # ns/op -> ms
        return {name: summary(v) for name, v in runs.items()}
    return {}


def count_lines(path: Path, ext: str) -> dict:
    files = [p for p in path.rglob('*' + ext) if 'bin' not in p.parts]
    code = sum(1 for f in files if not f.name.endswith('_test' + ext) for _ in f.open())
    tests = sum(1 for f in files if f.name.endswith('_test' + ext) for _ in f.open())
    return {'files': len(files), 'code_lines': code, 'test_lines': tests}


def bench_footprint(impl, tmp):
    spec = IMPLEMENTATIONS[impl]
    if impl == 'python':
        code = count_lines(ROOT / 'resume', '.py')
        tests = count_lines(ROOT / 'tests', '.py')
        return {'binary_mb': None, 'runtime': f'Python {platform.python_version()} interpreter',
                'files': code['files'], 'code_lines': code['code_lines'],
                'test_lines': tests['code_lines'] + sum(1 for _ in (ROOT / 'test_app.py').open()),
                'dependencies': 'none (standard library)'}
    out = {'binary_mb': spec['binary'].stat().st_size / 1e6, **count_lines(spec['sources'], spec['ext'])}
    if impl == 'go':
        # Modules actually compiled into the binary, not the whole module graph.
        info = subprocess.run(['go', 'version', '-m', str(spec['binary'])], capture_output=True, text=True).stdout
        deps = [line.split()[1] for line in info.splitlines() if line.strip().startswith('dep')]
        out['dependencies'] = f"{len(deps)} ({', '.join(deps)})"
        out['runtime'] = 'none (self-contained binary)'
    return out


GROUPS = ['startup', 'http', 'tailor', 'preview', 'refresh', 'micro', 'footprint']


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--only', help='comma-separated groups: ' + ', '.join(GROUPS))
    parser.add_argument('--impl', default=','.join(IMPLEMENTATIONS), help='comma-separated implementations')
    parser.add_argument('--refresh-rounds', type=int, default=3)
    args = parser.parse_args()
    groups = args.only.split(',') if args.only else GROUPS
    impls = args.impl.split(',')
    for tool in ('hyperfine', 'oha', 'curl'):
        if not shutil.which(tool):
            sys.exit(f'Install {tool} first (brew install {tool}).')
    for impl in impls:
        if IMPLEMENTATIONS[impl]['build']:
            print(f'building {impl}…', flush=True)
            subprocess.run(IMPLEMENTATIONS[impl]['build'], check=True)

    results = {'machine': {'platform': platform.platform(), 'cpu': platform.processor() or platform.machine(),
                           'cores': os.cpu_count(), 'date': datetime.now().isoformat(timespec='seconds')}}
    with tempfile.TemporaryDirectory(prefix='jhalcv-bench-') as tmp:
        tmp = Path(tmp)
        for group in groups:
            print(f'▸ {group}', flush=True)
            if group == 'refresh':
                results[group] = bench_refresh(impls, tmp, args.refresh_rounds)
                continue
            results[group] = {}
            for impl in impls:
                print(f'  {impl}', flush=True)
                results[group][impl] = globals()['bench_' + group](impl, tmp)

    folder = BENCH / 'results' / datetime.now().strftime('%Y-%m-%d')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'results.json'
    if path.exists():  # Merge, so groups can be run separately.
        previous = json.loads(path.read_text())
        for key, value in results.items():
            previous[key] = value if key == 'machine' else {**previous.get(key, {}), **value}
        results = previous
    path.write_text(json.dumps(results, indent=2))
    print(f'\nSaved {path.relative_to(ROOT)}. Report: python3 bench/report.py {path.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
