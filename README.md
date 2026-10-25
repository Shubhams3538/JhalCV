# JhalCV

A local resume workspace: paste a job description, import your GitHub projects,
review concise descriptions, and save a one-page PDF. Everyone runs their own
copy. There is no central server, account, database, frontend build, or Python
package installation.

## Start the browser app

Install **Python 3.9+**, [Typst](https://github.com/typst/typst#installation), and
[Poppler](https://poppler.freedesktop.org/) (`pdfinfo` and `pdftotext`). On macOS:

```bash
brew install typst poppler
```

Then, from this repository:

```bash
cp .env.example .env       # optional: existing environment-key workflow
./start.sh
# Windows, or any platform without sh:
python3 app.py
```

The launcher chooses a free port, binds to `127.0.0.1`, and opens your browser.
Keep the terminal running; press Ctrl+C to stop. To choose a port or open the
URL yourself, use `python3 app.py --port 8765 --no-browser`.

In **Settings → Providers & API keys**, add OpenAI, Gemini, Anthropic, or
OpenRouter keys. A provider-specific model menu selects a default automatically;
you can choose another named model or **Other model…** for a custom ID. Saved
entries offer **Change model** without requiring the key again. You can keep several
keys and use **Try first** to set priority. Failed keys are skipped for the rest
of the current operation; another configured key/provider is tried automatically.
If none succeeds, JhalCV preserves original wording and uses rules.

Saved credentials are never sent back to the browser, not even partial prefixes
or suffixes. Every entry displays the same dummy mask. New keys are entered in
a password field, sent to the local backend, and cleared from the form on
submission. They are stored in a permission-restricted `.local/credentials.json`
file (0600 on macOS/Linux), **not encrypted at rest**. Keys are excluded from
provider error messages, request-body caches, and reports. User-entered keys
necessarily pass through the browser when first submitted; saved keys are never
retrieved by the frontend.

When no key is configured, a startup dialog offers **Add an API key**, **Not
now**, or **I will never add an API key**. The last option is saved locally and
stops future reminders. You can re-enable reminders in Settings. Rules mode
continues to work without credentials.

1. Open **My profile**, replace the sample contact details, experience,
   education, achievements, and skills, then save. An existing `profile.json`
   is loaded automatically. Tailoring never edits these fixed sections.
2. In **Tailor resume**, add a public GitHub repository URL. The app fetches its
   default-branch README and prepares an editable project draft. Read the source,
   confirm what you actually built, remove planned/unverified claims, and save.
   You can also add projects manually or edit your existing library.
3. Paste the job description and choose which projects to consider. The current
   profile's project cap remains in effect (two by default). AI selects relevant
   projects and lightly refines their bullets; rules work without an API key.
4. Review the tailored bullet wording, then **Build PDF preview**. Check the
   PDF and any text-check warnings, then **Save to destination**.
5. Use **Choose folder…** in **Settings** to pick your saving folder, or enter
   its path, then save settings. The default is `build/shubham_Resume.pdf`.
   Each new export becomes the current file with that same name. The previous
   PDF is preserved under `Archive/` with a unique dated filename. The saved
   path is shown after export. Sort your file picker by **Date modified, newest
   first** to put the most recently saved resume at the top; picker sorting
   remains controlled by your operating system. Native selection uses macOS,
   Windows, or Linux (Zenity/KDialog) dialogs, with manual path entry as fallback.

### Clarity and the page budget

Project purpose comes first. The model is instructed to emphasize natural JD
overlap using only confirmed project evidence, never to force requirements into
the resume or invent achievements. Imported README drafts need your review:
READMEs can contain plans, examples, or claims about other contributors.

For existing bullets, every AI rewrite and every reviewed edit must fit the
**original character count AND word count**, keep the same bullet count, and
introduce no new numbers. Invalid AI edits fall back to the original bullet.
Project headings and stacks are unchanged by rewriting. Projects display their
heading and bullets, with no short introduction.

The original selected content is first fitted using the existing one-page
renderer. Reviewed wording is then compiled with exactly that baseline's font,
spacing, and margins. If it overflows, original project wording is restored and
the preview explains why. If the baseline itself cannot fit on one page, export
is blocked so you can shorten the saved profile. Text-layer checks run before
export; missing content blocks it, while heuristic glued-word/column warnings
are shown for visual review. Shorter character counts alone cannot guarantee
identical line wrapping, so the rendered PDF is the final check.

### Local data and optional network services

- `profile.json`: your source profile and confirmed project evidence.
- `.local/settings.json`: your output folder, filename, mode, and environment model.
- `.local/credentials.json`: private keys, provider/model choices, priority, and reminder preference.
- `.cache/web/`: PDF previews, rendered JSON, and decision reports including JDs.
- `.cache/llm/`: cached model requests and responses.

All of these are gitignored. Output folders you choose outside this repository
are your responsibility; avoid publicly tracked or shared folders for private
resumes. Preview files remain on disk until you delete `.cache/web/`; draft
sessions expire after two hours or when the local server stops. Save an exported
PDF before closing the server.

Public GitHub imports work without a token. For private repositories or higher
API limits, set `GITHUB_TOKEN` in `.env` with read access to the repository. Use
canonical repository URLs (`https://github.com/owner/repo`), not file/branch
links. Moved repositories need their current URL. API keys/tokens stay in the
local Python process and never reach the frontend.

In AI mode, your JD and relevant profile/project content are sent directly to
your configured providers; automatic failover can send the same content to
another configured provider. README import contacts GitHub. **Rules only** in
Settings makes no AI requests and uses README excerpts for import. Environment
keys remain supported via `OPENAI_API_KEY`, `GEMINI_API_KEY`,
`ANTHROPIC_API_KEY`, and `OPENROUTER_API_KEY`. **Ignore** hides an environment
key from JhalCV without modifying the environment file.

Provider adapters use native structured JSON requests: [OpenAI](https://developers.openai.com/api/docs/guides/structured-outputs),
[Gemini](https://ai.google.dev/gemini-api/docs/generate-content/structured-output),
[Anthropic](https://platform.claude.com/docs/en/build-with-claude/structured-outputs),
and [OpenRouter](https://openrouter.ai/docs/guides/features/structured-outputs).
Model IDs are editable because availability depends on your provider account.
Requests are limited to fixed official endpoints; keys travel only in headers.
Responses are validated locally before use or caching.

### Verification

```bash
python3 -m unittest discover -s tests -v
```

The suite covers storage validation, HTTP origin/token protection, GitHub URL
and README handling, AI length/number fallback, fixed-section preservation,
PDF overflow handling, real Typst/Poppler rendering, custom folders, stale
previews, archive preservation, and concurrent export protection. PDF integration tests skip if tools are not
installed. Provider tests use mocked native responses and failures to check
automatic failover, response validation, cache isolation, and credential privacy.
These fixtures do not prove model availability on a particular provider account.

## Original terminal workflow

`main.py` and `python3 -m resume` remain available:

### Terminal setup

```bash
brew install typst poppler          # the only dependencies; no Python packages
cp profile.example.json profile.json
cp .env.example .env                # add OPENAI_API_KEY (optional)
```

### Terminal usage

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
frontend data eval dsa teaching`). Projects show their name, technology stack,
GitHub link, and selected bullets. Legacy `taglines` in existing profiles are
accepted but do not appear in the editor or PDF.

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

For the original CLI selection step: identifiers, never prose. The browser app adds a separate, bounded, reviewable project-bullet rewrite after selection. It answers "use `ws1, ws2, ws4`" and the renderer
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
