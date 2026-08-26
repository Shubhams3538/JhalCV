# runtime_resume_maker — v1

Give it a job description, get back a tailored one-page PDF.

**v1 is deliberately LLM-free.** The tailoring is rule-based so the whole pipeline
runs today with no API key, which means v1 can be judged on the only question
that matters right now: *is the PDF it produces something you'd actually send?*
If the answer is yes, the LLM swaps in behind one function and nothing else moves.

## Setup

```bash
brew install typst poppler          # only dependencies; no Python packages
cp profile.example.json profile.json
$EDITOR profile.json                # your data. gitignored - never committed.
```

## Run it

```bash
python3 -m resume make --jd jds/backend_systems.txt
python3 -m resume make --jd jds/ai_engineer.txt

# Point the ATS checks at any resume PDF, including one this didn't make:
python3 -m resume check ~/Downloads/old_resume.pdf
```

## What is not in this repo

`.gitignore` keeps personal data out. Before pushing, confirm with
`git status --porcelain` that none of these appear:

| Excluded | Why |
|---|---|
| `profile.json` | your name, phone, email, full employment history |
| `build/` | every PDF carries your phone and email; `.report.json` embeds the JD verbatim |
| `jds/*.txt` (except the two samples) | **reveals which companies you are applying to** — the most sensitive thing here — and postings are usually copyrighted |
| `.env`, `*.key`, `*.pem` | the API key the v2 tailorer will need |
| `fonts/*` | licensed font binaries you should not redistribute |
| `*.pdf` | belt-and-braces, so a stray resume never lands in a commit |

The one that bites people is the third row. A public repo listing the job
descriptions you have been feeding it is a public list of where you are
interviewing, and your current employer can read it.

## Pipeline

```
profile.json ──┐
               ├──► tailor.py ──► resume_data.json ──► classic.typ ──► PDF
jds/*.txt   ───┘   (rule-based)          │                              │
                                         │              render.py: fit to 1 page
                                         │              verify.py: ATS self-check
                                         ▼
                              build/<date>_<jd>.report.json
                              (every decision, plus the JD verbatim)
```

## The four pieces

**`profile.json`** — the superset of everything true about you. Every bullet that
appears on any generated resume is written here, by you, in advance. The tailorer
only ever *selects and reorders*; it cannot write a new claim. That property is
what makes it safe to automate.

Bullets carry `angles` (a closed enum: `backend systems infra frontend agentic
data eval dsa teaching`). Projects additionally carry `taglines` keyed by angle,
so the same project can be introduced as "thread-pooled HTTP server" for a
systems role and "from-scratch HTTP server with connection pooling" for a backend
one. Same project, different framing, zero invention.

**`resume/tailor.py`** — scores the JD against the angle vocabulary, then ranks
bullets and projects by that score. Deterministic and debuggable: the report
shows why every item ranked where it did. When the LLM replaces this, it keeps
the same signature and the same output contract.

**`templates/classic.typ`** — Typst reads `resume_data.json` natively. There is
no string templating anywhere in this project, so generated content can never
produce an escaping bug or a broken compile.

**`resume/verify.py`** — compiles, extracts with `pdftotext`, and asserts the
result. Every check exists because a real resume shipped with that defect:

| Check | Catches |
|---|---|
| `bullet-intact` | text silently dropped or garbled by the renderer |
| `skill-label` | `FundamentalsData Structures` — glued table columns |
| `ligature` | `CODING PROfiLES` — ligature glyph with no Unicode mapping |
| `glued-words` | any two words run together in the text layer |
| `columns` | multi-column layout an ATS will interleave |
| `employer` / `project` / `link-url` | names and URLs missing from the text layer |
| `page-count` | overflow past the page budget |

## One-page fitting

`render.py` walks five typography presets, loosest first, recompiling each time.
Only when the tightest preset still overflows does it start dropping bullets, and
it drops the globally lowest-scoring one first, never below two per entry. Losing
leading always beats losing an achievement. Typst compiles in ~40ms, so a full
fit loop is imperceptible.

## Tailoring: rules or LLM

```bash
python3 -m resume make --jd jds/x.txt                    # auto: LLM if a key is present
python3 -m resume make --jd jds/x.txt --tailorer rules   # never call the API
python3 -m resume make --jd jds/x.txt --tailorer llm     # fail loudly if unavailable
python3 -m resume compare --jd jds/x.txt                 # diff the two JD parsers
```

`auto` falls back to rules on any failure — no key, timeout, malformed response,
failed validation — and records the reason in the sidecar report. It never
raises and always produces a PDF. `--tailorer llm` is the strict form: it exits
non-zero rather than silently degrading, which is what you want in CI.

### The split, decided by measurement

The LLM did not beat plain string matching at recall. `gpt-4o-mini` missed
`Kubernetes` and `Node.js` on a JD naming both outright, and scored a role
demanding "close to the metal" work as 0% systems. So the rule-based pass stays
and the model only adds to it:

| | source |
|---|---|
| `angle_scores` | LLM alone — the judgment call |
| `matched_skills` | rules ∪ LLM — rules guarantee literals, LLM adds implied |
| `gap_skills` | rules ∪ LLM — rules guarantee recall, LLM adds semantic |

Model is `gpt-5.5` (`llm_tailor.JD_MODEL`), overridable with `--model`.

### Why the LLM cannot break the lock

`tailor_llm` runs the rule-based tailorer first to build the complete resume,
then overwrites **only** `projects` and `skills`. Frozen sections come from
identical code in both modes, so the LLM has no path to them. Provable:

```bash
python3 -m resume lock --tailorer llm
```

### What the LLM is allowed to return

Identifiers, never prose. It answers "use `ws1, ws2, ws4`" and the renderer looks
up your wording from `profile.json`, so no generated text reaches the PDF. The
schema enforces the rest: `matched_skills` is an enum of your real skills, so the
API rejects an invented one server-side.

`validate_selection` then repairs what a schema cannot express — bullets claimed
by the wrong project, angles a project does not offer, duplicates, and skill
groups that came back incomplete. Skills must return as a *permutation*: foreign
entries are dropped, omitted ones appended. Nothing invented, nothing lost. Every
repair is printed and recorded rather than applied silently.

## Next

Replace `tailor.parse_jd` and the scoring in `tailor.tailor` with LLM calls that
return the same shapes. Add optional Tier-2 vocabulary rewriting behind a
validation gate: every number in a rewritten bullet must appear in the original,
every technology must appear in the project's `stack`, or the original is used.
`--no-rewrite` must always produce the pure Tier-1 resume.

## The lock

`profile.json` carries a `_locked.frozen_sections` list. Those sections render
verbatim from the file — no scoring, no selection, no reordering — whatever the
job description says. The page-fitter is also barred from trimming them, so a
frozen bullet can never be dropped for space; if the page overflows, only
project bullets are eligible.

| Section | Behaviour |
|---|---|
| `basics` | frozen |
| `experience` | frozen — every bullet, in profile order |
| `coding_profiles` | frozen |
| `education` | frozen |
| `projects` | varies by JD: which projects, their angle/tagline, which bullets |
| `skills` | varies by JD: ordering within each group |

Verify it at any time:

```bash
python3 -m resume lock      # regenerates against every JD in jds/, compares hashes
```

It fails loudly if a frozen section ever differs between two job descriptions.
Run it after touching `tailor.py`.

## Spacing model

Adjacent Typst blocks collapse to `max(prev.below, next.above)`, which makes an
exact vertical rhythm impossible to express with block spacing alone — the first
build of this template read as cramped for exactly that reason. Every tight group
now uses `stack`, which spaces its children exactly. `leading` and `bullet_gap`
are separate knobs, and `bullet_gap` must stay larger than `leading` so the eye
can tell "next line of this bullet" from "next bullet".

The presets in `render.py` run comfortable-first: a short resume is never
compressed, and tightening happens only when the page actually overflows.

## Notes on the data

Fields named `needs_verification` in `profile.json` flag numbers to confirm
before sending. The big one: the web server bullet originally read *"1M+
concurrent requests"*, which is not credible for a Java thread-pool server. It
now reads "1M+ requests in sustained load tests" — confirm the real figure.
