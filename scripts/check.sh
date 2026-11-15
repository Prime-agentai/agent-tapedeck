#!/usr/bin/env bash
# The whole offline gate: no network, no API key, no real agent CLI needed.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
PY="${PYTHON:-python3}"

echo "== compileall";  "$PY" -m compileall -q src tests examples
if "$PY" -c "import mypy" 2>/dev/null; then
  echo "== mypy --strict"; "$PY" -m mypy --strict src/tapedeck
else
  echo "== mypy not available, skipped"
fi
echo "== pytest (replay mode)"; "$PY" -m pytest -q --tapedeck-mode=replay tests examples
echo "== cassette scan"; "$PY" -m tapedeck scan examples/cassettes/*.json
echo "OK"
