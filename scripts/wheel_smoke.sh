#!/usr/bin/env bash
# Install the built wheel into a clean virtualenv, away from this checkout, and run a replay scan.
# Catches files the package needs but the wheel leaves out. The build backend comes from
# build-constraints.txt and the dependencies from uv.lock, both with hashes checked.
# No API key needed.
set -euo pipefail
cd "$(dirname "$0")/.."
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

uv build --wheel --build-constraints build-constraints.txt --require-hashes -o "$WORK/dist" >/dev/null
uv export --frozen --no-dev --no-emit-project --format requirements-txt -o "$WORK/requirements.txt" >/dev/null
uv venv -q --python 3.12 "$WORK/venv"
uv pip install -q --python "$WORK/venv/bin/python" --require-hashes -r "$WORK/requirements.txt"
uv pip install -q --python "$WORK/venv/bin/python" --no-deps "$WORK"/dist/*.whl
cp -R seed/demo_docs "$WORK/demo_docs"
cp seed/demo_scan_output.json "$WORK/"

cd "$WORK"   # nothing below can read from the checkout
ohl() { "$WORK/venv/bin/ohl" --db "$WORK/smoke.db" "$@"; }
"$WORK/venv/bin/python" -c "import ohl; assert 'site-packages' in ohl.__file__, ohl.__file__"
ohl init
ohl user add cso@smoke.local --name "Smoke CSO" --role cso --registration "GMC 0000000"
ohl system add "Smoke" --version 1 --intended-use "Smoke test of the installed wheel." --as cso@smoke.local
ohl system configure --system 1 --docs "$WORK/demo_docs" --confirm-no-pid --as cso@smoke.local
ohl scan --system 1 --as cso@smoke.local --replay demo_scan_output.json | tee scan.txt
grep -q "3 proposals for review, 1 hidden" scan.txt
ohl seed-self --as cso@smoke.local
ohl export --system 2 --format xlsx --out own-log.xlsx
ohl backup backup.db
"$WORK/venv/bin/ohl" --db backup.db verify

# The web app: templates and static files must be in the wheel too.
PORT=$("$WORK/venv/bin/python" -c "import socket; s = socket.socket(); s.bind(('127.0.0.1', 0)); print(s.getsockname()[1])")
"$WORK/venv/bin/ohl" --db "$WORK/smoke.db" serve --port "$PORT" --as cso@smoke.local >serve.log 2>&1 &
SERVER=$!   # the server process itself, not a subshell
trap 'kill "$SERVER" 2>/dev/null || true; wait "$SERVER" 2>/dev/null || true; rm -rf "$WORK"' EXIT
for _ in $(seq 50); do curl -sf -o /dev/null "http://127.0.0.1:$PORT/static/ohl.css" && break; sleep 0.2; done
curl -sf "http://127.0.0.1:$PORT/s/1" | grep -q "Smoke CSO"
curl -sfL "http://127.0.0.1:$PORT/s/1/review" | grep -q "Enter your rating first"   # review.html, after the redirect
curl -sf -o /dev/null "http://127.0.0.1:$PORT/static/ohl.css"
echo "wheel smoke test passed"
