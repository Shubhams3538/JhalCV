# JhalCV in Go

A port of the Python JhalCV backend to Go. It serves the same `web/`
frontend, reads and writes the same files (`profile.json`, `.local/*`,
`jobs.db`, `usage.db`), and returns the same API responses, so either
version can run on the same workspace.

## Run

```bash
cd jhalCV_go
go build -o bin/jhalcv ./cmd/jhalcv && ./bin/jhalcv              # browser app (app.py)
go build -o bin/resume ./cmd/resume && ./bin/resume --help       # terminal CLI (main.py, python -m resume)
```

`--root` (or `$JHALCV_ROOT` for the CLI) is the project folder holding
`web/`, `templates/`, `fonts/` and `data/`; it defaults to `..`. External
tools are the same as Python's: Typst, Poppler (`pdfinfo`, `pdftotext`,
`pdftohtml`), and optionally Chrome, Claude Code and Codex.

## Develop

```bash
gofmt -l .         # formatting (no output means formatted)
go vet ./...       # static checks
go test ./...      # tests
python3 ../bench/run.py --impl go,python   # benchmarks, see ../bench
```

## Layout

| Package | Python original | What it does |
|---|---|---|
| `cmd/jhalcv` | `app.py` | Starts the local server |
| `cmd/resume` | `main.py`, `resume/cli.py`, `resume/jobs/__main__.py` | Terminal workflow |
| `internal/web` | `resume/web.py` (HTTP part) | Routes, token/Origin/Host checks, responses |
| `internal/app` | `resume/web.py` (`App`) | Workspace state: drafts, tailoring, setup, job actions |
| `internal/storage` | `resume/storage.py` | Profile and settings, validation, atomic writes |
| `internal/credentials` | `resume/credentials.py` | API keys; the page only ever sees masks |
| `internal/llm` | `resume/llm.py`, `provider_api.py`, CLI runners | Structured AI calls, failover, cache |
| `internal/usage` | `resume/usage.py` | Token and cost ledger |
| `internal/resume` | `tailor.py`, `llm_tailor.py`, `combined_tailor.py`, `descriptions.py`, `skill_swap.py`, `insights.py`, `render.py`, `verify.py`, `workspace.py`, `github.py`, `folders.py` | Tailoring, PDF rendering and checks, export |
| `internal/jobs` | `resume/jobs/*` | Career-site readers, SQLite store, refresh, eligibility, Chrome pool |
| `internal/onboarding` | `onboarding.py`, `resume_import.py`, `document_text.py` | First-run setup and resume import |
| `internal/timing` | `timing.py` | Live tailoring progress |
| `internal/usererr` | `ValueError` | Errors whose message is shown to the user |

## How it was checked against Python

- Rules tailoring: identical data and reports for every profile × sample job.
- `/api/state`, `/api/jobs`, `/api/usage`: identical on a copy of the real workspace.
- Tailor → preview → PDF → export: identical rendered data and PDF.
- All 13 career-platform readers: identical jobs and fields on live sites.
- A full refresh of three companies: all 1,379 saved job rows identical afterwards.
- PDF and DOCX import, and the terminal CLI's output: identical.

## Intentional differences

- **Claude Code is tried before Codex** for subscription AI.
- HTML-to-text uses hand-written scanners instead of regexes, for speed;
  `internal/jobs/htmltext_test.go` checks they match the original regexes.
- A missing text field (for example an experience `location`) is read as
  empty; Python rejects it.
