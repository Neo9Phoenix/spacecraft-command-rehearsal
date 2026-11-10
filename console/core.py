"""Operator console core: command tracking, evidence checks and rehearsal scenarios.

This module has no F´ imports so it can be unit tested on its own. The server
(console/server.py) feeds it decoded events and telemetry from the F´ GDS
pipeline and gives it a ``send`` callable that wraps ``pipeline.send_command``.

Rules this module enforces:

* One command in flight at a time. A command is in flight until its evidence
  is complete or its evidence window (10 s) runs out.
* No command is sent unless downlink is live (telemetry seen in the last 3 s).
* A command's evidence is: the dispatcher's response event (OpCodeCompleted or
  OpCodeError), any payloadSwitch events, and the first sample of the channel
  the command reports on (PayloadSwitch or PayloadMode) received after it was
  sent. This mirrors what the integration tests assert.
* A rehearsal step is confirmed only when that evidence matches the step's
  expected result. If the window runs out, the step is Unconfirmed and the
  operator decides whether to send again.

The SAFE-mode interlock lives in the flight software. Nothing here blocks
POWER_ON in SAFE; the console sends it and reports what F´ did.
"""

import copy
import itertools
import threading
import time

SWITCH = "CommandRehearsal.payloadSwitch"
SWITCH_TLM = f"{SWITCH}.PayloadSwitch"
MODE_TLM = f"{SWITCH}.PayloadMode"
STATE_EVENT = f"{SWITCH}.SwitchStateChanged"
REJECTED_EVENT = f"{SWITCH}.PowerOnRejected"
MODE_EVENT = f"{SWITCH}.ModeChanged"
CMD_DISPATCHED = "CdhCore.cmdDisp.OpCodeDispatched"
CMD_DONE = "CdhCore.cmdDisp.OpCodeCompleted"
CMD_ERROR = "CdhCore.cmdDisp.OpCodeError"

EVIDENCE_TIMEOUT = 10.0  # seconds; same as the integration tests (telemetry downlinks at 1 Hz)
LINK_TIMEOUT = 3.0  # seconds without any downlink before the link is shown as lost
LATE_WINDOW = 60.0  # seconds after sending during which late evidence is attached to an Unconfirmed command
TIMELINE_LIMIT = 200
MODES = ("NORMAL", "SAFE")

COMMANDS = {
    "POWER_ON": {"label": "Power on", "mnemonic": f"{SWITCH}.POWER_ON", "channel": SWITCH_TLM},
    "POWER_OFF": {"label": "Power off", "mnemonic": f"{SWITCH}.POWER_OFF", "channel": SWITCH_TLM},
    "SET_MODE": {"label": "Set mode", "mnemonic": f"{SWITCH}.SET_MODE", "channel": MODE_TLM},
}

CHANNEL_LABELS = {SWITCH_TLM: "payload power", MODE_TLM: "flight mode"}


def short(name):
    """Last part of a dotted F´ name: CommandRehearsal.payloadSwitch.PayloadSwitch -> PayloadSwitch."""
    return name.rsplit(".", 1)[-1]


# ----------------------------------------------------------------------------
# Rehearsal scenarios
# ----------------------------------------------------------------------------


def _step(kind, title, outcome, mode=None, response="OK", events=(), forbid=(), tlm=None, setup=False):
    cmd = COMMANDS[kind]
    return {
        "kind": kind,
        "mode": mode,
        "title": title,
        "action": cmd["label"] + (f" {mode}" if mode else ""),
        "outcome": outcome,
        "setup": setup,
        "expect": {
            "response": response,
            "events": [{"name": n, "args": a} for n, a in events],
            "forbid_events": list(forbid),
            "telemetry": {"channel": cmd["channel"], "value": tlm},
        },
    }


SCENARIOS = [
    {
        "id": "normal-startup",
        "title": "Normal Startup",
        "summary": "Power the payload in NORMAL mode.",
        "start": {"mode": "NORMAL", "power": "OFF"},
        "steps": [
            _step("POWER_OFF", "Start with the payload off", "Command accepted. Telemetry shows OFF.",
                  tlm="OFF", setup=True),
            _step("SET_MODE", "Select NORMAL mode", "Command accepted. Telemetry shows NORMAL.",
                  mode="NORMAL", tlm="NORMAL", setup=True),
            _step("POWER_ON", "Power on the payload",
                  "Command accepted. Switch reports ON. Telemetry shows ON.",
                  events=[(STATE_EVENT, ["ON"])], forbid=[REJECTED_EVENT], tlm="ON"),
        ],
    },
    {
        "id": "safe-rejection",
        "title": "Safe Mode Rejection",
        "summary": "Confirm F´ refuses Power on in SAFE mode.",
        "start": {"mode": "SAFE", "power": "OFF"},
        "steps": [
            _step("POWER_OFF", "Start with the payload off", "Command accepted. Telemetry shows OFF.",
                  tlm="OFF", setup=True),
            _step("SET_MODE", "Enter SAFE mode", "Command accepted. Telemetry shows SAFE.",
                  mode="SAFE", tlm="SAFE", setup=True),
            _step("POWER_ON", "Try to power on in SAFE",
                  "Power on rejected in SAFE mode. PowerOnRejected event. Telemetry still shows OFF.",
                  response="EXECUTION_ERROR", events=[(REJECTED_EVENT, ["SAFE"])], forbid=[STATE_EVENT],
                  tlm="OFF"),
        ],
    },
    {
        "id": "safe-power-off",
        "title": "Power Off in Safe Mode",
        "summary": "Enter SAFE with the payload on, then power it off.",
        "start": {"mode": "SAFE", "power": "ON"},
        "steps": [
            _step("SET_MODE", "Select NORMAL mode", "Command accepted. Telemetry shows NORMAL.",
                  mode="NORMAL", tlm="NORMAL", setup=True),
            _step("POWER_ON", "Power on the payload", "Command accepted. Telemetry shows ON.",
                  events=[(STATE_EVENT, ["ON"])], tlm="ON", setup=True),
            _step("SET_MODE", "Enter SAFE with the payload on",
                  "Command accepted. Telemetry shows SAFE. No switch change, so the payload stays ON.",
                  mode="SAFE", forbid=[STATE_EVENT], tlm="SAFE", setup=True),
            _step("POWER_OFF", "Power off in SAFE",
                  "Command accepted. Switch reports OFF. Telemetry shows OFF.",
                  events=[(STATE_EVENT, ["OFF"])], forbid=[REJECTED_EVENT], tlm="OFF"),
        ],
    },
]
SCENARIOS_BY_ID = {s["id"]: s for s in SCENARIOS}


# ----------------------------------------------------------------------------
# Errors returned to the HTTP layer
# ----------------------------------------------------------------------------


class ConsoleError(Exception):
    status = 400


class Busy(ConsoleError):
    status = 409


class Disconnected(ConsoleError):
    status = 503


# ----------------------------------------------------------------------------
# Console state
# ----------------------------------------------------------------------------


class Console:
    """Thread-safe console state. Feed it with on_event/on_channel; call tick() periodically."""

    def __init__(self, send, opcodes, clock=time.time):
        """
        Args:
            send: callable(mnemonic, args) that sends a command through the GDS pipeline
            opcodes: {mnemonic: opcode} for the COMMANDS mnemonics
            clock: wall-clock source (seconds), replaceable in tests
        """
        self._send = send
        self._opcodes = dict(opcodes)
        self._switch_opcodes = set(self._opcodes.values())
        self._clock = clock
        self._ids = itertools.count(1)
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.version = 0

        self.last_packet_at = None  # ground time of the last event or channel of any kind
        self.last_tlm = None  # {"name", "ground_time", "fsw_time"} of the last channel sample
        self.link = "waiting"  # waiting | connected | lost
        self.link_epoch = 0  # increments every time the link (re)connects
        self.telemetry = {SWITCH_TLM: None, MODE_TLM: None}
        self.timeline = []  # newest last internally; reversed in snapshots
        self.records = {}
        self.in_flight = None
        self.run = None  # active rehearsal: {"scenario", "step", "attempts": {step: [ids]}, "state"}

    # -- helpers ----------------------------------------------------------------

    def _notify(self):
        self.version += 1
        self.changed.notify_all()

    def _touch_link(self, now):
        self.last_packet_at = now
        if self.link != "connected":
            self.link = "connected"
            self.link_epoch += 1
            self._notify()

    def _append(self, entry):
        self.timeline.append(entry)
        self.records[entry["id"]] = entry
        while len(self.timeline) > TIMELINE_LIMIT:
            old = self.timeline.pop(0)
            if old is not self.in_flight:
                self.records.pop(old["id"], None)

    # -- downlink ---------------------------------------------------------------

    def on_event(self, ev):
        """ev: {"name", "args", "severity", "text", "fsw_time"}; ground_time is added here."""
        with self.lock:
            now = self._clock()
            ev = dict(ev, ground_time=now)
            self._touch_link(now)
            name = ev["name"]
            rec = self.in_flight
            if name in (CMD_DISPATCHED, CMD_DONE, CMD_ERROR):
                opcode = ev["args"][0] if ev.get("args") else None
                if rec is not None and opcode == rec["opcode"] and rec["response"] is None:
                    if name == CMD_DISPATCHED:
                        rec["dispatched"] = rec["dispatched"] or ev
                    else:
                        status = "OK" if name == CMD_DONE else str(ev["args"][1])
                        rec["response"] = {"status": status, "event": ev}
                    self._advance(rec, now)
                elif rec is not None and opcode == rec["opcode"]:
                    # A second answer for this opcode while it is in flight (e.g. a retry's own answer
                    # after an earlier attempt's late one): keep it with the command as evidence.
                    rec["events"].append(ev)
                elif self._late(opcode=opcode, ev=ev, now=now):
                    pass
                elif opcode in self._switch_opcodes:
                    self._activity(ev, now)
                else:
                    return  # another component's command (e.g. SAY_HI from the GDS web UI)
            elif name.startswith(SWITCH + "."):
                if rec is not None:
                    rec["events"].append(ev)
                    self._advance(rec, now)
                elif not self._late(ev=ev, now=now):
                    self._activity(ev, now)
            else:
                return
            self._notify()

    def on_channel(self, ch):
        """ch: {"name", "value", "fsw_time"}; ground_time is added here."""
        with self.lock:
            now = self._clock()
            ch = dict(ch, ground_time=now)
            self._touch_link(now)
            self.last_tlm = {"name": ch["name"], "ground_time": now, "fsw_time": ch.get("fsw_time")}
            name = ch["name"]
            if name not in self.telemetry:
                return  # last-telemetry time is computed on the client; no broadcast needed
            self.telemetry[name] = dict(ch, epoch=self.link_epoch)
            rec = self.in_flight
            if rec is not None and rec["channel"] == name and rec["telemetry"] is None:
                rec["telemetry"] = ch
                self._advance(rec, now)
            elif rec is None:
                self._late(channel=ch, now=now)
            self._notify()

    @staticmethod
    def _settled(rec):
        """True once a command's response and telemetry have both arrived, on time or late."""
        late = rec.get("late", [])
        responded = rec["response"] is not None or any(x["name"] in (CMD_DONE, CMD_ERROR) for x in late)
        reported = rec["telemetry"] is not None or any(x["name"] == rec["channel"] for x in late)
        return responded and reported

    def _late(self, now, opcode=None, ev=None, channel=None):
        """Attach evidence that arrives after an Unconfirmed command's window closed.

        It is shown with that command but never changes its outcome: the operator
        still decides whether to send again. Returns True if it was attached.
        """
        rec = next((r for r in reversed(self.timeline) if r["type"] == "command"), None)
        if rec is None or rec["outcome"] != "unconfirmed" or now - rec["sent_at"] > LATE_WINDOW:
            return False
        if channel is not None:
            # Only the first late sample of the command's own channel is kept.
            if channel["name"] != rec["channel"] or any(x["name"] == channel["name"] for x in rec["late"]):
                return False
            rec["late"].append(channel)
        else:
            if opcode is not None and opcode != rec["opcode"]:
                return False
            rec["late"].append(ev)
        note = " Late evidence arrived; still Unconfirmed."
        if not rec["summary"].endswith(note):
            rec["summary"] += note
        return True

    def _activity(self, ev, now):
        """payloadSwitch activity this console did not command (e.g. from the GDS web UI)."""
        last = self.timeline[-1] if self.timeline else None
        if last and last["type"] == "activity" and now - last["sent_at"] < 3.0:
            last["events"].append(ev)
            return
        self._append({
            "id": next(self._ids), "type": "activity", "label": "Activity not sent from this console",
            "sent_at": now, "events": [ev], "summary": "Events from a command sent by another GDS client.",
        })

    def tick(self):
        """Expire evidence windows and update the link state. Call a few times per second."""
        with self.lock:
            now = self._clock()
            if self.link == "connected" and (self.last_packet_at is None or now - self.last_packet_at > LINK_TIMEOUT):
                self.link = "lost"
                self._notify()
            rec = self.in_flight
            if rec is not None and now - rec["sent_at"] > EVIDENCE_TIMEOUT:
                self._finish(rec, now, timed_out=True)
                self._notify()

    # -- commanding -------------------------------------------------------------

    def send(self, kind, mode=None, expect=None, origin=None):
        """Send one command. Returns the timeline record (a copy)."""
        with self.lock:
            if kind not in COMMANDS:
                raise ConsoleError(f"Unknown command {kind!r}")
            if kind == "SET_MODE" and mode not in MODES:
                raise ConsoleError("Set mode needs NORMAL or SAFE")
            if kind != "SET_MODE":
                mode = None
            if self.link != "connected":
                raise Disconnected("GDS link is down. Commands are disabled until telemetry resumes.")
            if self.in_flight is not None:
                raise Busy("Another command is in flight. Wait for its result.")
            cmd = COMMANDS[kind]
            args = [mode] if mode else []
            now = self._clock()
            opcode = self._opcodes[cmd["mnemonic"]]
            # F´ responses carry only the opcode. If an earlier attempt of this command is still
            # Unconfirmed and unanswered, its late evidence could arrive during this attempt, and the
            # two can't be told apart. Such an attempt can never be Confirmed.
            unsettled = [r for r in self.timeline
                         if r["type"] == "command" and r["opcode"] == opcode and r["outcome"] == "unconfirmed"
                         and now - r["sent_at"] <= LATE_WINDOW and not self._settled(r)]
            rec = {
                "id": next(self._ids),
                "type": "command",
                "kind": kind,
                "label": cmd["label"] + (f" {mode}" if mode else ""),
                "mnemonic": cmd["mnemonic"],
                "args": args,
                "opcode": opcode,
                "channel": cmd["channel"],
                "origin": origin,
                "expect": expect or {"response": None, "events": [], "forbid_events": [],
                                     "telemetry": {"channel": cmd["channel"], "value": None}},
                "sent_at": now,
                "dispatched": None,
                "response": None,
                "events": [],
                "telemetry": None,
                "phase": "sent",
                "outcome": None,
                "checks": [],
                "summary": "Command sent. Waiting for response.",
                "completed_at": None,
                "late": [],  # evidence that arrived after the window closed
                "ambiguous_with": [r["id"] for r in unsettled],  # earlier attempts it can't be told apart from
            }
            try:
                self._send(cmd["mnemonic"], args)
            except Exception as exc:  # pipeline refused it; record that and release the slot
                rec.update(phase="done", outcome="send_failed", completed_at=now,
                           summary=f"Command not sent: {exc}")
                self._append(rec)
                self._notify()
                raise ConsoleError(f"Command not sent: {exc}") from exc
            self._append(rec)
            self.in_flight = rec
            self._notify()
            return copy.deepcopy(rec)

    def _checks(self, rec):
        exp = rec["expect"]
        checks = []
        resp = rec["response"]
        want = exp.get("response")
        if resp is None:
            checks.append({"label": "Command response", "ok": None, "detail": "No response yet"})
        else:
            ok = True if want is None else resp["status"] == want
            detail = f"{resp['status']} ({short(resp['event']['name'])})"
            label = "Command response" if want is None else f"Command response is {want}"
            checks.append({"label": label, "ok": ok, "detail": detail})
        names = [(e["name"], e.get("args") or []) for e in rec["events"]]
        for req in exp.get("events", []):
            args = [str(a) for a in req.get("args") or []]
            seen = any(n == req["name"] and [str(a) for a in a_][: len(args)] == args for n, a_ in names)
            label = f"Event {short(req['name'])}({', '.join(args)})"
            checks.append({"label": label, "ok": True if seen else None, "detail": "Seen" if seen else "Not seen"})
        for name in exp.get("forbid_events", []):
            seen = any(n == name for n, _ in names)
            checks.append({"label": f"No {short(name)} event", "ok": False if seen else True,
                           "detail": "Seen" if seen else "None seen"})
        tlm = exp.get("telemetry") or {}
        sample = rec["telemetry"]
        if tlm.get("value") is None:
            label = f"{short(rec['channel'])} telemetry received"
            ok = None if sample is None else True
        else:
            label = f"{short(rec['channel'])} telemetry is {tlm['value']}"
            ok = None if sample is None else str(sample["value"]) == str(tlm["value"])
        checks.append({"label": label, "ok": ok,
                       "detail": "Not received" if sample is None else f"{sample['value']}"})
        if rec.get("ambiguous_with"):
            checks.append({"label": "Evidence belongs to this attempt", "ok": False,
                           "detail": f"Can't tell: an earlier {short(rec['mnemonic'])} is still unanswered"})
        return checks

    def _describe(self, rec, waiting=True):
        resp = rec["response"]
        rejected = next((e for e in rec["events"] if e["name"] == REJECTED_EVENT), None)
        if resp is None:
            return "Command sent. Waiting for response." if waiting else "No command response."
        if resp["status"] == "OK":
            head = "Command accepted."
        elif rec["kind"] == "POWER_ON" and rejected is not None:
            head = f"Power on rejected in {rejected['args'][0]} mode."
        else:
            head = f"Command rejected ({resp['status']})."
        if rec["telemetry"] is None:
            return f"{head} Waiting for telemetry." if waiting else f"{head} No telemetry."
        return f"{head} Telemetry shows {rec['telemetry']['value']}."

    def _advance(self, rec, now):
        rec["checks"] = self._checks(rec)
        if rec["response"] is not None:
            rec["phase"] = "responded"
        rec["summary"] = self._describe(rec)
        complete = rec["response"] is not None and rec["telemetry"] is not None and all(
            c["ok"] is not None for c in rec["checks"])
        if complete:
            self._finish(rec, now)

    def _finish(self, rec, now, timed_out=False):
        rec["checks"] = self._checks(rec)
        failed = [c for c in rec["checks"] if c["ok"] is False]
        pending = [c for c in rec["checks"] if c["ok"] is None]
        if rec.get("ambiguous_with"):
            rec["outcome"] = "unconfirmed"
            rec["summary"] = (f"Unconfirmed. {self._describe(rec, waiting=False)} This can't be told apart from a "
                              f"late answer to the earlier unanswered {short(rec['mnemonic'])}: F´ responses carry "
                              "only the opcode. Decide whether to send again.")
        elif failed:
            rec["outcome"] = "mismatch"
            rec["summary"] = (f"{self._describe(rec, waiting=False)} "
                              f"Did not match the expected result: {failed[0]['label']}.")
        elif timed_out or pending:
            rec["outcome"] = "unconfirmed"
            window = f"within {EVIDENCE_TIMEOUT:g} s"
            if rec["response"] is None:
                gap = f"No command response {window}."
            else:
                head = self._describe(rec, waiting=False).split(".")[0] + "."
                if rec["telemetry"] is None:
                    gap = f"{head} No {short(rec['channel'])} telemetry {window}."
                else:
                    gap = f"{head} Missing {window}: {pending[0]['label'] if pending else 'evidence'}."
            rec["summary"] = f"Unconfirmed. {gap} Decide whether to send again."
        else:
            rec["outcome"] = "confirmed"
            rec["summary"] = self._describe(rec)
        rec["phase"] = "done"
        rec["completed_at"] = now
        if self.in_flight is rec:
            self.in_flight = None

    # -- rehearsals -------------------------------------------------------------

    def start_scenario(self, scenario_id):
        with self.lock:
            if scenario_id not in SCENARIOS_BY_ID:
                raise ConsoleError(f"Unknown scenario {scenario_id!r}")
            self.run = {"scenario": scenario_id, "step": 0, "attempts": {}, "state": "active"}
            self._notify()

    def stop_scenario(self):
        with self.lock:
            self.run = None
            self._notify()

    def _step_outcome(self, step):
        ids = self.run["attempts"].get(step, [])
        if not ids:
            return None
        rec = self.records.get(ids[-1])
        return rec["outcome"] if rec else None

    def send_step(self):
        """Send the active scenario's current step (also used to send it again)."""
        with self.lock:
            run = self.run
            if run is None or run["state"] != "active":
                raise ConsoleError("No rehearsal step is waiting to be sent")
            if self._step_outcome(run["step"]) == "confirmed":
                raise ConsoleError("This step is already confirmed. Continue to the next step.")
            step = SCENARIOS_BY_ID[run["scenario"]]["steps"][run["step"]]
            origin = {"scenario": run["scenario"], "step": run["step"]}
            rec = self.send(step["kind"], step["mode"], copy.deepcopy(step["expect"]), origin)
            run["attempts"].setdefault(run["step"], []).append(rec["id"])
            self._notify()
            return rec

    def advance_scenario(self):
        """Move to the next step. Allowed only when the current step is confirmed."""
        with self.lock:
            run = self.run
            if run is None or run["state"] != "active":
                raise ConsoleError("No rehearsal is active")
            if self._step_outcome(run["step"]) != "confirmed":
                raise ConsoleError("This step is not confirmed yet")
            steps = SCENARIOS_BY_ID[run["scenario"]]["steps"]
            if run["step"] + 1 >= len(steps):
                run["state"] = "complete"
            else:
                run["step"] += 1
            self._notify()

    # -- snapshot ---------------------------------------------------------------

    def snapshot(self):
        with self.lock:
            tlm = {}
            for name, sample in self.telemetry.items():
                if sample is None:
                    tlm[name] = None
                else:
                    fresh = self.link == "connected" and sample["epoch"] == self.link_epoch
                    tlm[name] = {k: sample[k] for k in ("value", "fsw_time", "ground_time")}
                    tlm[name]["fresh"] = fresh
            run = None
            if self.run is not None:
                run = copy.deepcopy(self.run)
                steps = SCENARIOS_BY_ID[run["scenario"]]["steps"]
                run["step_outcomes"] = [self._step_outcome(i) for i in range(len(steps))]
                run["attempts"] = {str(k): v for k, v in run["attempts"].items()}
            return {
                "version": self.version,
                "server_time": self._clock(),
                "link": self.link,
                "last_packet_at": self.last_packet_at,
                "last_tlm": self.last_tlm,
                "telemetry": {"mode": tlm[MODE_TLM], "power": tlm[SWITCH_TLM]},
                "in_flight": self.in_flight["id"] if self.in_flight else None,
                "run": run,
                "timeline": copy.deepcopy(list(reversed(self.timeline))),
                "limits": {"evidence_timeout": EVIDENCE_TIMEOUT, "link_timeout": LINK_TIMEOUT},
            }
