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

import collections
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
SAMPLE_BUFFER = 64  # recent PayloadSwitch/PayloadMode samples kept for matching to their command
SESSION_CHECK = 2.0  # seconds between checks for a verified fresh simulator + GDS session
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

    def __init__(self, send, opcodes, clock=time.time, session_probe=None, store=None):
        """
        Args:
            send: callable(mnemonic, args) that sends a command through the GDS pipeline
            opcodes: {mnemonic: opcode} for the COMMANDS mnemonics
            clock: wall-clock source (seconds), replaceable in tests
            session_probe: callable returning {"fsw": id, "gds": id} for the running flight-software and
                GDS comm processes, or None if they can't both be identified (see server.session_probe)
            store: object with load() -> list and save(list), persisting unresolved attempts across
                console restarts (see server.UnresolvedStore)
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
        # Sent commands whose evidence is not yet accounted for, oldest first. Independent of the
        # timeline, of time and of link reconnects: an attempt leaves only when its evidence is
        # attributed to it, or when a verified fresh simulator + GDS session starts.
        self.outstanding = []
        self._samples = {name: collections.deque(maxlen=SAMPLE_BUFFER) for name in self.telemetry}
        self._sample_seq = itertools.count(1)
        self._used_samples = set()
        self._session_probe = session_probe or (lambda: None)
        self._store = store
        self._next_session_check = 0.0
        self._restore()

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
                field = "dispatch" if name == CMD_DISPATCHED else "response"
                # F´ answers carry only the opcode, so each answer goes to the oldest attempt of that
                # opcode still missing one. A retry's first answer may be an earlier attempt's.
                target = next((r for r in self.outstanding if r["opcode"] == opcode and r["acct"][field] is None),
                              None)
                if target is None:
                    if opcode not in self._switch_opcodes:
                        return  # another component's command (e.g. SAY_HI from the GDS web UI)
                    self._activity(ev, now)
                else:
                    target["acct"][field] = ev
                    if target is rec:
                        if field == "dispatch":
                            rec["dispatched"] = ev
                        else:
                            status = "OK" if name == CMD_DONE else str(ev["args"][1])
                            rec["response"] = {"status": status, "event": ev}
                        self._advance(rec, now)
                    else:
                        self._note_late(target, ev)
                    self._account(now)
            elif name.startswith(SWITCH + "."):
                if rec is not None:
                    rec["events"].append(ev)
                    self._advance(rec, now)
                elif self.outstanding:
                    self._note_late(self.outstanding[0], ev)
                else:
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
            ch["seq"] = next(self._sample_seq)
            self.telemetry[name] = dict(ch, epoch=self.link_epoch)
            self._samples[name].append(ch)
            rec = self.in_flight
            if rec is not None and rec["channel"] == name and rec["telemetry"] is None:
                rec["telemetry"] = ch
                self._advance(rec, now)
            self._account(now)
            self._notify()

    def _account(self, now):
        """Match buffered telemetry to outstanding attempts; drop attempts whose evidence is complete.

        The switch writes its telemetry after the dispatcher's OpCodeDispatched and before the
        command's response, so a sample whose FSW time lies in [dispatch, response] of an attempt
        is that attempt's write. Oldest attempts are matched first. An attempt is accounted for
        once its dispatch, response and such a sample have all arrived.
        """
        for r in list(self.outstanding):
            acct = r["acct"]
            if acct["tlm"] is None and acct["dispatch"] is not None and acct["response"] is not None:
                lo, hi = acct["dispatch"].get("fsw_t"), acct["response"].get("fsw_t")
                if lo is not None and hi is not None:
                    for sample in self._samples[r["channel"]]:
                        t = sample.get("fsw_t")
                        if t is not None and lo <= t <= hi and sample["seq"] not in self._used_samples:
                            self._used_samples.add(sample["seq"])
                            acct["tlm"] = sample
                            own = r.get("telemetry") is not None and r["telemetry"].get("seq") == sample["seq"]
                            if r is not self.in_flight and not own:
                                self._note_late(r, sample)
                            break
            if all(acct[k] is not None for k in ("dispatch", "response", "tlm")):
                self.outstanding.remove(r)
                self._persist()

    @staticmethod
    def _fresh_session(sent_in, current):
        """True only if both the flight software and the GDS comm process are verified to be different
        processes from those the attempt was sent to. Anything unknown counts as the same session."""
        if not sent_in or not current:
            return False
        return all(sent_in.get(k) and current.get(k) and sent_in[k] != current[k] for k in ("fsw", "gds"))

    def _check_session(self, now):
        """Clear unresolved attempts whose simulator and GDS have both been replaced."""
        if not self.outstanding or now < self._next_session_check:
            return
        self._next_session_check = now + SESSION_CHECK
        current = self._session_probe()
        gone = [r for r in self.outstanding if r is not self.in_flight and self._fresh_session(r["session"], current)]
        if not gone:
            return
        self.outstanding = [r for r in self.outstanding if r not in gone]
        self.link_epoch += 1  # readings from the old session become "Last known"
        self._append({
            "id": next(self._ids), "type": "notice", "label": "Simulator and GDS restarted", "sent_at": now,
            "summary": (f"Verified fresh session: flight software {current['fsw']} and GDS {current['gds']} are new "
                        f"processes. Cleared {len(gone)} unresolved attempt(s) "
                        f"({', '.join(r['label'] for r in gone)}): their late evidence can no longer arrive. "
                        "Flight mode and payload power are Last known until they report again."),
        })
        self._persist()
        self._notify()

    # Fields kept when an unresolved attempt is saved for a console restart.
    _SAVED = ("id", "kind", "label", "mnemonic", "opcode", "channel", "sent_at", "session", "acct")

    def _persist(self):
        if self._store is None:
            return
        saved = []
        for r in self.outstanding:  # includes a command in flight: if this process dies, it is unresolved
            item = {k: r.get(k) for k in self._SAVED}
            item["acct"] = {k: (None if v is None else {f: v.get(f) for f in ("name", "args", "fsw_t", "fsw_time")})
                            for k, v in r["acct"].items()}
            saved.append(item)
        try:
            self._store.save(saved)
        except Exception:  # the guard still holds in memory; a failed save must not break commanding
            pass

    def _restore(self):
        """Reload unresolved attempts saved by an earlier console process."""
        if self._store is None:
            return
        try:
            saved = self._store.load() or []
        except Exception:
            saved = []
        if not saved:
            return
        current = self._session_probe()
        now = self._clock()
        kept, cleared = [], []
        for item in saved:
            (cleared if self._fresh_session(item.get("session"), current) else kept).append(item)
        top = max([0] + [int(i.get("id") or 0) for i in saved])
        self._ids = itertools.count(top + 1)
        for item in kept:
            rec = {**item, "type": "command", "label": f"{item['label']} (before console restart)",
                   "outcome": "unconfirmed", "summary": "Unresolved attempt from before the console restarted.",
                   "args": [], "origin": None, "expect": {}, "dispatched": None, "response": None, "events": [],
                   "telemetry": None, "phase": "done", "checks": [], "completed_at": None, "late": [],
                   "ambiguous_with": [], "ambiguous_labels": [], "restored": True}
            self.outstanding.append(rec)
            self._append(rec)  # shown in the timeline so any late evidence for it is visible
        if kept:
            self._append({
                "id": next(self._ids), "type": "notice", "label": "Unresolved attempts restored", "sent_at": now,
                "summary": (f"Restored {len(kept)} unresolved attempt(s) from before the console restarted "
                            f"({', '.join(i['label'] for i in kept)}). The simulator and GDS are not verified to be "
                            "a fresh session, so their late evidence could still arrive."),
            })
        if cleared:
            self._append({
                "id": next(self._ids), "type": "notice", "label": "Simulator and GDS restarted", "sent_at": now,
                "summary": (f"Cleared {len(cleared)} saved unresolved attempt(s): the simulator and GDS are verified "
                            "new processes, so their late evidence can no longer arrive."),
            })
        self._persist()

    def _note_late(self, rec, item):
        """Show evidence that arrived after a command's window with that command. Never changes its outcome."""
        rec["late"].append(item)
        note = " Late evidence arrived; still Unconfirmed."
        if rec["outcome"] == "unconfirmed" and not rec["summary"].endswith(note):
            rec["summary"] += note

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
            self._check_session(now)

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
            # F´ responses carry only the opcode and telemetry carries no command reference. While an
            # earlier attempt with this opcode or this telemetry channel is unresolved, its late evidence
            # could arrive during this attempt and can't be told apart, so this attempt can never be
            # Confirmed. No time limit: the guard lasts until that evidence is attributed or a verified
            # fresh simulator + GDS session starts.
            unsettled = [r for r in self.outstanding if r["opcode"] == opcode or r["channel"] == cmd["channel"]]
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
                "ambiguous_labels": [r["label"] for r in unsettled],
                "acct": {"dispatch": None, "response": None, "tlm": None},  # evidence accounting (see _account)
                "session": self._session_probe(),  # flight-software + GDS processes it was sent to
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
            self.outstanding.append(rec)
            self._persist()
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
                           "detail": f"Can't tell: earlier {', '.join(rec['ambiguous_labels'])} still unresolved"})
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
            rec["summary"] = (f"Unconfirmed. {self._describe(rec, waiting=False)} This can't be told apart from late "
                              f"evidence of the earlier unresolved {', '.join(rec['ambiguous_labels'])}: F´ responses "
                              "carry only the opcode. That clears when its evidence arrives or the simulator and GDS "
                              "restart as verified new processes. Decide whether to send again.")
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
        if rec in self.outstanding:
            self._persist()

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
                "unresolved": [{"id": r["id"], "label": r["label"], "sent_at": r["sent_at"],
                                "channel": short(r["channel"])}
                               for r in self.outstanding if r is not self.in_flight],
                "run": run,
                "timeline": [{k: v for k, v in r.items() if k not in ("acct", "session")}
                             for r in copy.deepcopy(list(reversed(self.timeline)))],
                "limits": {"evidence_timeout": EVIDENCE_TIMEOUT, "link_timeout": LINK_TIMEOUT},
            }
