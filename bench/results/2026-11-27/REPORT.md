# JhalCV benchmark: python vs go

macOS-27.0.1-arm64-arm-64bit, 10 cores, run 2026-11-27T19:18:47. Every implementation used its own copy of the same workspace (real profile and job collection), in rules mode, so no AI calls are included.

### Cold start (launch → first HTTP response)

| | python | go | go vs python |
|---|---|---|---|
| median | 59.17 ms | 13.05 ms | 4.5× faster |
| mean ± stdev | 74.74 ms | 12.97 ms | 5.8× faster |

### HTTP throughput, 8 concurrent clients

| | python | go | go vs python |
|---|---|---|---|
| GET /api/jobs/progress (light) | 38,689.06 req/s | 79,888.10 req/s | 2.1× faster |
| GET /api/state (medium) | 122.14 req/s | 999.43 req/s | 8.2× faster |
| GET /api/jobs (heavy, all saved jobs) | 9.28 req/s | 147.96 req/s | 15.9× faster |

### HTTP latency p99, 8 concurrent clients

| | python | go | go vs python |
|---|---|---|---|
| GET /api/jobs/progress (light) | 2.03 ms | 0.21 ms | 9.7× faster |
| GET /api/state (medium) | 76.20 ms | 10.28 ms | 7.4× faster |
| GET /api/jobs (heavy, all saved jobs) | 948.07 ms | 62.92 ms | 15.1× faster |

### Server memory (RSS)

Go's garbage collector keeps freed heap for reuse instead of returning it to the OS at once.

| | python | go | go vs python |
|---|---|---|---|
| idle | 26.97 MB | 21.12 MB | 1.3× less |
| after the load test | 58.48 MB | 102.77 MB | 1.8× more |

### Resume tailoring through the API (rules, sequential)

| | python | go | go vs python |
|---|---|---|---|
| p50 per request | 9.08 ms | 2.81 ms | 3.2× faster |
| p99 per request | 10.61 ms | 3.18 ms | 3.3× faster |

### One-page PDF preview build

Dominated by Typst and Poppler (the same external tools for every implementation).

| | python | go | go vs python |
|---|---|---|---|
| median | 0.54 s | 0.53 s | same |

### Jobs refresh: fresh run, same companies for each

Empty job collection and no caches, like a Test lab run; network bound; implementations alternate each round. Summary: python: “100ms, ACKO, Adobe, Airbnb, Akamai, Amazon, Amazon / AWS, Arista Networks, Bolna AI, ClickHouse, Google, Morgan Stanley, Nurix AI, Salesforce, AMD: 436 matching job(s), 393 new. 392 checked by text rules. could not read: AMD.”; go: “100ms, ACKO, Adobe, Airbnb, Akamai, Amazon, Amazon / AWS, Arista Networks, Bolna AI, ClickHouse, Google, Morgan Stanley, Nurix AI, Salesforce, AMD: 436 matching job(s), 393 new. 392 checked by text rules. could not read: AMD.”

| | python | go | go vs python |
|---|---|---|---|
| median | 50.12 s | 33.50 s | 1.5× faster |
| peak memory | 507.44 MB | 101.81 MB | 5.0× less |

### CPU micro benchmarks (median per call)

| | python | go | go vs python |
|---|---|---|---|
| HTML → text (1.4 MB page) | 3.28 ms | 1.00 ms | 3.3× faster |
| HTML → text with links | 6.64 ms | 1.53 ms | 4.3× faster |
| Parse + validate profile | 0.08 ms | 0.01 ms | 5.9× faster |
| Job description analysis (rules) | 2.13 ms | 0.08 ms | 27.5× faster |
| Full rules tailoring | 2.26 ms | 0.11 ms | 21.4× faster |
| Read year requirements (400 lines) | 1.86 ms | 1.69 ms | 1.1× faster |

### Footprint

| | python | go |
|---|---|---|
| Deployable binary | – | 19.2 MB |
| Runtime needed | Python 3.9.6 interpreter | none (self-contained binary) |
| Lines of code | 8,591 | 17,815 |
| Lines of tests | 5,322 | 914 |
| Source files | 38 | 58 |
| Third-party modules | none (standard library) | 10 (github.com/dustin/go-humanize, github.com/google/uuid, github.com/mattn/go-isatty, github.com/ncruces/go-strftime, github.com/remyoudompheng/bigfft, golang.org/x/sys, modernc.org/libc, modernc.org/mathutil, modernc.org/memory, modernc.org/sqlite) |
