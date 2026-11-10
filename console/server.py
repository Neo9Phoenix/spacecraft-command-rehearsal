"""Operator console backend: a small adapter between the browser and the F´ GDS.

It connects to the running GDS the same way the integration tests' ``fprime_test_api``
fixture does (ConfigDrivenParser + StandardPipelineParser.pipeline_factory, reading
fprime-gds.yml from the deployment folder), sends commands with
``pipeline.send_command`` (what ``IntegrationTestAPI.send_command`` calls), and
receives events and telemetry from the pipeline's decoders.

The HTTP server listens on 127.0.0.1 only. Simulation only; nothing drives hardware.

Usage (normally through scripts/console.sh):
    python console/server.py [--port 8765]
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from console.core import COMMANDS, SCENARIOS, Console, ConsoleError  # noqa: E402

DEPLOYMENT_DIR = HERE.parent / "CommandRehearsal" / "RehearsalDeployment"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
BIND = "127.0.0.1"
LOG = logging.getLogger("console")


# ----------------------------------------------------------------------------
# F´ GDS pipeline
# ----------------------------------------------------------------------------


def _jsonable(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def _fsw_time(data):
    try:
        return data.time.to_readable()
    except Exception:
        return str(data.time)


def connect_pipeline():
    """Build the GDS pipeline exactly as the pytest fixture does, from the deployment folder."""
    from fprime_gds.executables.cli import ConfigDrivenParser, StandardPipelineParser

    os.chdir(DEPLOYMENT_DIR)  # fprime-gds.yml and settings.ini are found from here, as in the tests
    arg_ns, _, _ = ConfigDrivenParser.parse_known_args([StandardPipelineParser], arguments=[], client=True)
    return StandardPipelineParser.pipeline_factory(arg_ns)


def attach(console, pipeline):
    """Register event and channel consumers that feed the console."""
    from fprime_gds.common.handlers import DataHandler

    class Consumer(DataHandler):
        def __init__(self, convert, sink):
            self.convert, self.sink = convert, sink

        def data_callback(self, data, sender=None):
            try:
                self.sink(self.convert(data))
            except Exception:  # never let a bad packet kill the decoder thread
                LOG.exception("Failed to process downlinked item")

    def event(data):
        sev = data.template.get_severity()
        return {
            "name": data.template.get_full_name(),
            "args": [_jsonable(getattr(a, "val", a)) for a in (data.get_args() or [])],
            "severity": getattr(sev, "name", str(sev)),
            "text": data.get_display_text(),
            "fsw_time": _fsw_time(data),
        }

    def channel(data):
        val = data.get_val()
        return {
            "name": data.template.get_full_name(),
            "value": _jsonable(getattr(val, "val", val)),
            "fsw_time": _fsw_time(data),
        }

    pipeline.coders.register_event_consumer(Consumer(event, console.on_event))
    pipeline.coders.register_channel_consumer(Consumer(channel, console.on_channel))


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    console = None  # set in main()
    port = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    # Only answer requests addressed to this machine's loopback name, so a web page
    # elsewhere can't reach the console through DNS rebinding.
    def _host_ok(self):
        host = self.headers.get("Host", "")
        return host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def _send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send_json(403, {"error": "Use http://127.0.0.1:%d" % self.port})
        path = self.path.split("?", 1)[0]
        if path in STATIC:
            name, ctype = STATIC[path]
            data = (HERE / "static" / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/state":
            self._send_json(200, self.console.snapshot())
        elif path == "/api/config":
            self._send_json(200, {"scenarios": SCENARIOS,
                                  "commands": {k: v["label"] for k, v in COMMANDS.items()}})
        elif path == "/api/stream":
            self._stream()
        else:
            self._send_json(404, {"error": "Not found"})

    def _stream(self):
        """Server-sent events: one full snapshot per state change, plus a keepalive."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        console = self.console
        seen = -1
        try:
            while True:
                with console.changed:
                    if console.version == seen:
                        console.changed.wait(timeout=5.0)
                    snap = console.snapshot() if console.version != seen else None
                if snap is None:
                    self.wfile.write(b": keepalive\n\n")
                else:
                    seen = snap["version"]
                    self.wfile.write(b"data: " + json.dumps(snap).encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_POST(self):
        if not self._host_ok():
            return self._send_json(403, {"error": "Use http://127.0.0.1:%d" % self.port})
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"):
            return self._send_json(403, {"error": "Cross-origin request refused"})
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self._send_json(415, {"error": "Send application/json"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(min(length, 64 * 1024)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._send_json(400, {"error": "Body must be a JSON object"})

        console = self.console
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/command":
                rec = console.send(str(body.get("kind", "")), body.get("mode"))
                return self._send_json(202, {"id": rec["id"]})
            if path == "/api/scenario/start":
                console.start_scenario(str(body.get("id", "")))
            elif path == "/api/scenario/send":
                rec = console.send_step()
                return self._send_json(202, {"id": rec["id"]})
            elif path == "/api/scenario/advance":
                console.advance_scenario()
            elif path == "/api/scenario/stop":
                console.stop_scenario()
            else:
                return self._send_json(404, {"error": "Not found"})
        except ConsoleError as exc:
            return self._send_json(exc.status, {"error": str(exc)})
        return self._send_json(200, {"ok": True})


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765, help="HTTP port on 127.0.0.1 (default: %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every HTTP request")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="[console] %(levelname)s %(message)s")

    # Bind first, so a busy port fails before the pipeline threads start.
    server = ThreadingHTTPServer((BIND, args.port), Handler)
    server.daemon_threads = True

    pipeline = connect_pipeline()
    try:
        opcodes = {c["mnemonic"]: pipeline.dictionaries.command_name[c["mnemonic"]].get_op_code()
                   for c in COMMANDS.values()}

        def send(mnemonic, cmd_args):
            pipeline.send_command(mnemonic, cmd_args)

        console = Console(send, opcodes)
        attach(console, pipeline)
        Handler.console, Handler.port = console, args.port

        stop = threading.Event()

        def ticker():
            while not stop.wait(0.25):
                console.tick()

        threading.Thread(target=ticker, name="console-tick", daemon=True).start()
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
        LOG.info("Operator console on http://%s:%d (Ctrl-C to stop)", BIND, args.port)
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        stop.set()
    finally:
        server.server_close()
        pipeline.disconnect()
        LOG.info("Console stopped")
    # Pipeline helper threads (file uplinker) are not daemons; don't wait on them.
    time.sleep(0.2)
    os._exit(0)


if __name__ == "__main__":
    main()
