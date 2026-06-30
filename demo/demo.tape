# demo/demo.tape — reproducible terminal GIF for quackpack.
#
# Rendered with VHS (https://github.com/charmbracelet/vhs):
#
#     vhs demo/demo.tape          # writes demo/quackpack.gif
#
# VHS replays the keystrokes below in a headless terminal and records a GIF,
# so the demo is deterministic and re-renderable by anyone — no manual screen
# capture. The commands mirror demo/demo.sh (and the README quickstart) so all
# three stay in lock-step. See demo/README.md for setup.
#
# Prereqs: quackpack on PATH (`pipx install .` / `uv tool install .`) and run
# from the repo root so the examples/ paths resolve.

Output demo/quackpack.gif

Require quackpack

Set Shell        bash
Set FontSize     17
Set Width        1100
Set Height       740
Set Padding      24
Set Theme        "Catppuccin Mocha"
Set TypingSpeed  35ms
Set PlaybackSpeed 1.0

# Use a throwaway pack so the recording never touches a real ~/.quackpack.
Hide
Type "export QUACKPACK_HOME=$(mktemp -d)" Enter
Type "clear" Enter
Show

# --- 1. Stash three real queries from the bundled examples/ -----------------
Type 'quackpack add -n top-regions -f examples/top-regions.sql --tags demo --desc "Revenue by region"' Enter
Sleep 900ms
Type 'quackpack add -n big-orders -f examples/big-orders.sql --tags demo --desc "Orders at or above :min"' Enter
Sleep 900ms
Type 'quackpack add -n product-mix -f examples/product-mix.sql --tags demo --desc "Units + revenue share by product"' Enter
Sleep 1200ms

# --- 2. See what's in the pack ----------------------------------------------
Type "quackpack ls" Enter
Sleep 2200ms

# --- 3. Rerun a saved query against the sample CSV --------------------------
Type "quackpack run top-regions --file examples/sales.csv" Enter
Sleep 2400ms

# --- 4. Bind a :param at run time -------------------------------------------
Type "quackpack run big-orders --file examples/sales.csv --param min=300" Enter
Sleep 2600ms

# --- 5. Recall a query by anything you remember -----------------------------
Type "quackpack search region" Enter
Sleep 2400ms

# --- 6. JSON straight out for jq / scripts ----------------------------------
Type "quackpack run top-regions --file examples/sales.csv --format json" Enter
Sleep 3000ms
