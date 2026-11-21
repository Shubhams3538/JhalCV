# Benchmarks

Compares the JhalCV implementations (Python today, Go in `jhalCV_go/`, Rust
next) on identical work.

```bash
brew install hyperfine oha        # once
python3 bench/run.py              # everything (about 5 minutes)
python3 bench/run.py --only http,micro
python3 bench/report.py bench/results/<date>/results.json
```

Each implementation runs against its own disposable copy of this workspace
(your profile, settings and job collection) in **rules mode**. Nothing spends
AI quota, and your real files are never written.

## What is measured

| Group | What | Tool |
|---|---|---|
| `startup` | Process launch until the first HTTP response | hyperfine, 15 runs |
| `http` | Throughput and p50/p95/p99 latency of a light, a medium and a heavy read endpoint, 8 concurrent clients; server memory | oha |
| `tailor` | `POST /api/tailor` in rules mode, 200 sequential requests (the app allows one tailoring run at a time) | oha |
| `preview` | Building a one-page PDF preview (mostly Typst and Poppler time) | timed loop |
| `refresh` | A fresh refresh (empty job collection, no caches, like the Test lab) of the same 15 direct-feed companies from `data/companies.json`, one platform at a time; runs alternate between implementations so network drift affects both. `--refresh-companies N` changes the count | timed loop |
| `micro` | Pure CPU work on the same inputs (`bench/fixtures.py`): HTML to text, profile validation, job analysis, tailoring, year requirements | Go `testing.B` / Python loop |
| `footprint` | Binary size, runtime needed, lines of code and tests, dependencies | |

Results for each day are in `bench/results/<date>/` (`results.json` and
`REPORT.md`). Only timings and machine details are saved.

## Adding an implementation

Add an entry to `IMPLEMENTATIONS` in `run.py` (build command, server command,
binary, sources) and a branch in `bench_micro` that runs its micro benchmarks
on the fixtures and prints milliseconds per call.
