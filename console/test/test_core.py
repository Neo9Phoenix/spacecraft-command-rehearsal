"""Unit tests for the operator console core (no GDS needed).

Run from the project folder:  fprime-venv/bin/python -m pytest console/test -v
"""

import pytest

from console.core import (CMD_DISPATCHED, CMD_DONE, CMD_ERROR, COMMANDS, EVIDENCE_TIMEOUT, LINK_TIMEOUT, TIMELINE_LIMIT,
                          MODE_EVENT, MODE_TLM, REJECTED_EVENT, STATE_EVENT, SWITCH_TLM, Busy, Console,
                          ConsoleError, Disconnected)

OPCODES = {c["mnemonic"]: 0x10006000 + i for i, c in enumerate(COMMANDS.values())}
OP = {k: OPCODES[c["mnemonic"]] for k, c in COMMANDS.items()}
SAY_HI_OPCODE = 0x10005000


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env():
    clock = Clock()
    sent = []
    console = Console(lambda m, a: sent.append((m, list(a))), OPCODES, clock=clock)
    console.on_channel({"name": "CommandRehearsal.systemResources.CPU", "value": 3.0, "fsw_time": "t"})
    return console, clock, sent


def ev(console, name, *args):
    console.on_event({"name": name, "args": list(args), "severity": "ACTIVITY_HI", "text": name, "fsw_time": "t",
                      "fsw_t": console._clock()})


def ch(console, name, value, fsw_t=None):
    console.on_channel({"name": name, "value": value, "fsw_time": "t",
                        "fsw_t": console._clock() if fsw_t is None else fsw_t})


def last(console):
    return console.snapshot()["timeline"][0]


# Simulated flight-software responses, matching PayloadPowerSwitch.cpp
def fsw_power_on(console, mode, switch):
    ev(console, CMD_DISPATCHED, OP["POWER_ON"], 7)
    if mode == "SAFE":
        ev(console, REJECTED_EVENT, "SAFE")
        ev(console, CMD_ERROR, OP["POWER_ON"], "EXECUTION_ERROR")
        ch(console, SWITCH_TLM, switch)
    else:
        ev(console, STATE_EVENT, "ON")
        ev(console, CMD_DONE, OP["POWER_ON"])
        ch(console, SWITCH_TLM, "ON")


def fsw_power_off(console):
    ev(console, CMD_DISPATCHED, OP["POWER_OFF"], 7)
    ev(console, STATE_EVENT, "OFF")
    ev(console, CMD_DONE, OP["POWER_OFF"])
    ch(console, SWITCH_TLM, "OFF")


def fsw_set_mode(console, mode):
    ev(console, CMD_DISPATCHED, OP["SET_MODE"], 7)
    ev(console, MODE_EVENT, mode)
    ev(console, CMD_DONE, OP["SET_MODE"])
    ch(console, MODE_TLM, mode)


def test_unknown_before_any_switch_telemetry(env):
    console, _, _ = env
    snap = console.snapshot()
    assert snap["link"] == "connected"
    assert snap["telemetry"] == {"mode": None, "power": None}


def test_power_on_in_normal_is_confirmed_by_response_and_telemetry(env):
    console, _, sent = env
    console.send("POWER_ON")
    assert sent == [("CommandRehearsal.payloadSwitch.POWER_ON", [])]
    ev(console, CMD_DISPATCHED, OP["POWER_ON"], 7)
    ev(console, STATE_EVENT, "ON")
    ev(console, CMD_DONE, OP["POWER_ON"])
    rec = last(console)
    assert rec["outcome"] is None
    assert rec["summary"] == "Command accepted. Waiting for telemetry."
    assert console.snapshot()["in_flight"] == rec["id"]
    ch(console, SWITCH_TLM, "ON")
    rec = last(console)
    assert rec["outcome"] == "confirmed"
    assert rec["summary"] == "Command accepted. Telemetry shows ON."
    assert console.snapshot()["in_flight"] is None
    assert console.snapshot()["telemetry"]["power"]["value"] == "ON"


def test_power_on_rejected_in_safe_reports_rejection(env):
    console, _, sent = env
    console.send("POWER_ON")
    fsw_power_on(console, "SAFE", "OFF")
    rec = last(console)
    assert sent[-1][0].endswith("POWER_ON"), "the console must send POWER_ON in SAFE; F´ owns the interlock"
    assert rec["response"]["status"] == "EXECUTION_ERROR"
    assert [e["name"] for e in rec["events"]] == [REJECTED_EVENT]
    assert rec["summary"] == "Power on rejected in SAFE mode. Telemetry shows OFF."


def test_one_command_in_flight_at_a_time(env):
    console, _, sent = env
    console.send("POWER_OFF")
    with pytest.raises(Busy):
        console.send("POWER_ON")
    assert len(sent) == 1
    fsw_power_off(console)
    console.send("POWER_ON")
    assert len(sent) == 2


def test_commands_disabled_without_downlink():
    clock = Clock()
    console = Console(lambda *a: None, OPCODES, clock=clock)
    with pytest.raises(Disconnected):
        console.send("POWER_ON")
    ch(console, "CommandRehearsal.systemResources.CPU", 1.0)
    clock.t += LINK_TIMEOUT + 0.1
    console.tick()
    assert console.snapshot()["link"] == "lost"
    with pytest.raises(Disconnected):
        console.send("POWER_ON")


def test_last_known_state_is_marked_after_link_loss(env):
    console, clock, _ = env
    console.send("POWER_OFF")
    fsw_power_off(console)
    assert console.snapshot()["telemetry"]["power"]["fresh"] is True
    clock.t += LINK_TIMEOUT + 0.1
    console.tick()
    snap = console.snapshot()
    assert snap["link"] == "lost"
    assert snap["telemetry"]["power"] == {**snap["telemetry"]["power"], "value": "OFF", "fresh": False}
    # Link returns: the old value stays "last known" until the switch reports again.
    ch(console, "CommandRehearsal.systemResources.CPU", 1.0)
    assert console.snapshot()["link"] == "connected"
    assert console.snapshot()["telemetry"]["power"]["fresh"] is False
    console.send("POWER_OFF")
    fsw_power_off(console)
    assert console.snapshot()["telemetry"]["power"]["fresh"] is True


def test_telemetry_before_the_command_is_not_evidence(env):
    console, _, _ = env
    ch(console, SWITCH_TLM, "OFF")
    console.send("POWER_ON")
    ev(console, CMD_DONE, OP["POWER_ON"])
    assert last(console)["telemetry"] is None
    assert last(console)["outcome"] is None


def test_missing_telemetry_times_out_as_unconfirmed(env):
    console, clock, _ = env
    console.send("POWER_ON")
    ev(console, CMD_DONE, OP["POWER_ON"])
    clock.t += EVIDENCE_TIMEOUT + 0.1
    ch(console, "CommandRehearsal.systemResources.CPU", 1.0)  # link still alive
    console.tick()
    rec = last(console)
    assert rec["outcome"] == "unconfirmed"
    assert "Decide whether to send again" in rec["summary"]
    assert console.snapshot()["in_flight"] is None


def test_other_components_commands_are_ignored(env):
    console, _, _ = env
    console.send("POWER_ON")
    ev(console, CMD_DONE, SAY_HI_OPCODE)  # e.g. SAY_HI sent from the GDS web UI
    assert last(console)["response"] is None


def test_switch_activity_from_another_client_is_shown(env):
    console, _, _ = env
    ev(console, CMD_DISPATCHED, OP["POWER_ON"], 7)
    ev(console, STATE_EVENT, "ON")
    entry = last(console)
    assert entry["type"] == "activity"
    assert len(entry["events"]) == 2


def test_send_failure_releases_the_slot(env):
    console, _, _ = env

    def boom(*_):
        raise RuntimeError("pipeline closed")

    console._send = boom
    with pytest.raises(ConsoleError):
        console.send("POWER_ON")
    assert last(console)["outcome"] == "send_failed"
    assert console.snapshot()["in_flight"] is None


def test_set_mode_needs_a_valid_mode(env):
    console, _, sent = env
    with pytest.raises(ConsoleError):
        console.send("SET_MODE", "BOGUS")
    assert sent == []


# ---------------------------------------------------------------- rehearsals


def run_steps(console, script):
    for respond in script:
        console.send_step()
        respond()
        assert last(console)["outcome"] == "confirmed", last(console)["summary"]
        console.advance_scenario()


def test_normal_startup_scenario(env):
    console, _, sent = env
    console.start_scenario("normal-startup")
    run_steps(console, [
        lambda: fsw_power_off(console),
        lambda: fsw_set_mode(console, "NORMAL"),
        lambda: fsw_power_on(console, "NORMAL", "OFF"),
    ])
    assert console.snapshot()["run"]["state"] == "complete"
    assert [m.rsplit(".", 1)[-1] for m, _ in sent] == ["POWER_OFF", "SET_MODE", "POWER_ON"]


def test_safe_rejection_scenario(env):
    console, _, _ = env
    console.start_scenario("safe-rejection")
    run_steps(console, [
        lambda: fsw_power_off(console),
        lambda: fsw_set_mode(console, "SAFE"),
        lambda: fsw_power_on(console, "SAFE", "OFF"),
    ])
    assert console.snapshot()["run"]["state"] == "complete"


def test_power_off_in_safe_scenario(env):
    console, _, sent = env
    console.start_scenario("safe-power-off")
    run_steps(console, [
        lambda: fsw_set_mode(console, "NORMAL"),
        lambda: fsw_power_on(console, "NORMAL", "OFF"),
        lambda: fsw_set_mode(console, "SAFE"),
        lambda: fsw_power_off(console),
    ])
    assert console.snapshot()["run"]["state"] == "complete"
    assert sent[2] == ("CommandRehearsal.payloadSwitch.SET_MODE", ["SAFE"])


def test_step_cannot_advance_until_confirmed(env):
    console, clock, _ = env
    console.start_scenario("normal-startup")
    with pytest.raises(ConsoleError):
        console.advance_scenario()  # nothing sent yet
    console.send_step()
    written = clock.t  # the switch writes its telemetry while handling the command
    ev(console, CMD_DISPATCHED, OP["POWER_OFF"], 7)
    ev(console, CMD_DONE, OP["POWER_OFF"])
    with pytest.raises(ConsoleError):
        console.advance_scenario()  # response only, no telemetry yet
    clock.t += EVIDENCE_TIMEOUT + 0.1
    ch(console, "CommandRehearsal.systemResources.CPU", 1.0)
    console.tick()
    snap = console.snapshot()
    assert snap["run"]["step_outcomes"][0] == "unconfirmed"
    with pytest.raises(ConsoleError):
        console.advance_scenario()
    # Attempt 1's telemetry turns up late, so its evidence is accounted for and a retry is distinguishable.
    ch(console, SWITCH_TLM, "OFF", fsw_t=written)
    assert console.snapshot()["unresolved"] == []
    assert console.snapshot()["run"]["step_outcomes"][0] == "unconfirmed"  # late evidence never confirms
    # The operator chooses to send again; this attempt gets its evidence.
    console.send_step()
    fsw_power_off(console)
    assert console.snapshot()["run"]["step_outcomes"][0] == "confirmed"
    assert len(console.snapshot()["run"]["attempts"]["0"]) == 2
    console.advance_scenario()
    assert console.snapshot()["run"]["step"] == 1


def test_unexpected_acceptance_does_not_match(env):
    """If F´ ever accepted POWER_ON in the SAFE scenario, the step must not be confirmed."""
    console, clock, _ = env
    console.start_scenario("safe-rejection")
    run_steps(console, [lambda: fsw_power_off(console), lambda: fsw_set_mode(console, "SAFE")])
    console.send_step()
    fsw_power_on(console, "NORMAL", "OFF")  # wrong behavior: accepted, ON
    clock.t += EVIDENCE_TIMEOUT + 0.1
    ch(console, "CommandRehearsal.systemResources.CPU", 1.0)
    console.tick()
    rec = last(console)
    assert rec["outcome"] == "mismatch"
    with pytest.raises(ConsoleError):
        console.advance_scenario()


def test_entering_safe_must_not_move_the_switch(env):
    console, _, _ = env
    console.start_scenario("safe-power-off")
    run_steps(console, [lambda: fsw_set_mode(console, "NORMAL"), lambda: fsw_power_on(console, "NORMAL", "OFF")])
    console.send_step()  # SET_MODE SAFE
    ev(console, STATE_EVENT, "OFF")  # wrong behavior: the switch moved
    fsw_set_mode(console, "SAFE")
    assert last(console)["outcome"] == "mismatch"


def test_late_evidence_stays_with_the_unconfirmed_command(env):
    console, clock, _ = env
    console.send("POWER_OFF")
    clock.t += EVIDENCE_TIMEOUT + 0.1
    ch(console, "CommandRehearsal.systemResources.CPU", 1.0)
    console.tick()
    assert last(console)["outcome"] == "unconfirmed"
    fsw_power_off(console)  # the flight software answers late
    rec = last(console)
    assert rec["type"] == "command", "late evidence must not be shown as another client's activity"
    assert rec["outcome"] == "unconfirmed", "late evidence never confirms a command"
    assert [x["name"] for x in rec["late"]] == [CMD_DISPATCHED, STATE_EVENT, CMD_DONE, SWITCH_TLM]
    assert "Late evidence" in rec["summary"]
    assert console.snapshot()["telemetry"]["power"]["value"] == "OFF"  # the strip still updates


# ---------------------------------------------------------------- retries of the same opcode


def time_out(console, clock):
    clock.t += EVIDENCE_TIMEOUT + 0.1
    ch(console, "CommandRehearsal.systemResources.CPU", 1.0)  # link still alive
    console.tick()


def test_late_evidence_never_confirms_a_retry(env):
    """Attempt 1 times out unanswered; its late answer lands during the retry. F´ responses carry
    only the opcode, so the retry can't be told apart from it and must stay Unconfirmed."""
    console, clock, _ = env
    first = console.send("POWER_OFF")["id"]
    time_out(console, clock)
    retry = console.send("POWER_OFF")["id"]
    fsw_power_off(console)  # attempt 1's late answer, indistinguishable from the retry's
    recs = {r["id"]: r for r in console.snapshot()["timeline"]}
    assert recs[retry]["outcome"] != "confirmed"
    assert recs[retry]["response"] is None  # the answer went to attempt 1, the oldest unanswered
    assert [x["name"] for x in recs[first]["late"]].count(CMD_DONE) == 1
    fsw_power_off(console)  # the retry's own answer: shown with the retry, not as foreign activity
    snap = console.snapshot()
    rec = {r["id"]: r for r in snap["timeline"]}[retry]
    assert rec["outcome"] == "unconfirmed"
    assert rec["ambiguous_with"] == [first]
    assert "can't be told apart" in rec["summary"]
    assert any(c["label"] == "Evidence belongs to this attempt" and c["ok"] is False for c in rec["checks"])
    assert not [r for r in snap["timeline"] if r["type"] == "activity"]


def test_retry_confirms_once_the_first_attempt_has_settled(env):
    """If attempt 1's late evidence arrives before the retry is sent, the attempts are distinguishable."""
    console, clock, _ = env
    console.send("POWER_OFF")
    time_out(console, clock)
    fsw_power_off(console)  # late, but before the retry: attached to attempt 1
    console.send("POWER_OFF")
    fsw_power_off(console)
    rec = last(console)
    assert rec["ambiguous_with"] == []
    assert rec["outcome"] == "confirmed"


def test_rehearsal_step_retry_cannot_be_confirmed_by_late_evidence(env):
    console, clock, _ = env
    console.start_scenario("normal-startup")
    console.send_step()
    time_out(console, clock)
    console.send_step()  # operator chooses Send again
    fsw_power_off(console)  # attempt 1's late answer
    assert console.snapshot()["run"]["step_outcomes"][0] != "confirmed"
    with pytest.raises(ConsoleError):
        console.advance_scenario()
    fsw_power_off(console)  # the retry's own answer
    assert console.snapshot()["run"]["step_outcomes"][0] == "unconfirmed"
    with pytest.raises(ConsoleError):
        console.advance_scenario()


def test_interleaved_answers_stay_with_the_retry(env):
    """Seen live: both attempts' answers arrive in one burst before the telemetry. The retry stays
    Unconfirmed, and its own answer is not mislabeled as another client's activity."""
    console, clock, _ = env
    console.send("POWER_OFF")
    time_out(console, clock)
    retry = console.send("POWER_OFF")["id"]
    for _ in range(2):  # attempt 1's answer, then the retry's
        ev(console, CMD_DISPATCHED, OP["POWER_OFF"], 7)
        ev(console, STATE_EVENT, "OFF")
        ev(console, CMD_DONE, OP["POWER_OFF"])
    ch(console, SWITCH_TLM, "OFF")
    snap = console.snapshot()
    rec = {r["id"]: r for r in snap["timeline"]}[retry]
    assert rec["outcome"] == "unconfirmed"
    assert not [r for r in snap["timeline"] if r["type"] == "activity"]
    assert rec["response"] is not None  # the second answer is the retry's
    first = rec["ambiguous_with"][0]
    assert [x["name"] for x in {r["id"]: r for r in snap["timeline"]}[first]["late"]].count(CMD_DONE) == 1



# ---------------------------------------------------------------- v0.2.1: unresolved attempts


DISPATCHED_TLM = "CdhCore.cmdDisp.CommandsDispatched"


def test_retry_after_60_s_is_not_confirmed_by_first_attempts_late_evidence(env):
    """POWER_ON times out unanswered; the operator retries after 60 s; only attempt 1's late
    response and telemetry arrive. The retry must never show Confirmed."""
    console, clock, _ = env
    console.send("POWER_ON")
    time_out(console, clock)
    clock.t += 61
    ch(console, "CommandRehearsal.systemResources.CPU", 1.0)
    retry = console.send("POWER_ON")["id"]
    fsw_power_on(console, "NORMAL", "OFF")  # attempt 1's late answer only
    time_out(console, clock)
    rec = {r["id"]: r for r in console.snapshot()["timeline"]}[retry]
    assert rec["outcome"] == "unconfirmed"
    assert rec["ambiguous_with"]


def test_unresolved_attempt_survives_timeline_eviction(env):
    console, clock, _ = env
    first = console.send("POWER_ON")["id"]
    time_out(console, clock)
    for _ in range(TIMELINE_LIMIT + 5):  # push attempt 1 out of the timeline
        console.send("SET_MODE", "NORMAL")
        fsw_set_mode(console, "NORMAL")
    assert first not in {r["id"] for r in console.snapshot()["timeline"]}
    console.send("POWER_ON")
    fsw_power_on(console, "NORMAL", "OFF")  # could be attempt 1's late answer
    time_out(console, clock)
    assert last(console)["outcome"] == "unconfirmed"
    assert last(console)["ambiguous_with"] == [first]


def test_same_channel_command_is_also_protected(env):
    """POWER_OFF reports on the same PayloadSwitch channel as an unresolved POWER_ON."""
    console, clock, _ = env
    console.send("POWER_ON")
    time_out(console, clock)
    console.send("POWER_OFF")
    fsw_power_off(console)
    assert last(console)["outcome"] == "unconfirmed"


def test_guard_clears_when_late_evidence_is_accounted_for(env):
    console, clock, _ = env
    console.send("POWER_ON")
    time_out(console, clock)
    fsw_power_on(console, "NORMAL", "OFF")  # dispatch, response and telemetry, nothing in flight
    assert console.snapshot()["unresolved"] == []
    console.send("POWER_ON")
    fsw_power_on(console, "NORMAL", "OFF")
    assert last(console)["outcome"] == "confirmed"


def test_response_without_telemetry_does_not_clear_the_guard(env):
    console, clock, _ = env
    console.send("POWER_ON")
    time_out(console, clock)
    ev(console, CMD_DISPATCHED, OP["POWER_ON"], 7)
    ev(console, CMD_DONE, OP["POWER_ON"])  # late response, but its telemetry never arrives
    assert len(console.snapshot()["unresolved"]) == 1
    console.send("POWER_ON")
    fsw_power_on(console, "NORMAL", "OFF")
    assert last(console)["outcome"] == "unconfirmed"


def test_dispatch_counter_drop_alone_does_not_clear_the_guard(env):
    console, clock, _ = env
    ch(console, DISPATCHED_TLM, 5)
    console.send("POWER_ON")
    time_out(console, clock)
    ch(console, DISPATCHED_TLM, 0)  # looks like a restart, but nothing verified the processes changed
    assert len(console.snapshot()["unresolved"]) == 1


def test_link_reconnect_does_not_clear_the_guard(env):
    console, clock, _ = env
    console.send("POWER_ON")
    time_out(console, clock)
    clock.t += LINK_TIMEOUT + 1
    console.tick()
    assert console.snapshot()["link"] == "lost"
    ch(console, "CommandRehearsal.systemResources.CPU", 1.0)  # link back
    assert console.snapshot()["link"] == "connected"
    assert len(console.snapshot()["unresolved"]) == 1


class MemoryStore:
    def __init__(self):
        self.items = []

    def load(self):
        return list(self.items)

    def save(self, items):
        self.items = [dict(i) for i in items]


SESSION = {"fsw": "100@Tue Nov 10 00:00:00 2026", "gds": "90@Tue Nov 10 00:00:00 2026"}


def make_console(clock, store, session):
    console = Console(lambda m, a: None, OPCODES, clock=clock, session_probe=lambda: session["now"], store=store)
    ch(console, "CommandRehearsal.systemResources.CPU", 1.0)
    return console


def unresolved_power_on(clock, store, session):
    console = make_console(clock, store, session)
    console.send("POWER_ON")
    time_out(console, clock)
    assert len(store.items) == 1
    return console


def test_restarting_only_the_console_keeps_the_guard():
    """Same running simulator and GDS: a new console process restores the guard, and a retry stays Unconfirmed."""
    clock, store, session = Clock(), MemoryStore(), {"now": dict(SESSION)}
    unresolved_power_on(clock, store, session)
    console = make_console(clock, store, session)  # console restarted, attached to the same GDS
    snap = console.snapshot()
    assert [u["label"] for u in snap["unresolved"]] == ["Power on (before console restart)"]
    assert any(r["type"] == "notice" and r["label"] == "Unresolved attempts restored" for r in snap["timeline"])
    retry = console.send("POWER_ON")
    assert retry["ambiguous_with"]
    fsw_power_on(console, "NORMAL", "OFF")  # attempt 1's late answer
    fsw_power_on(console, "NORMAL", "OFF")
    time_out(console, clock)
    assert {r["id"]: r for r in console.snapshot()["timeline"]}[retry["id"]]["outcome"] == "unconfirmed"


def test_console_restart_with_verified_fresh_session_clears_the_guard():
    clock, store, session = Clock(), MemoryStore(), {"now": dict(SESSION)}
    unresolved_power_on(clock, store, session)
    session["now"] = {"fsw": "200@Tue Nov 10 01:00:00 2026", "gds": "190@Tue Nov 10 01:00:00 2026"}
    console = make_console(clock, store, session)
    assert console.snapshot()["unresolved"] == []
    assert store.items == []
    console.send("POWER_ON")
    fsw_power_on(console, "NORMAL", "OFF")
    assert last(console)["outcome"] == "confirmed"


def test_fresh_session_detected_while_running_clears_the_guard():
    clock, store, session = Clock(), MemoryStore(), {"now": dict(SESSION)}
    console = unresolved_power_on(clock, store, session)
    session["now"] = {"fsw": "200@Tue Nov 10 01:00:00 2026", "gds": "190@Tue Nov 10 01:00:00 2026"}
    clock.t += 3
    ch(console, "CommandRehearsal.systemResources.CPU", 1.0)
    console.tick()
    snap = console.snapshot()
    assert snap["unresolved"] == []
    assert any(r["type"] == "notice" and r["label"] == "Simulator and GDS restarted" for r in snap["timeline"])


@pytest.mark.parametrize("now", [
    {"fsw": "200@Tue Nov 10 01:00:00 2026", "gds": SESSION["gds"]},  # only the flight software changed
    {"fsw": SESSION["fsw"], "gds": "190@Tue Nov 10 01:00:00 2026"},  # only the GDS changed
    None,                                                            # processes can't be identified
])
def test_partial_or_unverified_session_change_keeps_the_guard(now):
    clock, store, session = Clock(), MemoryStore(), {"now": dict(SESSION)}
    console = unresolved_power_on(clock, store, session)
    session["now"] = now
    clock.t += 3
    ch(console, "CommandRehearsal.systemResources.CPU", 1.0)
    console.tick()
    assert len(console.snapshot()["unresolved"]) == 1
    assert len(make_console(clock, store, session).snapshot()["unresolved"]) == 1  # and across a console restart


def test_late_evidence_after_console_restart_is_attributed_and_clears_the_guard():
    clock, store, session = Clock(), MemoryStore(), {"now": dict(SESSION)}
    unresolved_power_on(clock, store, session)
    console = make_console(clock, store, session)
    fsw_power_on(console, "NORMAL", "OFF")  # attempt 1's late evidence reaches the new console
    snap = console.snapshot()
    assert snap["unresolved"] == []
    assert store.items == []
    restored = next(r for r in snap["timeline"] if r.get("restored"))
    assert restored["outcome"] == "unconfirmed"  # late evidence is shown, never confirms
    assert [x["name"] for x in restored["late"]] == [CMD_DISPATCHED, STATE_EVENT, CMD_DONE, SWITCH_TLM]


def test_normal_commands_leave_nothing_unresolved(env):
    console, _, _ = env
    console.start_scenario("safe-power-off")
    run_steps(console, [
        lambda: fsw_set_mode(console, "NORMAL"),
        lambda: fsw_power_on(console, "NORMAL", "OFF"),
        lambda: fsw_set_mode(console, "SAFE"),
        lambda: fsw_power_off(console),
    ])
    snap = console.snapshot()
    assert snap["unresolved"] == []
    # A confirmed command's own telemetry is not relabeled as late evidence (seen live in v0.2.0 testing).
    assert all(r["late"] == [] for r in snap["timeline"] if r["type"] == "command")


def test_unresolved_state_is_saved_before_dispatch():
    """At the moment the command leaves, the saved state already holds it as unresolved, so a crash
    between dispatch and any later save can't lose the guard."""
    clock, store, session = Clock(), MemoryStore(), {"now": dict(SESSION)}
    seen_at_dispatch = []
    console = Console(lambda m, a: seen_at_dispatch.append([i["kind"] for i in store.items]), OPCODES,
                      clock=clock, session_probe=lambda: session["now"], store=store)
    ch(console, "CommandRehearsal.systemResources.CPU", 1.0)
    console.send("POWER_ON")
    assert seen_at_dispatch == [["POWER_ON"]]
    # Simulated crash right after dispatch: a new console restores the guard from the saved state.
    restarted = make_console(clock, store, session)
    assert [u["label"] for u in restarted.snapshot()["unresolved"]] == ["Power on (before console restart)"]


def test_command_is_not_sent_if_the_state_cannot_be_saved():
    class FailingStore(MemoryStore):
        def save(self, items):
            raise OSError("disk full")

    sent = []
    clock = Clock()
    console = Console(lambda m, a: sent.append(m), OPCODES, clock=clock, session_probe=lambda: dict(SESSION),
                      store=FailingStore())
    ch(console, "CommandRehearsal.systemResources.CPU", 1.0)
    with pytest.raises(ConsoleError):
        console.send("POWER_ON")
    assert sent == []
    assert last(console)["outcome"] == "send_failed"
    assert console.snapshot()["in_flight"] is None


def test_refused_send_is_removed_from_the_saved_state():
    store = MemoryStore()
    clock = Clock()

    def refuse(*_):
        raise RuntimeError("pipeline closed")

    console = Console(refuse, OPCODES, clock=clock, session_probe=lambda: dict(SESSION), store=store)
    ch(console, "CommandRehearsal.systemResources.CPU", 1.0)
    with pytest.raises(ConsoleError):
        console.send("POWER_ON")
    assert store.items == []
    assert console.snapshot()["unresolved"] == []
