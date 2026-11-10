"""Integration tests: the payload power switch through the F´ ground system.

These run against a live RehearsalDeployment: commands go out through the
F´ GDS to the flight software, and results come back as events and
telemetry. Simulation only; the switch drives no hardware.

Run with scripts/integration-test.sh (it starts the deployment and GDS,
runs pytest, then stops them). See README.md.
"""

SWITCH = "CommandRehearsal.payloadSwitch"
POWER_ON = f"{SWITCH}.POWER_ON"  # the same command is sent in both modes
POWER_OFF = f"{SWITCH}.POWER_OFF"
SET_MODE = f"{SWITCH}.SET_MODE"
SWITCH_TLM = f"{SWITCH}.PayloadSwitch"
MODE_TLM = f"{SWITCH}.PayloadMode"
STATE_EVENT = f"{SWITCH}.SwitchStateChanged"
REJECTED_EVENT = f"{SWITCH}.PowerOnRejected"
CMD_ERROR_EVENT = "CdhCore.cmdDisp.OpCodeError"
CMD_DONE_EVENT = "CdhCore.cmdDisp.OpCodeCompleted"

TIMEOUT = 10  # seconds; telemetry is downlinked once per second


def mark(api):
    """History positions, so later checks only look at what arrives after this point."""
    return api.get_event_test_history().size(), api.get_telemetry_test_history().size()


def start_with_switch_off(api, mode):
    """Put the switch OFF, confirm it via telemetry, then select the mode under test."""
    _, tlm_start = mark(api)
    api.send_and_assert_command(POWER_OFF, timeout=TIMEOUT)
    api.assert_telemetry(SWITCH_TLM, "OFF", start=tlm_start, timeout=TIMEOUT)

    _, tlm_start = mark(api)
    api.send_and_assert_command(SET_MODE, [mode], timeout=TIMEOUT)
    api.assert_telemetry(MODE_TLM, mode, start=tlm_start, timeout=TIMEOUT)


def test_power_on_in_normal_mode_turns_switch_on(fprime_test_api):
    api = fprime_test_api
    start_with_switch_off(api, "NORMAL")
    ev_start, tlm_start = mark(api)

    # Command accepted: dispatched, the switch reports ON, then completed OK.
    api.send_and_assert_command(
        POWER_ON,
        timeout=TIMEOUT,
        events=[api.get_event_pred(STATE_EVENT, ["ON"])],
    )
    api.assert_telemetry(SWITCH_TLM, "ON", start=tlm_start, timeout=TIMEOUT)
    api.assert_event_count(0, [REJECTED_EVENT], start=ev_start)

    opcode = api.translate_command_name(POWER_ON)
    api.assert_event_count(0, [api.get_event_pred(CMD_ERROR_EVENT, [opcode, None])], start=ev_start)


def test_power_on_in_safe_mode_is_rejected_and_switch_stays_off(fprime_test_api):
    api = fprime_test_api
    start_with_switch_off(api, "SAFE")
    ev_start, tlm_start = mark(api)
    opcode = api.translate_command_name(POWER_ON)

    api.send_command(POWER_ON)

    # The command is rejected by the flight software ...
    api.assert_event(CMD_ERROR_EVENT, [opcode, "EXECUTION_ERROR"], start=ev_start, timeout=TIMEOUT)
    # ... with a rejection event naming SAFE mode ...
    api.assert_event(REJECTED_EVENT, ["SAFE"], start=ev_start, timeout=TIMEOUT)
    # ... and the switch reports, after the rejection, that it is still OFF.
    api.assert_telemetry(SWITCH_TLM, "OFF", start=tlm_start, timeout=TIMEOUT)

    # It never completed OK, never reported ON, and never announced a switch change.
    api.assert_event_count(0, [api.get_event_pred(CMD_DONE_EVENT, [opcode])], start=ev_start)
    api.assert_telemetry_count(0, [api.get_telemetry_pred(SWITCH_TLM, "ON")], start=tlm_start)
    api.assert_event_count(0, [STATE_EVENT], start=ev_start)


def test_tutorial_say_hi_still_works(fprime_test_api):
    """The Hello World tutorial's command, run through the same ground system."""
    api = fprime_test_api
    ev_start, _ = mark(api)
    api.send_and_assert_command(
        "CommandRehearsal.hiCmpntInstance.SAY_HI",
        ["Hello, F Prime"],
        timeout=TIMEOUT,
        events=[api.get_event_pred("CommandRehearsal.hiCmpntInstance.SayHiEvent", ["Hello, F Prime"])],
    )
