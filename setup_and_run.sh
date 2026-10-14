#!/usr/bin/env bash
# One-command setup and launch for macOS / Linux.
#
#   bash setup_and_run.sh
#
# Creates a virtual environment, installs the kit, builds the dummy SAP data,
# checks every rule, runs the end-to-end demo, then opens the planning
# assistant. No API key is needed; without one the assistant runs offline.
# Pass --no-chat to stop before the assistant opens.
set -euo pipefail
cd "$(dirname "$0")"

step() { echo; echo "== $1"; }

PYTHON=$(command -v python3 || command -v python || true)
if [ -z "$PYTHON" ]; then
  echo "Python was not found. Install Python 3.10 or newer, then run this again." >&2
  exit 1
fi

if [ ! -d .venv ]; then
  step "Creating a virtual environment"
  "$PYTHON" -m venv .venv
fi
if [ -x .venv/bin/python ]; then VPY=.venv/bin/python; else VPY=.venv/Scripts/python; fi

step "Installing the kit (first time takes a minute)"
"$VPY" -m pip install --quiet --disable-pip-version-check -e ".[dev]"
step "Building the dummy SAP data"
"$VPY" -m sapkit generate
step "Checking every rule against the built-in answers"
"$VPY" -m sapkit verify
mkdir -p out
step "Running the end-to-end demo (outreach, replies, rerun)"
"$VPY" -m sapkit demo --offline > out/demo.log
step "Writing the report"
"$VPY" -m sapkit report

# Let Claude Code use the tools when it is opened in this folder (no API key needed).
printf '{"mcpServers": {"sapkit": {"command": "%s", "args": ["-m", "sapkit", "mcp"]}}}\n' "$(pwd)/$VPY" > .mcp.json

echo
echo "Setup complete."
echo "  Supplier drafts:  out/drafts/"
echo "  Report:           out/report/summary.md"
echo "  Run the tests:    $VPY -m pytest"
echo "  Use real Claude:  run 'claude' in this folder and approve the 'sapkit' tools"

if [ "${1:-}" != "--no-chat" ]; then
  echo
  "$VPY" -m sapkit ask
fi
