#!/usr/bin/env python3
"""Python side of the CPU micro benchmarks (see jhalCV_go/bench for Go).

    PYTHONPATH=. python3 bench/micro.py FIXTURES_DIR

Prints {name: {n, mean, median, min, max, stdev}} in milliseconds. Each
sample is the mean of a timed loop, like Go's ns/op.
"""
import json
import statistics
import sys
import time
from pathlib import Path

from resume.jobs.eligibility import requirement_years
from resume.jobs.fetch import html_to_text, page_with_links
from resume.storage import validate_profile
from resume.tailor import parse_jd, tailor

fixtures = Path(sys.argv[1])
page = (fixtures / 'page.html').read_text()
raw_profile = (fixtures / 'profile.json').read_text()
profile = validate_profile(json.loads(raw_profile))
jd = (fixtures / 'jd.txt').read_text()
posting = (fixtures / 'posting.txt').read_text()

CASES = {
    'HTMLToText': lambda: html_to_text(page),
    'PageWithLinks': lambda: page_with_links(page, 'https://example.com/careers'),
    'ParseProfile': lambda: validate_profile(json.loads(raw_profile)),
    'ParseJD': lambda: parse_jd(jd, profile),
    'Tailor': lambda: tailor(profile, jd),
    'RequirementYears': lambda: requirement_years(posting),
}


def sample(fn, budget=0.25):
    """Mean milliseconds per call over a loop lasting about budget seconds."""
    fn()
    loops, start = 0, time.perf_counter()
    while True:
        fn()
        loops += 1
        elapsed = time.perf_counter() - start
        if elapsed >= budget:
            return elapsed / loops * 1000


results = {}
for name, fn in CASES.items():
    runs = sorted(sample(fn) for _ in range(5))
    results[name] = {'n': len(runs), 'mean': statistics.fmean(runs), 'median': statistics.median(runs),
                     'min': runs[0], 'max': runs[-1], 'stdev': statistics.stdev(runs)}
print(json.dumps(results))
