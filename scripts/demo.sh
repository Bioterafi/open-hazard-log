#!/usr/bin/env bash
# Build a throwaway demo database (demo.db) with made-up data, for local viewing only.
# There's no login: serve it with  uv run ohl --db demo.db serve --as cso@demo.local
set -euo pipefail
cd "$(dirname "$0")/.."

DB=demo.db
rm -f "$DB" "$DB-wal" "$DB-shm"

ohl() { uv run ohl --db "$DB" "$@"; }

ohl init
ohl user add cso@demo.local --name "Demo CSO" --role cso --registration "GMC 0000000"
ohl user add viewer@demo.local --name "Demo Viewer" --role viewer
ohl system add "Ward Results Viewer" --version 2.3 --as cso@demo.local \
  --intended-use "Displays laboratory results for all inpatients across the Trust. Made-up demo system." \
  --clinical-context "Used by ward doctors and nurses when reviewing bloods; results inform prescribing."
ohl import seed/sample_results_viewer.csv --system 1 --as cso@demo.local
ohl system configure --system 1 --docs "$(pwd)/seed/demo_docs" --confirm-no-pid --as cso@demo.local
ohl scan --system 1 --as cso@demo.local --replay seed/demo_scan_output.json   # saved output, no API call
ohl seed-self --as cso@demo.local
ohl verify
echo "Demo ready. Run: uv run ohl --db $DB serve --as cso@demo.local   then open http://127.0.0.1:8000"
