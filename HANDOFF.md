# JhalCV handoff

Updated October 24, 2026. Branch: `v2`. The user requested the accompanying commit be dated October 25, 2026. Do not push unless asked.

## Product and preferences

JhalCV runs locally: `./start.sh` opens the browser frontend. For development, use `python3 app.py --no-browser --port 8765`. Keep the existing CLI working.

Users enter a job description, manage projects, import GitHub READMEs, preview a one-page resume, and save it to a chosen folder. Project descriptions should convey the actual project, match the JD naturally, and stay within the existing length budget. No project introductions. Removing projects must persist in the saved profile.

The user wants implementation without code walkthroughs. Read `AGENTS.local.md`; defaults are slopper, ego-review, and expertise none.

## Changes in the accompanying commit

- Native saving-folder chooser, with manual path fallback. The folder contains one current `shubham_Resume.pdf`; previous copies go into `Archive`. Saving uses a per-folder lock and atomic replacement.
- Success notices disappear after five seconds; errors remain visible.
- Settings support multiple OpenAI, Gemini, Anthropic, and OpenRouter keys, a preferred key, and automatic failover. The startup reminder offers a permanent opt-out.
- Model selectors show readable provider-specific choices and a selected default, plus an Other model option. Existing saved keys have a Change model editor; changing a model does not require re-entering its key. Existing models are preserved.
- Saved credentials stay on the local backend. Browser responses contain fixed dummy masks only, never a key or its prefix. Provider failures and generated output are checked to prevent secret leakage into reports or caches.

## Private data and validation

Do not overwrite the user's profile or environment file, or print credentials. `profile.json`, `.local/`, `.cache/`, and `build/` are ignored. Credentials are stored with mode 0600 in `.local/credentials.json`; they are not encrypted at rest.

Validation: 56 unit tests passed; JS syntax, Python compilation, and diff checks passed. Isolated browser checks covered model selection, saved-model updates, dummy-only credential state, reminder choices, and disappearing notices. A live OpenAI call succeeded with the new default `gpt-5.4-mini`. Other providers were tested with transport fixtures, not live accounts. Windows/Linux native folder dialogs have not been verified on those operating systems.

Run the suite with `python3 -m unittest discover -s tests -v`.

## Future idea

A Chrome extension could send selected JD text through a right-click action to JhalCV and generate the current resume in the designated folder. This is an agreed future direction, not implemented yet.
