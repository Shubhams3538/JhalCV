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
