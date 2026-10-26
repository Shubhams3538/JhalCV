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
