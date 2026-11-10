#!/usr/bin/env bash
# Run the payload power switch integration tests against a live deployment.
#
# 1. Starts the RehearsalDeployment flight software and the F´ GDS headless
#    (no browser). The flight-software link listens on 127.0.0.1 only
#    (see CommandRehearsal/RehearsalDeployment/fprime-gds.yml).
# 2. Waits until the flight software has connected to the GDS.
# 3. Runs pytest on CommandRehearsal/RehearsalDeployment/test/int.
# 4. Always stops the GDS and flight software, and exits with pytest's status.
#
# Usage (from the project folder, after building):  scripts/integration-test.sh [extra pytest args]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY="$ROOT/CommandRehearsal/RehearsalDeployment"
PORT=50000
APP="$ROOT/build-artifacts/Darwin/CommandRehearsal_RehearsalDeployment/bin/CommandRehearsal_RehearsalDeployment"

if [[ ! -x "$APP" ]]; then
    echo "error: deployment not built ($APP missing). Run: fprime-util build" >&2
    exit 2
fi
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "error: TCP port $PORT is already in use; stop the other fprime-gds first" >&2
    exit 2
fi

# shellcheck disable=SC1091
source "$ROOT/fprime-venv/bin/activate"
cd "$DEPLOY"
mkdir -p logs
GDS_LOG="$DEPLOY/logs/integration-gds.log"

# Start the GDS (which launches the flight software) in its own process group,
# so the whole group can be stopped together.
set -m
fprime-gds -g none >"$GDS_LOG" 2>&1 &
GDS_PID=$!
set +m

stop_gds() {
    kill -TERM -- "-$GDS_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do
        kill -0 -- "-$GDS_PID" 2>/dev/null || return 0
        sleep 0.5
    done
    kill -KILL -- "-$GDS_PID" 2>/dev/null || true
}
trap stop_gds EXIT

echo "Waiting for the flight software to connect to the GDS on 127.0.0.1:$PORT ..."
for _ in $(seq 1 60); do
    if lsof -nP -iTCP:"$PORT" -sTCP:ESTABLISHED >/dev/null 2>&1; then
        break
    fi
    if ! kill -0 "$GDS_PID" 2>/dev/null; then
        echo "error: fprime-gds exited early; see $GDS_LOG" >&2
        exit 2
    fi
    sleep 1
done
if ! lsof -nP -iTCP:"$PORT" -sTCP:ESTABLISHED >/dev/null 2>&1; then
    echo "error: flight software did not connect within 60 s; see $GDS_LOG" >&2
    exit 2
fi
sleep 2  # let the GDS pipeline finish starting

set +e
python -m pytest test/int -v "$@"
STATUS=$?
set -e
exit "$STATUS"
