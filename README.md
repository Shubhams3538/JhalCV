# runtime_resume_maker

Paste a job description, get a tailored one-page PDF.

Your experience stays fixed. The projects and skills change per job — which
projects appear, how each is framed, which bullets show, and what order skills
sit in. An LLM makes those calls; it never writes a word that reaches the page.

## Setup

```bash
brew install typst poppler          # the only dependencies; no Python packages
cp profile.example.json profile.json
cp .env.example .env                # add OPENAI_API_KEY (optional)
```

## Run it

```bash
python3 main.py
```

Copy a job posting first and it offers your clipboard. Otherwise paste it and
press Enter.

```bash
python3 main.py jd.txt              # from a file
cat jd.txt | python3 main.py        # piped
python3 main.py --tailorer rules    # no API calls
```

The PDF lands in `build/` and opens automatically.

```
▸ tailoring stripe-platform.txt
  llm gpt-5.5 (called)
  reads as: Senior platform/backend infrastructure role focused on Go payment
            services, Kubernetes operations, and platform observability.
  angles: backend 40%, infra 35%, systems 25%
  project: multithreaded-web-server (systems) — direct concurrency evidence
  project: ai-interviewer (backend) — Postgres-backed API and validation
  ✓ all checks passed
▸ gaps: redis

→ build/2026-08-27_stripe-platform.pdf
```

That last line is the sleeper feature: what the job wants that your profile
can't back. It never touches the PDF — it tells you what to build next.

## Other commands

```bash
python3 -m resume compare --jd jds/x.txt    # diff the two JD parsers
python3 -m resume lock --tailorer llm       # prove frozen sections never move
python3 -m resume check some_resume.pdf     # ATS checks on any PDF
python3 -m resume make --jd jds/x.txt -v    # full flags
```

## How it works

```
profile.json ──┐
               ├─► tailor ──► resume_data.json ──► classic.typ ──► PDF
job posting ───┘   rules or LLM        │                            │
                                       │            fit to one page (render.py)
                                       │            ATS self-check  (verify.py)
                                       ▼
                              build/<date>_<label>.report.json
                              every decision, plus the posting verbatim
```

**`profile.json`** — the superset of everything true about you. Every bullet that
can appear on any generated resume is written here, by you, in advance. The
tailorer only *selects and reorders*. That property is what makes it safe to
automate.

Bullets carry `angles` from a closed enum (`agentic backend systems infra
frontend data eval dsa teaching`). Projects also carry `taglines` keyed by
angle, so one project introduces itself as *"thread-pooled HTTP server"* for a
systems role and *"from-scratch HTTP server with connection pooling"* for a
backend one. Same project, different framing, nothing invented.

Bullets also carry `status`: `shipped` (default) or `planned`. Planned bullets
never render and no tailorer is shown them — write them ahead as a spec, flip
them when the code lands. A project with fewer than two shipped bullets is
dropped rather than rendered thin.

**`templates/classic.typ`** — Typst reads the JSON natively. There is no string
templating anywhere, so generated content can never cause an escaping bug or a
broken compile.

**`resume/verify.py`** — compiles, extracts with `pdftotext`, asserts the result.
Every check exists because a real resume shipped with that defect:

| Check | Catches |
|---|---|
| `bullet-intact` | text silently dropped or garbled by the renderer |
| `skill-label` | `FundamentalsData Structures` — glued table columns |
| `ligature` | `CODING PROfiLES` — ligature glyph with no Unicode mapping |
| `glued-words` | any two words run together in the text layer |
| `columns` | multi-column layout an ATS will interleave |
| `page-count` | overflow past the budget |

It catches visual corruption an ATS would trip on. It cannot see crowding — two
lines nearly touching still look fine to it.

## One-page fitting

`render.py` walks five typography presets, loosest first, recompiling each time.
Only when the tightest still overflows does it drop bullets, lowest-scoring
first, never below two per entry. Losing leading always beats losing an
achievement. Typst compiles in ~40ms, so the whole loop is imperceptible.

## The lock

`profile.json` declares `_locked.frozen_sections`. Those render verbatim — no
scoring, no selection, no reordering — whatever the job says. The fitter cannot
trim them either.

| Section | Behaviour |
|---|---|
| `basics`, `experience`, `coding_profiles`, `education` | frozen |
| `projects` | varies: which, what angle, which bullets |
| `skills` | varies: ordering within each group |

`_locked.max_projects_on_page` caps the project count and cannot be raised from
the command line.

```bash
python3 -m resume lock --tailorer llm
```

Regenerates against every JD in `jds/` and fails loudly if a frozen section
differs between two of them. Run it after touching `tailor.py`.

## Rules or LLM

`auto` (the default) uses the LLM when a key is present and falls back to rules
on any failure — no key, timeout, malformed response, failed validation —
recording the reason in the sidecar. It never raises. `--tailorer llm` is the
strict form: it exits non-zero rather than silently giving you a worse resume.

### The split, decided by measurement

The LLM did not beat plain string matching at recall. `gpt-4o-mini` missed
`Kubernetes` and `Node.js` on a posting naming both outright, and scored a role
demanding "close to the metal" work as 0% systems. So the rule-based pass stays
and the model only adds to it:

| | source |
|---|---|
| `angle_scores` | LLM alone — the judgment call |
| `matched_skills` | rules ∪ LLM — rules guarantee literals, LLM adds implied |
| `gap_skills` | rules ∪ LLM — rules guarantee recall, LLM adds semantic |

Only `gpt-5.5` mapped "networking" onto Computer Networks — the kind of
semantic catch string matching can never make. Set in `llm_tailor.JD_MODEL`,
overridable with `--model`.

### Why the LLM cannot break the lock

`tailor_llm` runs the rule-based tailorer first to build the complete resume,
then overwrites **only** `projects` and `skills`. Frozen sections come from
identical code in both modes, so the LLM has no path to them.

### What the LLM may return

Identifiers, never prose. It answers "use `ws1, ws2, ws4`" and the renderer
looks up your wording, so no generated text reaches the PDF. The schema enforces
the rest — `matched_skills` is an enum of your real skills, so the API rejects
an invented one server-side.

`validate_selection` repairs what a schema cannot express: bullets claimed by
the wrong project, angles a project doesn't offer, duplicates, incomplete skill
groups. Skills must return as a *permutation* — foreign entries dropped, omitted
ones appended. Nothing invented, nothing lost. Every repair is printed, never
applied silently.

## Spacing model

Adjacent Typst blocks collapse to `max(prev.below, next.above)`, which makes an
exact vertical rhythm impossible with block spacing alone — the first build of
this template read as cramped for exactly that reason. Every tight group now
uses `stack`, which spaces children exactly. `leading` and `bullet_gap` are
separate knobs, and `bullet_gap` must stay larger than `leading` so the eye can
tell "next line of this bullet" from "next bullet".

## What is not in this repo

`.gitignore` keeps personal data out. Confirm with `git status --porcelain`
before pushing.

| Excluded | Why |
|---|---|
| `profile.json` | your name, phone, email, employment history |
| `build/` | every PDF carries your phone and email; reports embed the posting |
| `jds/*.txt` (bar the samples) | **reveals where you are applying** — and your employer can read a public repo |
| `.env`, `*.key`, `*.pem` | your API key |
| `.cache/` | cached model responses |
| `fonts/*` | licensed binaries |

The third row is the one that bites people.

## Layout

```
main.py                 interactive entry point
profile.json            your data (gitignored)
profile.example.json    same schema, fake data
templates/classic.typ   the resume template
resume/
  cli.py                make · compare · lock · check
  tailor.py             rule-based tailoring, the lock, status filtering
  llm_tailor.py         JD comprehension + selection, and their validation
  llm.py                the only module that touches the network
  render.py             Typst invocation and the fitting loop
  verify.py             ATS checks
```

## Notes on the data

`needs_verification` fields flag numbers to confirm before sending. They never
render. Three are outstanding, all on the web server project.
