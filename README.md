# Open Hazard Log by Bioterafi

A DCB0129/DCB0160 hazard log where agents propose and the Clinical Safety Officer decides.
Open source under the Apache-2.0 licence. Website: https://hazardlog.bioterafi.xyz · Source: https://github.com/Bioterafi/open-hazard-log
This hazard log is the first module of a wider clinical safety platform covering the whole DCB0129/DCB0160 lifecycle.
The spec is in [SPEC.md](SPEC.md). Built so far: **M1** (the log), **M2** (agent scan and review), **M3** (evidence and code-change checks) and **M4** (release 0.1.1: making the public release safe to run).

## What M1 does

- Hazards, causes, controls, evidence and risk ratings, with risk derived from the system's risk matrix.
- Only a CSO can change the log. Every change is written in the same transaction as a hash-chained audit event.
- **Issue version** (DCB0129 3.3.2): the CSO freezes a numbered, signed snapshot.
- Import from CSV/XLSX in the NHS hazard log template v3.6 layout. Export the same layout (CSV, XLSX, JSON), plus a deployer-actions list and the audit trail.
- `ohl verify` re-walks the audit chain and names the first broken event.

## What M2 adds

- **Scan** (`ohl scan` or Run scan on the Review page): reads the system's docs and chosen code, and proposes hazards through four lenses. It uses `claude-opus-5-5` with structured output, effort high, and refusal fallback on. The model that actually answered is stored on every proposal.
- **Quote check:** every citation must appear word for word in its source. If no quote is found, the proposal is hidden; if only some are found, it's marked low confidence.
- **No write path:** the scan can only store proposals plus one run event. A test checks the scan module for write calls.
- **Review:** the CSO rates new hazards blind, and the agent's rating appears only afterwards. The CSO can then approve, edit and approve, or reject with a reason. Rejection reasons are fed into the next scan. Each decision records the blind rating, the suggested rating, the final version and the time spent.
- **Replay:** `ohl scan --replay saved.json` runs the whole pipeline on a saved model output with no API call. The demo uses this, and it can be used to re-check a prompt change offline.

Not yet: "Ask someone" on a proposal.

## What M3 adds

- **Checks** (`ohl check` or Run checks on the Review page), with no AI involved. They re-hash every evidence file and look for commits since the last check that touch a cause's code paths.
- **First run:** each file's current hash is recorded as its baseline.
- **Later runs:** a changed or missing file, or linked code that changed, becomes a proposal. The checks never change the log themselves, and a test enforces this.
- **Decisions:** re-confirm evidence, mark a control invalid, mark a hazard reviewed, or reject. Changed evidence can be re-confirmed in bulk.
- **Freshness:** the dashboard shows when the checks last ran, and flags them after 7 days.

To run them nightly, add a cron line such as:

```bash
0 6 * * * cd /path/to/hazlog && uv run ohl check --system 1 --as you@trust.nhs.uk
```

The demo builds `demo-repo/`, a made-up git repository. It deletes a test, changes linked code and edits a doc, so the checks have something to find.

## What M4 (0.1.1) adds

- **Only this computer, only its own pages.** The app answers only requests addressed to `localhost` or a loopback address such as `127.0.0.1` (anything else gets 400) and coming from this computer (anything else gets 403), even if it's started with `uvicorn --host 0.0.0.0`. Requests carrying proxy headers (`Via`, `Forwarded`, `X-Forwarded-*`, `X-Real-IP` and the like) are refused too, but a proxy that adds none can't be detected, so never run it behind one. Every change needs the token from your own browser session; a change the browser reports as coming from another page or from inside a frame is refused; and no page can be shown in a frame. Together these stop a web page you visit from acting as you, by DNS rebinding, a hidden form or clickjacking. Only your own visit to a proposal starts its time-on-proposal clock.
- **Runs stored whole or not at all.** A check run stores its observations, its proposals and its run event in one transaction, so a failure part-way can't lose a re-review. A scan or check that fails after starting leaves a `run_failed` event in the audit trail, and doesn't count as a fresh run. If the database can't take even that record (it's locked, full or read-only), the message says so instead of claiming it.
- **Feedback per pipeline.** Rejected check findings no longer reach the scan prompt; only rejected scan proposals do.
- **Safe exports.** XLSX cells are stored as text, so a hazard titled `=1+1` can't run as a formula when another organisation opens the file. In CSV every cell is quoted, and text that a spreadsheet could take as the start of a formula (at the start of a cell, or after `;`, a tab or a line break) gets a leading apostrophe. Importing an OHL CSV export removes those apostrophes again; other CSV files are imported exactly as they are. XLSX is the better format to share.
- **Packaged resources.** The prompt, risk matrix and OHL's own hazard log are inside the package, so an installed wheel works away from a checkout. `ohl/prompts/MANIFEST` holds each prompt's SHA-256: an edited prompt fails the tests and the scan refuses to run. Released prompts' hashes are also pinned in the tests, and CI refuses a pull request that changes an existing prompt or MANIFEST line.
- **Replay smoke set.** Ten saved model outputs (`evals/hazard.scan/smoke.json`) run through the real scan pipeline on every change. It checks the pipeline is deterministic, not how well a model finds hazards.
- **`ohl backup`** copies the database while the app is running, leaves out the session secret, and verifies the copy. `ohl verify` opens a database read-only.
- **`ohl issue`** issues a version from the command line.
- **CI** on every push to `main` and every pull request: tests, lint, a dependency audit, the demo, and a clean install of the built wheel that also serves a page. uv is pinned, actions are pinned to commit SHAs, and every Python package comes from `uv.lock` or `build-constraints.txt` (the build backend) with its hash checked.

## Run it

You need [uv](https://docs.astral.sh/uv/) and git.

```bash
git clone https://github.com/Bioterafi/open-hazard-log
cd open-hazard-log
uv sync
./scripts/demo.sh                 # throwaway demo.db with made-up data
uv run ohl --db demo.db serve --as cso@demo.local     # then open http://127.0.0.1:8000
```

**There's no login.** The app runs on one computer as one named person, chosen with `--as` (or the only CSO, if there is just one). Every change is still recorded under that person's name. Because anyone who can reach the app acts as that person, it only listens on this computer (127.0.0.1), and it answers only requests from this computer addressed to `localhost` or a loopback address. Don't put it behind a proxy.

## Real use

```bash
uv run ohl init
uv run ohl user add you@trust.nhs.uk --name "Your Name" --role cso --registration "GMC 1234567"
uv run ohl system add "Product" --version 1.0 --as you@trust.nhs.uk
uv run ohl import old-hazard-log.xlsx --system 1 --as you@trust.nhs.uk
uv run ohl serve --as you@trust.nhs.uk
```

To scan, set the sources and confirm they contain no patient data, then run (needs `ANTHROPIC_API_KEY`; each run is billed):

```bash
uv run ohl system configure --system 1 --repo ~/code/product --docs docs --code-globs "src/results/*" --confirm-no-pid --as you@trust.nhs.uk
uv run ohl scan --system 1 --as you@trust.nhs.uk
```

Back up while the app is running, and keep copies on another computer:

```bash
uv run ohl backup ohl-2026-11-17.db       # a verified copy, without the session secret
uv run ohl --db ohl-2026-11-17.db verify
```

Other commands: `ohl issue --system 1 --confirm --as you@trust.nhs.uk` (sign a version), `ohl seed-self [--repo .]` (OHL's own hazard log, unrated), `ohl export`, `ohl audit`, `ohl verify [--file audit.json]`.

## Landing page

`site/index.html` is a static product page in the same style. It's self-contained, so it can be hosted anywhere:

```bash
python3 -m http.server 8767 --directory site     # then open http://127.0.0.1:8767
```

It's live at https://hazardlog.bioterafi.xyz. Cloudflare serves `site/` as static files from a Worker with no code, configured in `wrangler.jsonc`, and `site/_headers` sets the security headers. To publish a change, run:

```bash
wrangler deploy
```

Only the landing page is deployed. The app is never deployed: it has no login and is meant to run on your own computer.

## Tests

```bash
uv run pytest
uv run ruff check ohl tests
./scripts/wheel_smoke.sh      # build the wheel, install it in a clean virtualenv, run a replay scan
```

CI runs these, the demo and a dependency audit on every push to `main` and every pull request (`.github/workflows/ci.yml`).

## Layout

```
ohl/db.py         schema (append-only triggers on event and issue), transactions
ohl/audit.py      hash chain, verify, export, replay
ohl/risk.py       risk matrix lookup from config
ohl/service.py    every change to the log: users, systems, hazards, controls, issue version
ohl/scan.py       agent scan: gather sources, prompt, model call, schema and quote checks
ohl/proposals.py  proposal store, blind rating, approve / edit / reject, check decisions
ohl/checks.py     evidence hashes and git-diff checks (no AI, no write path to the log)
ohl/dashboard.py  dashboard numbers
ohl/importer.py   CSV/XLSX import (template layout, matched by header text)
ohl/export.py     CSV/XLSX/JSON export, deployer actions
ohl/app.py        web pages; templates/ and static/ohl.css
ohl/local.py      answers only this computer (Host header and client address)
ohl/cli.py        command line
ohl/prompts/      the scan prompt, and MANIFEST with each prompt file's SHA-256
ohl/config/       risk_matrix.toml: default scheme (DCB0129 guidance Tables 7-10, an example)
ohl/seed/         ohl_self.toml: OHL's own hazard log; each control in place links to its test
seed/             demo only: a made-up sample log, demo docs and a saved scan output
evals/            hazard.scan/smoke.json: the replay smoke set
scripts/          demo.sh, wheel_smoke.sh
build-constraints.txt   the build backend, pinned with hashes
```

The look follows anthropic.com's design scheme: ivory page, serif titles, rounded panels and pill buttons (style only, no Anthropic marks). Newsreader and Hanken Grotesk load from Google Fonts; where that's blocked, pages fall back to Georgia and the system sans.

## Safety and scope

- **It supports clinical risk management; it isn't it.** Your Clinical Risk Management System, Plan and Safety Case Report remain your organisation's responsibility. A named, registered Clinical Safety Officer makes every decision in the log.
- **It doesn't make a product compliant**, and it isn't affiliated with or endorsed by NHS England.
- **The agent scan supplements a hazard workshop.** It never replaces one.
- **No login.** Run it on your own computer only. `ohl serve` refuses to listen anywhere else. Shared or hosted use needs real sign-in first.
- **No patient data.** Scans read docs and code only, after the CSO confirms the sources contain no patient-identifiable data.
- Open Hazard Log keeps its own hazard log (`ohl seed-self --repo .`), with each control marked in place linked to the test that shows it. Each tagged release gets an issued version of it; see [docs/RELEASING.md](docs/RELEASING.md). Report safety concerns about the tool itself as issues marked "safety".

## Contributing

Issues and pull requests are welcome.

- Run `uv run pytest` before sending a change. The suite never calls the Anthropic API.
- Some tests guard safety properties: the scan and checks have no write path to the log, every quote is checked, ratings are blind, and the audit chain catches tampering. Don't weaken them to make a change pass.
- Prompt changes get a new file and version (`ohl/prompts/scan_v2.md`) plus a line in `ohl/prompts/MANIFEST`, never an edit in place.
- Changing an expectation in `evals/hazard.scan/smoke.json` is a safety-relevant change: say why in the commit.

## Licence

Copyright 2026 Bioterafi. Licensed under the Apache License, Version 2.0: see [LICENSE](LICENSE) and [NOTICE](NOTICE).
