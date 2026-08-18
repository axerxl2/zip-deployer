#!/usr/bin/env bash
#
# Runs every check: Python correctness tests (both HTTP transports), the
# browser end-to-end tests, and a short benchmark.
#
#   ./tests/run_all.sh          # tests + quick benchmark
#   ./tests/run_all.sh --full   # tests + the full 3000-file benchmark
#
set -euo pipefail

cd "$(dirname "$0")/.."

green() { printf '\033[32m%s\033[0m\n' "$1"; }
bold()  { printf '\033[1m%s\033[0m\n' "$1"; }

bold "== Python tests (stdlib transport) =="
ZIP_DEPLOYER_TRANSPORT=stdlib python3 -W ignore::ResourceWarning tests/test_deploy.py

bold "== Python tests (requests transport) =="
if python3 -c 'import requests' 2>/dev/null; then
    ZIP_DEPLOYER_TRANSPORT=requests python3 -W ignore::ResourceWarning tests/test_deploy.py
else
    echo "requests not installed — skipping (the stdlib transport is what ships by default)"
fi

bold "== Browser tests =="
if [ -d node_modules/jszip ]; then
    node tests/test_browser.mjs
else
    echo "jszip not installed — run 'npm install --no-save jszip' to enable the browser tests"
fi

bold "== Benchmark =="
if [ "${1:-}" = "--full" ]; then
    python3 -W ignore tests/benchmark.py --files 3000 --legacy-files 400
else
    python3 -W ignore tests/benchmark.py --files 500 --legacy-files 150
fi

green "All checks completed."
