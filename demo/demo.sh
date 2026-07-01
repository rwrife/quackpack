#!/usr/bin/env bash
#
# demo/demo.sh — the canonical quackpack walkthrough.
#
# This is the single source of truth for the README demo: it runs the real
# CLI against the bundled examples/ dataset, so what you see is exactly what
# you get. It powers two things:
#
#   1. The recorded GIF/cast in the README (rendered via demo/demo.tape, see
#      demo/README.md). The "tape" types these very commands.
#   2. A CI smoke test (tests/test_demo.py) that runs this script end-to-end
#      and asserts the headline output still appears — so the demo can't rot.
#
# It is fully self-contained and side-effect-free: it stashes into a throwaway
# QUACKPACK_HOME (a temp dir) and never touches your real ~/.quackpack pack.
#
# Usage:
#   demo/demo.sh            # run the walkthrough (fast, for CI / piping)
#   QP_DEMO_PACED=1 demo/demo.sh   # add small pauses, for live recording
#
# Run it from the repository root so the examples/ paths resolve.

set -euo pipefail

# --- locate the repo root (this script lives in demo/) -----------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"
cd "${REPO_ROOT}"

if [[ ! -f examples/sales.csv ]]; then
  echo "demo: cannot find examples/sales.csv — run this from the repo root." >&2
  exit 1
fi

# --- isolate: never touch the user's real pack -------------------------------
QUACKPACK_HOME="$(mktemp -d)"
export QUACKPACK_HOME
cleanup() { rm -rf "${QUACKPACK_HOME}"; }
trap cleanup EXIT

# --- pacing: slow down only when recording -----------------------------------
PAUSE="${QP_DEMO_PAUSE:-0.9}"
pause() { if [[ -n "${QP_DEMO_PACED:-}" ]]; then sleep "${PAUSE}"; fi; }

# Echo a prompt + command so the recording reads like a real shell session,
# then actually run it.
run() {
  printf '\033[1;32m$\033[0m %s\n' "$*"
  pause
  "$@"
  echo
  pause
}

# --- the walkthrough ---------------------------------------------------------
# 1. Stash three real queries from the bundled examples/.
run quackpack add -n top-regions -f examples/top-regions.sql --tags demo --desc "Revenue by region"
run quackpack add -n big-orders  -f examples/big-orders.sql  --tags demo --desc "Orders at or above :min"
run quackpack add -n product-mix -f examples/product-mix.sql --tags demo --desc "Units + revenue share by product"

# 2. See what's in the pack.
run quackpack ls

# 3. Rerun a saved query against the sample CSV — no re-typing the SQL.
run quackpack run top-regions --file examples/sales.csv

# 4. Bind a :param at run time.
run quackpack run big-orders --file examples/sales.csv --param min=300

# 5. Recall a query by anything you remember about it.
run quackpack search region

# 6. Pull JSON straight out for jq / scripts.
run quackpack run top-regions --file examples/sales.csv --format json

printf '\033[1;36m# stash it once, rerun it forever 🦆📦\033[0m\n'
