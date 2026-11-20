#!/usr/bin/env python3
"""Turn bench/results/<date>/results.json into a Markdown comparison.

    python3 bench/report.py bench/results/2026-11-27/results.json

Writes REPORT.md next to the results and prints it. The first
implementation in each row is the baseline for the "vs" column.
"""
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
results = json.loads(path.read_text())
impls = [i for i in ('python', 'go', 'rust') if any(i in (results.get(g) or {}) for g in results if g != 'machine')]
base = impls[0]
lines = []
out = lines.append


def ratio(value, baseline, higher_better, words=('faster', 'slower')):
    if not value or not baseline:
        return ''
    r = value / baseline if higher_better else baseline / value
    return f'{r:.1f}× {words[0]}' if r >= 1.05 else (f'{1 / r:.1f}× {words[1]}' if r <= 0.95 else 'same')


def table(title, unit, rows, higher_better=False, note='', words=('faster', 'slower')):
    """rows: [(label, {impl: value}) or (label, {impl: value}, unit, words)]"""
    out(f'\n### {title}\n')
    if note:
        out(note + '\n')
    header = '| | ' + ' | '.join(impls) + ' | ' + ' | '.join(f'{i} vs {base}' for i in impls[1:]) + ' |'
    out(header)
    out('|' + '---|' * (header.count('|') - 1))
    for row in rows:
        label, values = row[0], row[1]
        row_unit, row_words = (row[2], row[3]) if len(row) > 2 else (unit, words)
        cells = [f'{values[i]:,.2f} {row_unit}' if isinstance(values.get(i), (int, float)) else '–' for i in impls]
        vs = [ratio(values.get(i), values.get(base), higher_better, row_words) for i in impls[1:]]
        out(f'| {label} | ' + ' | '.join(cells) + ' | ' + ' | '.join(vs) + ' |')


m = results.get('machine', {})
out('# JhalCV benchmark: ' + ' vs '.join(impls))
out(f"\n{m.get('platform', '')}, {m.get('cores', '?')} cores, run {m.get('date', '')}. "
    'Every implementation used its own copy of the same workspace (real profile and job collection), '
    'in rules mode, so no AI calls are included.')

if 'startup' in results:
    r = results['startup']
    table('Cold start (launch → first HTTP response)', 'ms',
          [('median', {i: r[i]['seconds']['median'] * 1000 for i in r}),
           ('mean ± stdev', {i: r[i]['seconds']['mean'] * 1000 for i in r})])

if 'http' in results:
    r = results['http']
    names = {'progress': 'GET /api/jobs/progress (light)', 'state': 'GET /api/state (medium)', 'jobs': 'GET /api/jobs (heavy, all saved jobs)'}
    table('HTTP throughput, 8 concurrent clients', 'req/s',
          [(names[k], {i: r[i][k]['requests_per_second'] for i in r}) for k in names], higher_better=True)
    table('HTTP latency p99, 8 concurrent clients', 'ms',
          [(names[k], {i: r[i][k]['p99_ms'] for i in r}) for k in names])
    table('Server memory (RSS)', 'MB',
          [('idle', {i: r[i]['memory_mb']['idle'] for i in r}), ('after the load test', {i: r[i]['memory_mb']['after_load'] for i in r})],
          words=('less', 'more'),
          note="Go's garbage collector keeps freed heap for reuse instead of returning it to the OS at once.")

if 'tailor' in results:
    r = results['tailor']
    table('Resume tailoring through the API (rules, sequential)', 'ms',
          [('p50 per request', {i: r[i]['p50_ms'] for i in r}), ('p99 per request', {i: r[i]['p99_ms'] for i in r})])

if 'preview' in results:
    r = results['preview']
    table('One-page PDF preview build', 's', [('median', {i: r[i]['seconds']['median'] for i in r})],
          note='Dominated by Typst and Poppler (the same external tools for every implementation).')

if 'refresh' in results:
    r = results['refresh']
    table('Jobs refresh: 3 real companies', 's',
          [('median', {i: r[i]['seconds']['median'] for i in r}),
           ('peak memory', {i: r[i]['peak_mb'] for i in r}, 'MB', ('less', 'more'))],
          note='Network bound; implementations alternate each round. Summary: ' +
               '; '.join(f'{i}: “{r[i]["message"]}”' for i in r))

if 'micro' in results:
    r = results['micro']
    names = {'HTMLToText': 'HTML → text (1.4 MB page)', 'PageWithLinks': 'HTML → text with links',
             'ParseProfile': 'Parse + validate profile', 'ParseJD': 'Job description analysis (rules)',
             'Tailor': 'Full rules tailoring', 'RequirementYears': 'Read year requirements (400 lines)'}
    table('CPU micro benchmarks (median per call)', 'ms',
          [(names.get(k, k), {i: r[i][k]['median'] for i in r if k in r[i]}) for k in names if any(k in r[i] for i in r)])

if 'footprint' in results:
    r = results['footprint']
    out('\n### Footprint\n')
    out('| | ' + ' | '.join(impls) + ' |')
    out('|' + '---|' * (len(impls) + 1))
    for key, label in (('binary_mb', 'Deployable binary'), ('runtime', 'Runtime needed'), ('code_lines', 'Lines of code'),
                       ('test_lines', 'Lines of tests'), ('files', 'Source files'), ('dependencies', 'Third-party modules')):
        cells = []
        for i in impls:
            v = r.get(i, {}).get(key)
            cells.append('–' if v is None else f'{v:.1f} MB' if key == 'binary_mb' else f'{v:,}' if isinstance(v, int) else str(v))
        out(f'| {label} | ' + ' | '.join(cells) + ' |')

report = '\n'.join(lines) + '\n'
(path.parent / 'REPORT.md').write_text(report)
print(report)
