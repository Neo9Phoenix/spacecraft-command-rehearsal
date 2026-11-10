#!/usr/bin/env bash
# Start the operator console against the local F´ simulator.
#
# 1. Starts the RehearsalDeployment flight software and the F´ GDS headless
#    (no browser), as scripts/integration-test.sh does. If an fprime-gds is
#    already running (TCP port 50000 in use), the console attaches to it instead.
# 2. Starts the console adapter (console/server.py) on http://127.0.0.1:8765.
# 3. On Ctrl-C, stops the adapter and, if this script started them, the GDS
#    and flight software.
#
# Usage (from the project folder, after building):  scripts/console.sh [--port N]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY="$ROOT/CommandRehearsal/RehearsalDeployment"
FSW_PORT=50000
APP="$ROOT/build-artifacts/Darwin/CommandRehearsal_RehearsalDeployment/bin/CommandRehearsal_RehearsalDeployment"

if [[ ! -x "$APP" ]]; then
    echo "error: deployment not built ($APP missing). Run: fprime-util build" >&2
    exit 2
fi

# shellcheck disable=SC1091
source "$ROOT/fprime-venv/bin/activate"
cd "$DEPLOY"
mkdir -p logs
GDS_LOG="$DEPLOY/logs/console-gds.log"
GDS_PID=""

stop_gds() {
    [[ -n "$GDS_PID" ]] || return 0
    kill -TERM -- "-$GDS_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do
        kill -0 -- "-$GDS_PID" 2>/dev/null || return 0
        sleep 0.5
    done
    kill -KILL -- "-$GDS_PID" 2>/dev/null || true
}
trap stop_gds EXIT

if lsof -nP -iTCP:"$FSW_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "An fprime-gds is already listening on 127.0.0.1:$FSW_PORT; attaching the console to it."
else
    echo "Starting the flight software and GDS (log: $GDS_LOG) ..."
    set -m
    fprime-gds -g none >"$GDS_LOG" 2>&1 &
    GDS_PID=$!
    set +m
    for _ in $(seq 1 60); do
        lsof -nP -iTCP:"$FSW_PORT" -sTCP:ESTABLISHED >/dev/null 2>&1 && break
        if ! kill -0 "$GDS_PID" 2>/dev/null; then
            echo "error: fprime-gds exited early; see $GDS_LOG" >&2
            exit 2
        fi
        sleep 1
    done
fi

# The adapter prints the URL. It binds to 127.0.0.1 only.
python "$ROOT/console/server.py" "$@"
