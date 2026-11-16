#!/usr/bin/env bash
# Build a throwaway demo database (demo.db) with made-up data, for local viewing only.
# Demo login: cso@demo.local with the password below. Never use these outside a local demo.
set -euo pipefail
cd "$(dirname "$0")/.."

DB=demo.db
DEMO_PASSWORD="${DEMO_PASSWORD:-demo-only-password}"
rm -f "$DB" "$DB-wal" "$DB-shm"

ohl() { uv run ohl --db "$DB" "$@"; }

ohl init
echo "$DEMO_PASSWORD" | ohl user add cso@demo.local --name "Demo CSO" --role cso --registration "GMC 0000000" --password-stdin
echo "$DEMO_PASSWORD" | ohl user add viewer@demo.local --name "Demo Viewer" --role viewer --password-stdin
ohl system add "Ward Results Viewer" --version 2.3 --as cso@demo.local \
  --intended-use "Displays laboratory results for all inpatients across the Trust. Made-up demo system." \
  --clinical-context "Used by ward doctors and nurses when reviewing bloods; results inform prescribing."
ohl import seed/sample_results_viewer.csv --system 1 --as cso@demo.local
ohl seed-self --as cso@demo.local
ohl verify
echo "Demo ready. Run: uv run ohl --db $DB serve   then log in as cso@demo.local"
