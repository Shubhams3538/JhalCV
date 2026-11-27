# JhalCV

A local resume workspace: paste a job description, and JhalCV tailors your
saved profile to it and saves a one-page PDF. It also collects jobs from
company career sites and checks each one against your experience. Everything
runs on your own machine; there is no account or central server.

The backend exists in two languages, behind the same browser frontend:

- **Python**, the original (`resume/`, `app.py`)
- **Go**, a full port (`jhalCV_go/`)

Both read and write the same files, so you can switch between them on the
same workspace. A benchmark suite compares the two.

## Start

Install [Typst](https://github.com/typst/typst#installation) and
[Poppler](https://poppler.freedesktop.org/) (`pdfinfo`, `pdftotext`), plus
Go 1.27+ for the Go backend or Python 3.9+ for the Python one. On macOS:

```bash
brew install typst poppler go
```

Then, from this folder:

| Command | What it does |
|---|---|
| `make go` | Start JhalCV with the Go backend |
| `make python` | Start JhalCV with the Python backend |
| `make test` | Check and test the Go code |
| `make bench` | Run the Python vs Go benchmarks and print the report |

Add options with `ARGS`, for example `make go ARGS="--port 8765 --no-browser"`.
The app binds to `127.0.0.1`, picks a free port and opens your browser; press
Ctrl+C to stop. Run only one backend at a time on the same folder.

On first start, a setup guide imports your resume (PDF or DOCX), lets you
choose an AI (Claude Code, Codex, an API key, or rules only), and builds a
first preview. Claude Code is tried before Codex.

## Your data stays local

Everything personal is gitignored: `profile.json`, `.local/` (settings, API
keys, jobs and usage databases), `.cache/` (previews and AI answers),
`build/`, real job descriptions in `jds/`, `.env` and `fonts/`. Check
`git status --porcelain` before pushing.

In AI mode, your job description and the relevant profile content go to the
AI you chose. **Rules only** makes no AI requests.

## How the Go port was done

The Go version was ported module by module from the Python code. Each module
was checked against the Python app before moving on:

1. **Server and storage.** A local-only HTTP server serving the same `web/`
   frontend with the same security checks (Host header, per-run token,
   Origin), then the profile and settings with the same validation.
2. **AI and credentials.** Structured AI calls across API keys and the Claude
   Code and Codex CLIs, with failover, cooldowns, a local answer cache and
   the same usage ledger (`usage.db`).
3. **Resume pipeline.** Rules and AI tailoring, Typst page fitting, ATS text
   checks, previews, and archived export.
4. **Jobs collector and setup.** Readers for 13 career platforms, the SQLite
   job store, parallel refresh with a bounded review queue, a headless
   Chrome pool, and first-run setup with PDF/DOCX import.
5. **Terminal CLI**, then the benchmark suite.

How it was checked, each time by running both apps on a copy of the real
workspace:

- Rules tailoring: identical picks, scores and reports for every profile and
  sample job.
- `/api/state`, `/api/jobs`, `/api/usage`: identical responses.
- Tailor → preview → PDF → export: identical output files.
- All 13 career-platform readers: identical jobs and fields from the live sites.
- A full refresh: every saved job row identical afterwards.
- PDF and DOCX import, and the CLI output: identical.

Go-specific choices:

| Python | Go |
|---|---|
| Dicts everywhere | Typed structs for profile, settings, drafts and jobs; JSON maps only for open-ended AI answers and reports |
| `ValueError` for user-facing errors | A small `usererr` type; other errors are logged and shown as a generic message |
| Thread pools and `contextvars` | Goroutines, channels, and `context.Context` for budgets, sessions and timings |
| `re` with lookbehind | Go's `regexp` has no lookbehind, so boundaries are checked in code |
| Regex-based HTML-to-text | Hand-written single-pass scanners, tested against the original regexes on 20,000 random pages |
| Standard library only | Standard library, plus a pure-Go SQLite driver and `x/term` |

The only behaviour change is that Claude Code is tried before Codex.

Layout of the Go code:

| Package | Replaces |
|---|---|
| `cmd/jhalcv` | `app.py` |
| `cmd/resume` | `main.py`, `resume/cli.py`, `python -m resume.jobs` |
| `internal/web`, `internal/app` | `resume/web.py` |
| `internal/storage` | `resume/storage.py` |
| `internal/credentials`, `internal/llm`, `internal/usage` | `credentials.py`, `llm.py`, `provider_api.py`, `usage.py` |
| `internal/resume` | `tailor.py`, `llm_tailor.py`, `combined_tailor.py`, `descriptions.py`, `skill_swap.py`, `insights.py`, `render.py`, `verify.py`, `workspace.py`, `github.py`, `folders.py` |
| `internal/jobs` | `resume/jobs/*` |
| `internal/onboarding` | `onboarding.py`, `resume_import.py`, `document_text.py` |

## Benchmarks: Python vs Go

`make bench` runs everything, then prints and saves the report in
`bench/results/<date>/REPORT.md`. Each backend runs on its own throwaway copy
of the workspace in rules mode, so no AI calls are made and your real files
are never touched. Details are in [`bench/README.md`](bench/README.md).

Results on an Apple M5 (10 cores), 27 Nov 2026:

| What | Python | Go | Difference |
|---|---|---|---|
| Cold start (launch → first response) | 59 ms | 13 ms | Go 4.5× faster |
| `GET /api/jobs`, all saved jobs, 8 clients | 9 req/s | 148 req/s | Go 16× faster |
| `GET /api/state`, 8 clients | 122 req/s | 999 req/s | Go 8× faster |
| `GET /api/jobs` p99 latency | 948 ms | 63 ms | Go 15× faster |
| Tailoring through the API (rules) | 9.1 ms | 2.8 ms | Go 3.2× faster |
| One-page PDF preview | 0.54 s | 0.53 s | Same (Typst does the work) |
| Fresh jobs refresh, 15 companies | 50.1 s | 33.5 s | Go 1.5× faster |
| Peak memory during that refresh | 507 MB | 102 MB | Go 5× less |
| Server memory after the HTTP load test | 58 MB | 103 MB | Python 1.8× less |

CPU-only work on identical inputs:

| What | Python | Go | Difference |
|---|---|---|---|
| Job description analysis | 2.13 ms | 0.08 ms | Go 27× faster |
| Full rules tailoring | 2.26 ms | 0.11 ms | Go 21× faster |
| Profile parsing and validation | 0.08 ms | 0.01 ms | Go 6× faster |
| HTML → text, 1.4 MB page | 3.28 ms | 1.00 ms | Go 3.3× faster |
| Reading year requirements (regex heavy) | 1.86 ms | 1.69 ms | About the same |

Footprint:

| | Python | Go |
|---|---|---|
| To run it | A Python interpreter | One 19 MB binary, nothing else |
| Lines of code | 8,591 | 17,815 |
| Lines of tests | 5,322 | 914 |
| Third-party code | None | Pure-Go SQLite driver and its dependencies (10 modules) |

What stands out:

- **Go wins most on pure computation** (20–27×) and under concurrent load,
  where Python's threads share one interpreter lock.
- **Work that waits on outside tools or the network gains less.** PDF
  previews are Typst's time; a refresh is mostly waiting for career sites.
  The refresh still finished 1.5× sooner using a fifth of the memory.
- **Regex-heavy code is close.** Python's `re` is written in C, and Go's
  `regexp` trades speed for guaranteed linear time. Replacing the hottest
  patterns with scanners was needed to make Go faster at HTML-to-text.
- **Go holds on to memory after a burst**: its garbage collector keeps
  freed heap for reuse instead of returning it straight away.
- **Go takes about twice the code**, mostly explicit error handling and types,
  and ships as a single self-contained binary.

Every speed figure depends on the machine and, for the refresh, on how the
career sites respond that day.
