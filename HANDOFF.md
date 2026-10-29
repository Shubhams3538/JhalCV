# JhalCV handoff

Updated October 24, 2026. Branch: `jobs`. The user requested the accompanying commit be dated October 25, 2026. Do not push unless asked.

## Product and preferences

JhalCV runs locally: `./start.sh` opens the browser frontend. For development, use `python3 app.py --no-browser --port 8765`. Keep the existing CLI working.

Users enter a job description, manage projects, import GitHub READMEs, preview a one-page resume, and save it to a chosen folder. Project descriptions should convey the actual project, match the JD naturally, and stay within the existing length budget. No project introductions. Removing projects must persist in the saved profile.

The user wants implementation without code walkthroughs. Read `AGENTS.local.md`; defaults are slopper, ego-review, and expertise none.

## Changes in the accompanying commit

- Native saving-folder chooser, with manual path fallback. The folder contains one current `shubham_Resume.pdf`; previous copies go into `Archive`. Saving uses a per-folder lock and atomic replacement.
- Success notices disappear after five seconds, errors after eight; clicking any notice dismisses it.
- Settings support multiple OpenAI, Gemini, Anthropic, and OpenRouter keys, a preferred key, and automatic failover. The startup reminder offers a permanent opt-out.
- Model selectors show readable provider-specific choices and a selected default, plus an Other model option. Existing saved keys have a Change model editor; changing a model does not require re-entering its key. Existing models are preserved.
- Saved credentials stay on the local backend. Browser responses contain fixed dummy masks only, never a key or its prefix. Provider failures and generated output are checked to prevent secret leakage into reports or caches.

## Changes on October 26, 2026

- Checked projects are the only ones sent to the AI. When the checked count fits the page cap, every checked project is shown; the model is asked for exactly that many and omitted ones are backfilled.
- Insights (nav item above Settings): each draft records the skills its JD asked for in `.local/insights.json` (one record per JD, canonical names so C++/cpp and k8s/Kubernetes merge). Shows top 10 missing skills (checked against the whole profile) and top 10 requested skills, with Add to skills, Hide, and Reset. Recommendations are deferred by the user.
- Skill groups print at most 9 items (`MAX_SKILLS_PER_GROUP`, hardcoded for now), most job-relevant first.
- `**phrase**` in bullets renders bold. AI rewrites must keep bold phrases unchanged or the original bullet is kept.
- Heading rows (company/city, role/dates, project/GitHub) render as one text line, so parsers no longer attach a city or link to the previous entry.
- Unexpected server errors print a traceback to the terminal; the browser still gets the generic message. The user reported that message once; the cause is not yet identified.

## Jobs collector (branch `jobs`, in progress)

- October 25 CSV import: Jobs supports UTF-8 `company,url` files (also name/link headers), preview and row selection, 500 rows/500 KB, atomic insertion and duplicate name/canonical URL skipping. India defaults for imported sources. Import never auto-refreshes. The user's 145-company workbook was audited; 76 URLs refined. Deliverables are ignored `build/company-import/india_145_companies.csv` and `portal_link_audit.csv`; 15 portals remain blocked or unverified, listed in the audit. No bulk import was performed in the user's database.
- Explicit IN/IND country fields from Lever/Ashby and verified India-only Workday facets are retained in location labels. Workday country context survives pagination without facets; mixed-country selections are not relabelled India.

- Jobs page: the user adds careers URLs with filters already applied, plus optional title include/exclude keywords. Data lives in `.local/jobs.db` (SQLite).
- Readers in `resume/jobs/sources.py`: Google Careers (embedded `ds:1` data), Amazon (`search.json`, with `country[]` renamed to `normalized_country_code[]`), Workday CXS API, Greenhouse, Lever, Ashby. Any other page is read by an agent from page text; only links that exist on the page are kept (first results page only).
- Custom career pages that embed a Greenhouse job-board script (e.g. Tower Research Capital `/roles/`) resolve its board token and read the full Greenhouse feed directly, without an AI page-reader call. The remaining generic page reader still covers only the supplied page, not pagination links.
- Each saved source has an editable comma-separated location filter, independent of its URL and title filters. Greenhouse office locations include country when available (e.g. Gurgaon, Haryana, India). Out-of-filter saved jobs are hidden from active lists and AI checks, while Applied jobs remain visible. Multi-office jobs show only matching locations in a filtered view. Needs review and Old drawers appear only with the default Eligible tab. Tower source is set to India only per user.
- Relevance and experience check (`resume/jobs/eligibility.py`): batches of 8 jobs sent to Codex first, then Claude Code (`resume/jobs/agents.py`), on the user's subscriptions. Agents run in an empty temp folder with no tools, and API-key env vars are removed. If both fail, a text-rule fallback runs. The years default to the profile dates and can be overridden.
- `python3 -m resume.jobs refresh` runs a refresh from the terminal. Scheduling every 24 to 48 hours is not decided yet.
- Profile saves now carry a revision, so a stale tab cannot overwrite newer profile edits.
- Three main Jobs tabs: Eligible (default), Not eligible, and Applied. Needs review and Old are collapsed sections. Jobs dated today through seven calendar days ago qualify for the first two tabs when verdicts are clear; eight-day-old unapplied jobs move to Old, and unapplied jobs are deleted once posting age exceeds eight days. Applied jobs remain until explicitly deleted. Invalid, future, missing-date, and uncertain jobs go to Needs review. Individual and category Delete retain minimal source/external-ID tombstones to prevent recollection. Age cleanup runs on Jobs state load and refresh, not while the app is closed.
- Google/Amazon request newest sorting; Greenhouse update timestamps never count as posting dates. Old Greenhouse saved dates are quarantined on schema migration until refreshed. Workday relative dates and detail-page dates are supported. Barclays visible "Date live" takes precedence over conflicting structured dates. Saved URLs missing dates are retried independently of listing success, with oldest-attempt rotation and a bounded detail budget. Expired new arrivals are tombstoned so they cannot consume that budget repeatedly.
- Broad software/adjacent-role relevance shares the existing batch experience call. Clearly unrelated jobs move to Not relevant; ambiguous roles are retained. Decisions persist for unchanged content, and changed duties/qualifications trigger rechecking. Failed/deferred detail reads retry next refresh.
- User direction: downloadable local-first app, own data/subscriptions; make it reliable for this user before adding role-selection menus or public onboarding. No hosted platform planned now.


## Private data and validation

Do not overwrite the user's profile or environment file, or print credentials. `profile.json`, `.local/`, `.cache/`, and `build/` are ignored. Credentials are stored with mode 0600 in `.local/credentials.json`; they are not encrypted at rest.

Validation (October 25): 56 unit tests passed; JS syntax, Python compilation, and diff checks passed. Isolated browser checks covered model selection, saved-model updates, dummy-only credential state, reminder choices, and disappearing notices. A live OpenAI call succeeded with the new default `gpt-5.4-mini`. Other providers were tested with transport fixtures, not live accounts. Windows/Linux native folder dialogs have not been verified on those operating systems.

Run the suite with `python3 -m unittest discover -s tests -v`.

## Future idea

A Chrome extension could send selected JD text through a right-click action to JhalCV and generate the current resume in the designated folder. This is an agreed future direction, not implemented yet.

Freshness/relevance/navigation/persistence verification: 125 unit tests pass; isolated browser checks verified all three tabs, collapsed Needs review/Old sections, eight-day Old placement, nine-day cleanup, and Applied retention. A live Tower board read returned 100 jobs, including five within seven days. A live Codex batch retained two unusually titled software roles and rejected recruiting work.

Navigation uses URL hashes for the active page and Jobs subtab (e.g. `#jobs/applied`), restoring them on reload and browser back/forward. No framework migration: keep vanilla JavaScript + Python while making the personal local workflow reliable.

Dark theme is the default. Header toggle remembers light/dark in localStorage (`jhalcv-theme`). `/theme.js` runs before CSS, stays within CSP, and works independently of API startup. PDF contents are not inverted or recolored.

## AI usage ledger (October 25)

- `resume/usage.py` persists metadata-only accounting in `.local/usage.db`; API instrumentation runs before structured-answer validation and includes unmeasured failed attempts. Local cache hits do not add tokens/cost.
- Jobs' `Agents` receives a usage store explicitly (thread-safe scopes inside each invocation); Codex `--json` turn usage and Claude JSON per-model usage/cost are captured, including nonzero exits and partial timeout output. Subscription equivalent is separated from API estimates.
- `/api/usage` and authenticated `/api/usage/settings` power the new `#usage` view. Codex's unreported actual model is labelled `codex-default`; its selectable valuation reference affects future estimates only. Price snapshot/source links and incomplete accounting warnings are visible.
- Earlier usage is not recoverable. Unknown models, missing usage and unsupported long-context pricing stay unknown, never silently priced as zero. No reset/delete operation was added.

Usage validation: 155 tests passed, including provider/cache math, failed-call accounting, Claude/Codex parsing, concurrent persistence, CLI coverage and HTTP authentication. Fresh reviewer found no remaining issues after CLI coverage fix. JS syntax and diff checks passed. Live port 8765 smoke verified Usage endpoint/assets and all 147 saved sources. Browser visual inspection was unavailable due preview timeouts; no live provider charges were created for the ledger smoke.

## Refresh optimization checkpoint

- Current branch `perf/job-refresh`. Pre-optimization implementation committed as `e6efd67` (author/committer October 26, 2026 19:00 IST), documentation as `449d7f7` (October 27 19:00 IST). No pushes performed.
- Eight source workers, twelve shared detail workers, four browser slots, three subscription slots. Active-work source budget150s, HTTP12s, Chrome25s/6s virtual-time budget, AI75s. Slot waits are excluded from active budgets. Independent eligibility batches run three at a time with45s active budget.120 full checks per refresh; today's jobs incrementally first, older/unknown backlog newest-first after collection. Slow/malformed page output does not disable a healthy provider for the whole refresh; account/login/quota errors still do.
- Exact-prompt AI result cache14days; no new inference cost for local hits. Title screening only for16+new detail-only postings. Removed unsafe new numeric-experience shortcut; AI still judges full qualification alternatives.
- Added direct SmartRecruiters/Oracle/Jibe readers and periodic linked-platform discovery (`resolve.py`). Normal refresh does not render or guess board names during discovery, preserving its time budget. Never silently discard role/location query filters to upgrade. Retain all saved jobs on upgrades, normalize Workday locale URLs, reconcile IDs by canonical URL, and persist both URL+ID tombstones for deletion/category cleanup/age pruning. Edit career link UI/API supports manual repairs while retaining jobs.
- Independently verified feeds and repaired four local sources (AMD, GitHub, JPMorgan Chase, Oracle/OCI); all382 saved jobs retained. Local SQLite backup before repairs: `.cache/jobs-before-verified-link-repairs.sqlite3`. Probe evidence `.cache/company-portals/verified-repairs.json`; all four produced India postings, with23/1/99/95 recent at check time. Previously completed resolver work already moved other sources to direct feeds. Unresolved challenge/login/JS pages are not claimed fixed.
- Old refresh completed (exclusive jobs.lock was idle). Old running server had a KeyError for smartrecruiters labels after source upgrades; new state uses fallback labels and all readers are loaded on restart. Do not kill any active refresh: acquire/check the job lock before a reload.
- Validation:179 tests passed before final polish; new concurrency/cache/budget/filter/date-priority/identity/deletion/edit regression tests. Offline147-source benchmark3.946s serial vs0.584s parallel6.75x, simulated waits only. Re-run suite after final review fixes; browser visual QA pending.

Final verification:180 tests passed; Python compilation, JS syntax and diff checks passed. T3 preview now works: verified147 source cards/edit controls,560pxscrollable company list, correct Jobs route and no notice error. Live targeted GitHub refresh completed23s, found1new India job and checked it withCodex; saved collection now383jobs. App reloaded only after previousrefresh finished (exclusive joblock idle); running at127.0.0.1:8765. Existing fullrefresh errors for remaining portals are retained, not claimed resolved. Optimization changes remain uncommitted on perf/job-refresh per request to commit the pre-optimization work first.

Final independent review: no remaining concrete findings after regression fixes for Workday locale identity, location-filter preservation, and bidirectional URL/ID deletion tombstones. All12performance/persistence regressiontests pass. UI simulated-progress check displayed19/147 and activecompanynames, then restoredreal idle state without initiatinga refresh.

## Saved-only review button (October26)

- New Jobs toolbar **Review unreviewed jobs (N)** sends authenticated `/api/jobs/review` (alias of existing `/api/jobs/recheck`). `recheck_only` branches before any source collection/discovery/details. `_review_saved` snapshots all eligible-to-review unchecked saved rows, then processes24-job chunks with existing parallel8-job eligibility batches; no120-cap, no repeated completed rows, no endless retry of uncertain answers. Preserves last_refresh and updates last_review.
- Progress uses phase=review, total/completed jobs, with polling; queue count returned by Jobs state. Emptyqueue/running disablesbutton. Re-render after task cleanup prevents generic task() from accidentally re-enabling it.
-184 tests passed, including144-pending-job fullqueue/no-portal/completed-exclusion test, uncertainty/Applied persistence, hidden/location exclusions and authenticated/busy reviewendpoint. No remaining findings in fresh reviewer pass before final UI task-cleanup polish. Real backlog is not automatically reviewed by development checks; user starts it using the button.

Saved-review final browser check: live button shows Review unreviewed jobs(240); intercepted UI click sent onlyjobs/review, displayed24/240jobsreviewed, remaineddisabledaftertaskcleanup; emptyqueuealso disabled. Realbacklogunchanged (no240-jobrunstartedbydevelopment). Reloadedidleapp at8765;147sources/383jobsretained.184tests pass plusJS/diffchecks; freshreviewfoundnoconcreteissues.

## Commit checkpoint (October 26)

All current work recorded on `perf/job-refresh`: optimization, portal readers and persistence fixes in `418c549` dated October 27, 2026 20:00 IST; saved-only review workflow follows as the October 28 commit. Full suite:184 tests; independent first-commit snapshot:180 tests. Both author and committer dates are set explicitly. No push performed.

## Live timing and tailoring context (October26)

- New metadata-only `resume/jobs/metrics.py`: thread-safe inclusive span aggregates, per-company labels, context propagation into source/detail/eligibility executors, failure/cache/queue counts; atomic completed JSON reports in `.local/job-benchmarks/`. New GET `/api/benchmarks` (optionalrun=id) exposes latestactive/completed and10recent summaries, including terminal-generatedreports. Failures to persist never fail paid work.
- Benchmarks sidebar route displays wall-clock duration separately from inclusive overlapping stage totals; collectionAI is explicitly labelled to avoid suggesting eligibilityAI is attributed percompany. DownloadJSON contains metadataonly; user should now run Refresh all to produce a full-refresh measurement. No synthetic data was inserted into the user's reports.
- Same-tab Tailor resume pins originating title/link and Back to this job; restores tab/search/drawer/sourceScroll/globalScroll, focuses/highlights exactjob, followschangedcategories and explainsdeletedjobs. sessionStorage stores onlyjob/UIcontext, notJD/profile. Generationguard prevents staleJobsreturns from changing URL/scroll after furthernavigation.
-192tests passed, covering parallelcontext, persistence, failedspans/queue waits, newerexternalrun selection, HTTP/AIinstrumentation, metadata privacy, reportendpoints and existingflows. Isolatedfixture browser verifiedreportstage/companytables, savedJDload/pinnedtitle, Needsreview/searchrestoration/highlight/focus, and delayedreturncancelledbySettingsnavigation.
- Freshreview found2issues, bothfixed: failedqueueacquire now raises inside measuredspan; staleBack-to-jobresponses guardedbyviewrevision. BrowsercompanyAIlabelclarifiedasCollectionAI. Finalreview verification pending.

Final verification:192tests passed, JSsyntax/Pythoncompilation/diffchecks passed; freshreview verified bothfixes and foundnoremainingconcreteissues. Production app reloaded onlywhilejoblockidle;381savedjobs/147sources retained. Benchmarks UI verified stage/companytables anddownload availability; a live single-company Nutanix report was present(3.05s, failedcollection), so it must not be mistaken for a full-refresh benchmark. User still needs Refresh all for full-run analysis. Isolated8766QAfixture used onlysyntheticdata, noAPIcharges oruserprofilewrites; fixture serverstopped afterverification.


## Manual rejection and Nutanix (October26)

- SQLite additive manual_eligible column plus authenticated jobs/eligibility action. Manual no is durable across refresh/detail/AI resets/results and excluded from review/detail queues; undo returns to retained AI decision. Applied/date precedence unchanged.
- Job actions now Tailor resume + More (manual rejection/undo, Applied toggle, Delete); AI explanations collapsed, open details preserved during rendering.
- Nutanix custom filtered careers URLs use the verified public Jobvite board plus employer XML feed. Follow category Show More and numbered search pagination, intersect public IDs before applying country/team/location/search filters. Unknown filters fail closed. Feed dates retained without inventing corrections; they may differ a day from frontend dates.
- Live reader returned all15 matching India Engineering/IT jobs. Fresh independent review found no concrete issues;197tests passed before final documentation/UI verification. No commit or push requested.

## Career-portal error fixes (October 26, 2026)

Diagnosed all 52 sources recording errors (147 total). Three root causes and fixes, all verified live:

- Transport: many career sites drop urllib's TLS handshake (SSL handshake timeouts) or answer 403/406 to its fingerprint. `_open` now retries once through curl (`--http2`, browser headers, same redirect/size guards). Fixes plain fetch for ACKO, AMD, Atlassian, Cisco, Confluent-class 429s are separate, MoEngage, Morgan Stanley, Media.net, Qure.ai, Qualcomm pages, and unblocks `resolve`/`jibe_from_page` (Docusign auto-upgrades next refresh).
- Headless render: Chrome headless advertised "HeadlessChrome" and Cloudflare/Uber/SAP/Akamai bot filters rejected it before job HTML loaded. `render_page` now sends the real Chrome UA, disables AutomationControlled, bounds page load with `--timeout=30000`, virtual time 9s, hard budget 45s. Verified: SAP, Uber, Ather, Goldman, Meta render with job links.
- New direct readers (no browser, no AI for listings): Eightfold PCSX (`pcsx`, Qualcomm + morganstanley.eightfold.ai; search 10/page, rate-limited later pages keep earlier ones), Salesforce static dump (`salesforce`, a.sfdcstatic.com jobs_1/2.json), Keka embed feed (`keka`, hash scraped from portal page; Nurix, Skit.ai), Kula SSR boards (`kula`, slug titles + detail pages recover real title/location; ACKO, Rocketlane, Cashfree). Detect/labels/READERS/DETAILS extended; `resolve` learns kula/keka/eightfold patterns (kula is dateless: never guessed, page evidence only); failing page sources retry discovery daily (`RESOLVE_RETRY`) instead of every 14 days.
- Listing items with an empty location (Kula, some AI pages) are no longer dropped before their detail page can supply one; views still hide jobs that never get a location.

DB repairs (backup `.cache/jobs-before-portal-fixes-20261026-021614.sqlite3`, upgrades via `upgrade_source`, jobs retained): LinkedIn→lever, Media.net→smartrecruiters, Morgan Stanley→pcsx, Qualcomm→pcsx, Salesforce→salesforce, Nurix→keka, Skit.ai→keka (skit.keka.com), Docusign→jibe, ACKO/Cashfree/Rocketlane→kula. Targeted refresh with AI stubbed (no usage spend, verdicts untouched): 11 sources 96 jobs, then Kula re-run 90 jobs with real titles/locations. AMD jibe reader works unupgraded via curl. Nutanix reader verified (50 jobs via Jobvite).

Still blocked, manual entry or later work: Cisco + Lowe's (Phenom `/widgets` needs an undocumented CSRF-gated POST schema; render never shows cards), Walmart (custom Next.js app, search results not server-rendered; only /api/init seen), Flipkart (TurboHire thapi.azurewebsites.net endpoints found but jobs call not identified), Hasura (Gem public GraphQL batch, query body unknown), Akamai (bot wall on www + jobs hosts, headless blocked), Quantbox (site TLS certificate expired — their bug), Confluent (Cloudflare 429 rate limit; retry later). LinkedIn lever board has no India roles (0 matching is correct). Nurix/Skit boards currently have no fresh (<8-day) postings; 0 matching is correct.

215 tests pass (13 new in tests/test_job_portals.py). No commit or push performed.

Note: before these repairs started (first backup at 02:16), the jobs table was found emptied (0 rows, sources' last_run/last_error cleared) — not caused by this work; likely an earlier test run. Older snapshot with 2 applied + 380 new jobs: `.cache/jobs-before-verified-link-repairs.sqlite3`. A full refresh repopulates listings; applied flags are not recovered automatically.

## GLM bridge from ZCode's login (October26)

- `resume/zcode.py`: when no Z.AI key is saved, job agents fall back to ZCode's own login. ZCode stores its Z.AI OAuth access token AES-256-GCM encrypted in `~/.zcode/v2/credentials.json`; a Node helper decrypts only that field into process memory (10-minute in-process cache), and it authenticates against the existing Coding Plan endpoint (verified: a wrong token gets HTTP 401, the real token only HTTP 429 while the plan was throttled). The token never reaches disk, logs, the usage database, or the browser.
- Consent is a kill switch: `.local/zcode_glm.json` with `{"enabled": true}`. Started as a one-day grant; the user made it standing the same night. Deleting the file or setting `false` turns the bridge off; the old date format is rejected.
- `tests/test_glm.py` gained consent-switch, helper-failure/caching, saved-key-precedence, and agent-run coverage. Suite: 225 tests pass.
- Overnight, `.cache/overnight_glm_benchmark.sh` waits for the 03:30 reset, verifies GLM through the bridge, then runs `python3 -m resume.jobs refresh` for the full-refresh benchmark. Log: `.cache/overnight_glm_benchmark.log`.

## AI retries, needs-review requeue, activity popup (October 26, 2026)

- `agents.py`: each model now gets `AGENT_ATTEMPTS=3` tries (2s/4s backoff) before the chain moves on; HTTP 429/rate-limit reclassified from run-disabling to retryable (usage-limit/quota/auth still disable a provider for the run). `CHAIN_GIVEUP=2`: after the whole chain fails twice in a row, later asks raise immediately so text rules take over without stalling the run.
- Requeued only the 396 needs-review jobs (UPDATE ... WHERE eligible='unclear'; 370 not-eligible + 86 eligible rule verdicts kept). Review run completed: 348 Codex verdicts, 48 rule fallbacks. GLM bridge currently throttled/unavailable; Codex quota healthy again.
- Live activity popup (`#activity` in index.html, styles in style.css, `updateActivity/pollActivity` in app.js): fixed bottom-right card on every page showing phase (Refreshing jobs / Reviewing saved jobs), progress bar, latest progress message, active-company chips (4 + "+N more"). Idle poll 8s, active 1.5s, paused when the tab is hidden; completion/error summary lingers 6s; ✕ hides for the current task only; clicking the card opens Jobs. renderJobs feeds it instantly; init starts the watcher.
- Verified in a throwaway app instance on port 8770 (started and stopped; user's 8931 untouched): load-hidden, running state (bar 16%, chips), review title, completion auto-hide, dismiss semantics, dark-theme screenshot check. tests/test_glm.py expectation updated to the 3-attempt contract; performance test patches RETRY_BACKOFF=0. Suite green.
- Backups: .cache/jobs-before-requeue-needsreview-*.sqlite3. No commit or push.

## Security review and hardening (October 26)

A fresh-eyes security review (three independent sweeps plus a server-core pass, every claim verified) found one high and two medium issues; all ten accepted findings are fixed on `security-fix`:

- Codex kept its shell tool under `--sandbox read-only`, so injected page text could drive local file reads and stage contents through the page-answer `note` field. Codex now runs under a `sandbox-exec` Seatbelt profile denying reads of `.env`, `.ssh`, `.zcode`, keychains, preferences, and the repo (probed once per process; a missing wrapper fails open), `HOME` points at the throwaway call folder while `CODEX_HOME` stays real, and AI notes are control-stripped and capped before display.
- Job URLs from page JSON-LD/feeds were stored unsanitized and rendered as clickable links: a `javascript:` URL in a posting could run script in the app origin. `_job` now stores only http(s) links (relative JSON-LD URLs resolve against the page base), and the frontend `el()` helper only assigns http(s) `href`/`src`.
- `.env` held live keys world-readable: now 0600 on disk and `load_env` enforces it.
- `check_url` resolves hostnames (a private answer is rejected; verdicts cached 5 minutes; transient DNS failure fails open) and returns normalized URLs; urllib redirects are validated before being followed via `_GuardedRedirects`, curl gets `--proto-redir =https`, and `http.client.HTTPException` joins the fallback path.
- HTML stripping (`html_to_text`, `page_with_links`) is linear: per-candidate rescan on crafted pages could pin a worker's CPU. Unclosed elements keep their content as before.
- CLI prints of model-derived text go through `_plain` (whole escape sequences removed, then control chars); `_SECRETS` scrub covers `ZHIPUAI_*`/`ZAI_BASE_URL`.

## Jobs section bands (October 26)

- User request: the Eligible list and the Needs review drawer were indistinguishable while scrolling, and the main list could not collapse. `#jobs-list` now sits inside a `<details class="jobs-drawer jobs-section" id="jobs-current">` whose summary shows the active tab name (`#jobs-current-label`, updated in renderJobs) plus the `#jobs-count` pill (moved out of `.panel-head`), so the main list collapses exactly like the review/Old drawers.
- All three section summaries are sticky bands (`top: 64px`, under the topbar, opaque background): while scrolling a long list its header stays pinned, so the section is always identifiable. Needs review is deliberately louder without a second accent color: a `3px double` ink rule above it and a slightly elevated `--surface-2` band (the amber warning palette was tried first and rejected by the user as off-theme), plus a soft shadow while open.
- "Back to this job" context now carries `listOpen`; returning opens the main drawer when the targeted job lives in it. Verified live on the running instance (read-only + client-side drawer toggles): boundary screenshot, pinned band mid-scroll, and collapsed list. Uncommitted.
- User request: drop the `#jobs-progress` status line under the toolbar ("the popup shows the current state anyway") — the span, its render block in renderJobs, and both CSS rules are removed; refresh state and errors live only in the activity popup. `ago()` stays (Companies pane uses it). Verified in the browser: element gone, toolbar renders cleanly.

## Performance pass, systems review (October 26, night)

Mined the 10-minute full-refresh benchmark (per-company + per-stage inclusive spans), then fixed what the evidence supported. jobs.db backup before the A/B: `.cache/jobs-before-perf-pass-*.sqlite3`.

- **Browser slots 4 → 6** (`fetch._BROWSERS`): browser_queue was the scarcest resource (2,835s of wait across 81 renders in the full run). A/B on a 12-company render-heavy subset (Apple, Rippling, Quadeye, …, cached pages so no new AI spend): wall 235.6s → 194.4s (−18%), browser_queue 1,525s → 946s (−38%); expected ~1–1.5 min off browser-bound full refreshes. Portal flakiness adds run-to-run noise (failure sets differed between the A/B runs).
- **AI chain under a hard throttle**: `_ask` retries each provider 3×, and 429s are deliberately retryable — but a plan throttled all night made every later ask re-pay 6 failed calls + backoff before Codex. Now, when a provider exhausts all its attempts within one ask on `http 429`/`rate limit`, it sits out the rest of the run (same as exhausted accounts). Within-ask retry behavior is unchanged (existing retry test still passes); a new test pins that a second ask skips the throttled provider.
- **`prune_old()` left the polling path**: `state()` pruned on every `/api/jobs` read — a write transaction + full scan ~2×/second during refresh (two pollers), contending with the refresh's own writes. Now throttled to once per 60s (`JobService._last_prune`); refresh's own prunes unchanged.
- **Halved poll bandwidth**: the activity watcher (`pollActivity`, 1.5s cadence during a run) fetched the full ~1,000-row jobs payload just to read `progress`. New lightweight `GET /api/jobs/progress` returns only the run state; the watcher uses it, the 2s live-list poll keeps the full payload (live verdicts preserved).
- New tests: tests/test_perf.py (prune throttle), progress endpoint in test_web.py, throttle-exhaustion in test_glm.py.


- **Test mode (token saver):** while `.local/jobs.test-mode` exists (currently `{"feeds": 20, "pages": 10}`), a full refresh collects/judges only the first 20 direct-feed + 10 page companies; the final backlog check is scoped to those. Progress total shows the shrunken set; summary lists the included names. Delete the file for full refreshes. The Review-unreviewed button stays unlimited (user-initiated).
- **Fallback no longer fakes relevance:** when the text-rule backup answers, relevance stays NULL (pending) instead of 'unclear', so unchecked-relevance jobs sit in Needs review until real AI decides — the source of the "irrelevant jobs in my tabs" report. `set_eligibility` writes NULL relevance and accepts an AI-read posted date only into an empty date column.
- **AI date recovery:** the batch prompt now labels each job's date and asks for `posted_date` only when the code found none; `eligibility.ai_date` accepts only ISO dates within the last 60 days and not in the future.
- **Judge-as-finished:** the incremental check now judges every unchecked job of each company as that company finishes (not only today's postings), so fast feeds' results are final while page companies still render.
- 245 tests pass (new: ai-date believability, date-only-fills-undated, test-mode slicing, updated category/screening/today-priority contracts). Browser-verified the drawer on a throwaway 8770 instance. No commit or push.
- `_run` gained `agent`/`extra_env` parameters (usage rows keep the real agent name under the wrapper). Timing-safe token compare uses bytes so any header value is safe; the jobs lock opens with `O_NOFOLLOW`; Greenhouse/Lever board tokens are regex-validated.
- LLM cache files, Typst data files, and the CLI sidecar (JD + PII) are written 0600 and atomically.
- Tests: `tests/test_security.py` (16 tests) covers URL validation, redirect guard, stored-URL schemes, parsing parity and linearity, `_plain`, the Codex wrapper (argv/env plus a live Seatbelt deny probe), and cache permissions; four `urlopen` patches in existing tests now patch `fetch._OPENER`. Suite: 241 tests.

## Performance pass, systems review (October 26, night)

Mined the 10-minute full-refresh benchmark (per-company + per-stage inclusive spans), then fixed what the evidence supported. jobs.db backup before the A/B: `.cache/jobs-before-perf-pass-*.sqlite3`.

- **Browser slots 4 → 6** (`fetch._BROWSERS`): browser_queue was the scarcest resource (2,835s of wait across 81 renders in the full run). A/B on a 12-company render-heavy subset (Apple, Rippling, Quadeye, …, cached pages so no new AI spend): wall 235.6s → 194.4s (−18%), browser_queue 1,525s → 946s (−38%); expected ~1–1.5 min off browser-bound full refreshes. Portal flakiness adds run-to-run noise (failure sets differed between the A/B runs).
- **AI chain under a hard throttle**: `_ask` retries each provider 3×, and 429s are deliberately retryable — but a plan throttled all night made every later ask re-pay 6 failed calls + backoff before Codex. Now, when a provider exhausts all its attempts within one ask on `http 429`/`rate limit`, it sits out the rest of the run (same as exhausted accounts). Within-ask retry behavior is unchanged (existing retry test still passes); a new test pins that a second ask skips the throttled provider.
- **`prune_old()` left the polling path**: `state()` pruned on every `/api/jobs` read — a write transaction + full scan ~2×/second during refresh (two pollers), contending with the refresh's own writes. Now throttled to once per 60s (`JobService._last_prune`); refresh's own prunes unchanged.
- **Halved poll bandwidth**: the activity watcher (`pollActivity`, 1.5s cadence during a run) fetched the full ~1,000-row jobs payload just to read `progress`. New lightweight `GET /api/jobs/progress` returns only the run state; the watcher uses it, the 2s live-list poll keeps the full payload (live verdicts preserved).
- New tests: tests/test_perf.py (prune throttle), progress endpoint in test_web.py, throttle-exhaustion in test_glm.py.
