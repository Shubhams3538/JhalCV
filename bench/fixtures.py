#!/usr/bin/env python3
"""Write the identical inputs every implementation's micro benchmarks read.

    python3 bench/fixtures.py OUT_DIR

page.html    a 2 MB careers page with scripts, links and 600 job cards
profile.json the sample profile (profile.example.json)
jd.txt       a sample job description
posting.txt  a long job posting with many year requirements
"""
import random
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
rng = random.Random(7)  # Fixed seed: identical bytes on every run.

roles = ['Software Engineer', 'Backend Engineer', 'Data Engineer', 'SRE', 'Frontend Developer', 'ML Engineer']
cities = ['Bengaluru, India', 'Pune, India', 'Hyderabad, India', 'Remote', 'London, UK']
cards = []
for i in range(600):
    cards.append(f'''<li class="job"><a href="/jobs/{i}-{rng.choice(roles).lower().replace(' ', '-')}">
  <h3>{rng.choice(roles)} {['I', 'II', 'III'][i % 3]}</h3></a>
  <p class="loc">{rng.choice(cities)}</p><p>Posted {rng.randint(1, 30)} days ago &amp; hiring</p>
  <div><p>We build <b>distributed systems</b> in Go, Python and Rust.</p><br>
  <p>Requirements: {rng.randint(1, 8)}+ years of experience with APIs, Kubernetes &lt;and&gt; SQL.</p></div></li>''')
script = '<script>' + 'var data = {"k": [1,2,3]};' * 20000 + '</script>'
style = '<style>' + '.job{margin:0;padding:4px}' * 5000 + '</style>'
page = f'<html><head><title>Careers</title>{style}{script}</head><body><ul>{"".join(cards)}</ul>{script}</body></html>'
(out / 'page.html').write_text(page)

shutil.copy2(ROOT / 'profile.example.json', out / 'profile.json')
shutil.copy2(ROOT / 'jds' / 'backend_systems.txt', out / 'jd.txt')

lines = []
for i in range(400):
    n = rng.randint(1, 9)
    lines.append(rng.choice([
        f'- {n}+ years of professional experience building backend services',
        f'- Minimum {n} years in a similar role',
        f'- {n} to {n + 3} years of hands-on experience with Kubernetes',
        f'- Nice to have: {n} years of Rust',
        f'- Company founded {n * 4} years ago',
        '- Strong communication and ownership',
        'Preferred qualifications',
        'Basic qualifications',
    ]))
(out / 'posting.txt').write_text('\n'.join(lines))
