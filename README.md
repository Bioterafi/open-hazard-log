# Open Hazard Log

A DCB0129/DCB0160 hazard log where agents propose and the Clinical Safety Officer decides.
The spec is in [SPEC.md](SPEC.md). Built so far: **M1** (the log) and **M2** (agent scan and review).

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

Not yet: "Ask someone" on a proposal, and bulk approve. Bulk approve comes with the M3 evidence checks.

## Run it

```bash
uv sync
./scripts/demo.sh                 # throwaway demo.db with made-up data
uv run ohl --db demo.db serve --as cso@demo.local     # then open http://127.0.0.1:8000
```

**There's no login.** The app runs on one computer as one named person, chosen with `--as` (or the only CSO, if there is just one). Every change is still recorded under that person's name. Because anyone who can reach the app acts as that person, it only listens on this computer (127.0.0.1) and refuses any other address.

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

Other commands: `ohl seed-self` (OHL's own hazard log, unrated), `ohl export`, `ohl audit`, `ohl verify [--file audit.json]`.

## Tests

```bash
uv run pytest
```

## Layout

```
ohl/db.py         schema (append-only triggers on event and issue), transactions
ohl/audit.py      hash chain, verify, export, replay
ohl/risk.py       risk matrix lookup from config
ohl/service.py    every change to the log: users, systems, hazards, controls, issue version
ohl/scan.py       agent scan: gather sources, prompt, model call, schema and quote checks
ohl/proposals.py  proposal store, blind rating, approve / edit / reject
ohl/importer.py   CSV/XLSX import (template layout, matched by header text)
ohl/export.py     CSV/XLSX/JSON export, deployer actions
ohl/app.py        web pages; templates/ and static/ohl.css
ohl/cli.py        command line
prompts/scan_v1.md        the scan prompt (a change means a new file and version)
config/risk_matrix.toml   default scheme (DCB0129 guidance Tables 7-10, an example)
seed/             OHL's own hazards; a made-up sample log, demo docs and a saved scan output
```

The look follows anthropic.com's design scheme: ivory page, serif titles, rounded panels and pill buttons (style only, no Anthropic marks). Source Serif 4 and DM Sans load from Google Fonts; where that's blocked, pages fall back to Georgia and the system sans.
