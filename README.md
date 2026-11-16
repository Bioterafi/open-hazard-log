# Open Hazard Log

A DCB0129/DCB0160 hazard log where agents propose and the Clinical Safety Officer decides.
The spec is in [SPEC.md](SPEC.md). This is **M1**: the log itself, with no agent yet.

## What M1 does

- Hazards, causes, controls, evidence and risk ratings, with risk derived from the system's risk matrix.
- Only a CSO can change the log. Every change is written in the same transaction as a hash-chained audit event.
- **Issue version** (DCB0129 3.3.2): the CSO freezes a numbered, signed snapshot.
- Import from CSV/XLSX in the NHS hazard log template v3.6 layout. Export the same layout (CSV, XLSX, JSON), plus a deployer-actions list and the audit trail.
- `ohl verify` re-walks the audit chain and names the first broken event.

## Run it

```bash
uv sync
./scripts/demo.sh                 # throwaway demo.db with made-up data
uv run ohl --db demo.db serve     # http://127.0.0.1:8000, log in as cso@demo.local
```

The demo password is in `scripts/demo.sh`. It's for local demos only.

## Real use

```bash
uv run ohl init
uv run ohl user add you@trust.nhs.uk --name "Your Name" --role cso --registration "GMC 1234567"
uv run ohl system add "Product" --version 1.0 --as you@trust.nhs.uk
uv run ohl import old-hazard-log.xlsx --system 1 --as you@trust.nhs.uk
uv run ohl serve
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
ohl/importer.py   CSV/XLSX import (template layout, matched by header text)
ohl/export.py     CSV/XLSX/JSON export, deployer actions
ohl/app.py        web pages; templates/ and static/ohl.css
ohl/cli.py        command line
config/risk_matrix.toml   default scheme (DCB0129 guidance Tables 7-10, an example)
seed/             OHL's own hazards; a made-up sample log
```

The typewriter font (Courier Prime, SIL OFL) goes in `ohl/static/fonts/`. Until it's added, pages fall back to Courier New.
