"""Unit tests for the operator console core (no GDS needed).

Run from the project folder:  fprime-venv/bin/python -m pytest console/test -v
"""

import pytest

from console.core import (CMD_DISPATCHED, CMD_DONE, CMD_ERROR, COMMANDS, EVIDENCE_TIMEOUT, LINK_TIMEOUT,
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
    console.on_event({"name": name, "args": list(args), "severity": "ACTIVITY_HI", "text": name, "fsw_time": "t"})


def ch(console, name, value):
    console.on_channel({"name": name, "value": value, "fsw_time": "t"})


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
    # Attempt 1's telemetry turns up late, so it has settled and a retry is distinguishable.
    ch(console, SWITCH_TLM, "OFF")
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
    assert recs[retry]["outcome"] == "unconfirmed"
    assert recs[retry]["ambiguous_with"] == [first]
    assert "can't be told apart" in recs[retry]["summary"]
    assert any(c["label"] == "Evidence belongs to this attempt" and c["ok"] is False for c in recs[retry]["checks"])
    fsw_power_off(console)  # the retry's own answer: shown with the retry, not as foreign activity
    snap = console.snapshot()
    assert not [r for r in snap["timeline"] if r["type"] == "activity"]
    assert {r["id"]: r for r in snap["timeline"]}[retry]["outcome"] == "unconfirmed"


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
    assert [e["name"] for e in rec["events"]].count(CMD_DONE) == 1  # the second answer, kept as evidence
