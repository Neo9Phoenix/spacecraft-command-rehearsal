#!/usr/bin/env bash
# Start the operator console against the local F´ simulator.
#
# 1. Starts the RehearsalDeployment flight software and the F´ GDS headless
#    (no browser), as scripts/integration-test.sh does. If this project's
#    fprime-gds is already running on TCP port 50000, the console attaches to it
#    instead. Any other process on that port is refused with an explanation.
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
    # Attach only to an fprime-gds serving this deployment's dictionary; refuse anything else.
    LISTENER_PID="$(lsof -nP -iTCP:"$FSW_PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 || true)"
    LISTENER_CMD="$(ps -o command= -p "$LISTENER_PID" 2>/dev/null || true)"
    DICT="$ROOT/build-artifacts/Darwin/CommandRehearsal_RehearsalDeployment/dict/RehearsalDeploymentTopologyDictionary.json"
    if [[ "$LISTENER_CMD" != *"fprime_gds.executables.comm"* || "$LISTENER_CMD" != *"$DICT"* ]]; then
        echo "error: TCP port $FSW_PORT is in use by PID ${LISTENER_PID:-unknown}, which is not this project's F´ GDS:" >&2
        echo "  ${LISTENER_CMD:-(process details unavailable)}" >&2
        echo "The console attaches only to an fprime-gds started for RehearsalDeployment. Stop that process" >&2
        echo "or free port $FSW_PORT, then run scripts/console.sh again." >&2
        exit 2
    fi
    echo "This project's fprime-gds is already listening on 127.0.0.1:$FSW_PORT (PID $LISTENER_PID); attaching the console to it."
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
