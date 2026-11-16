#!/usr/bin/env bash
# Build a throwaway demo database (demo.db) with made-up data, for local viewing only.
# Also builds demo-repo/, a tiny made-up git repository the checks can watch.
# There's no login: serve it with  uv run ohl --db demo.db serve --as cso@demo.local
set -euo pipefail
cd "$(dirname "$0")/.."

DB=demo.db
REPO=demo-repo
rm -f "$DB" "$DB-wal" "$DB-shm"
rm -rf "$REPO"

ohl() { uv run ohl --db "$DB" "$@"; }
g() { git -C "$REPO" -c user.name="Demo Developer" -c user.email=dev@demo.local "$@"; }

ohl init
ohl user add cso@demo.local --name "Demo CSO" --role cso --registration "GMC 0000000"
ohl user add viewer@demo.local --name "Demo Viewer" --role viewer
ohl system add "Ward Results Viewer" --version 2.3 --as cso@demo.local \
  --intended-use "Displays laboratory results for all inpatients across the Trust. Made-up demo system." \
  --clinical-context "Used by ward doctors and nurses when reviewing bloods; results inform prescribing."
ohl import seed/sample_results_viewer.csv --system 1 --as cso@demo.local

# A made-up product repository holding the files the sample log's causes and evidence point to.
mkdir -p "$REPO"/{src/results,tests,docs/design,docs/sop}
echo "export const onPatientSwitch = () => {};"          > "$REPO/src/results/context.ts"
echo "export const UNITS = ['mmol/L'];"                  > "$REPO/src/results/units.ts"
echo "export const STALE_AFTER_MINUTES = 120;"           > "$REPO/src/results/stale.ts"
echo "export const MAX_COMMENT = 120;"                   > "$REPO/src/results/comment.tsx"
echo "test('mismatched patient id discards render');"   > "$REPO/tests/patient_id.test.ts"
echo "test('slow network context switch');"             > "$REPO/tests/context_switch.test.ts"
echo "test('unknown units rejected');"                  > "$REPO/tests/units.test.ts"
echo "test('full comment shown on expand');"            > "$REPO/tests/comment.test.ts"
printf '# Units mapping\nAll potassium results are normalised to mmol/L.\n' > "$REPO/docs/design/units.md"
printf '# Quarterly units review\nLab liaison checks the mapping table.\n'  > "$REPO/docs/sop/units-review.md"
printf '# Downtime\nPrint the last 24 hours of results.\n'                  > "$REPO/docs/sop/downtime.md"
g init -q
g add -A
g commit -q -m "Ward Results Viewer 2.3"

ohl system configure --system 1 --repo "$(pwd)/$REPO" --docs "$(pwd)/seed/demo_docs" --confirm-no-pid --as cso@demo.local
ohl scan --system 1 --as cso@demo.local --replay seed/demo_scan_output.json   # saved output, no API call
ohl check --system 1 --as cso@demo.local                                       # first run: records baselines

# Some development happens: a test is deleted, linked code and a design doc change.
rm "$REPO/tests/context_switch.test.ts"
echo "export const STALE_AFTER_MINUTES = 240;" > "$REPO/src/results/stale.ts"
printf '# Units mapping\nPotassium results are normalised to mmol/L; mEq/L is converted 1:1.\n' > "$REPO/docs/design/units.md"
g add -A
g commit -q -m "Relax stale-result threshold; tidy tests"
ohl check --system 1 --as cso@demo.local                                       # finds the changes

ohl seed-self --as cso@demo.local
ohl verify
echo "Demo ready. Run: uv run ohl --db $DB serve --as cso@demo.local   then open http://127.0.0.1:8000"
