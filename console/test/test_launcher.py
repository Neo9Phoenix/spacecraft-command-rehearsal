"""Regression test: scripts/console.sh refuses to attach to a non-F´ process on port 50000.

Needs the deployment built and TCP port 50000 free (skipped otherwise).
Run from the project folder:  fprime-venv/bin/python -m pytest console/test -v
"""

import os
import signal
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "build-artifacts/Darwin/CommandRehearsal_RehearsalDeployment/bin/CommandRehearsal_RehearsalDeployment"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_launcher_refuses_unrelated_listener_on_fsw_port():
    if not APP.exists():
        pytest.skip("deployment not built")
    squatter = socket.socket()  # an unrelated process (this test) holds the flight-software port
    try:
        squatter.bind(("127.0.0.1", 50000))
    except OSError:
        squatter.close()
        pytest.skip("port 50000 is busy (is a GDS running?)")
    squatter.listen()
    try:
        proc = subprocess.Popen([str(ROOT / "scripts/console.sh"), "--port", str(_free_port())],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.communicate()
            pytest.fail("launcher attached to an unrelated listener instead of refusing")
    finally:
        squatter.close()
    assert proc.returncode == 2, out
    assert "not this project's F´ GDS" in out, out
    assert f"PID {os.getpid()}" in out, out  # names the process holding the port
    assert "Operator console on" not in out, out
