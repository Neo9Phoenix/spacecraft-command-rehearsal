// ======================================================================
// \title  PayloadPowerSwitchTester.cpp
// \author neophoenix
// \brief  cpp file for PayloadPowerSwitch component test harness implementation class
// ======================================================================

#include "PayloadPowerSwitchTester.hpp"

namespace CommandRehearsal {

// ----------------------------------------------------------------------
// Construction and destruction
// ----------------------------------------------------------------------

PayloadPowerSwitchTester ::PayloadPowerSwitchTester()
    : PayloadPowerSwitchGTestBase("PayloadPowerSwitchTester", PayloadPowerSwitchTester::MAX_HISTORY_SIZE),
      component("PayloadPowerSwitch") {
    this->initComponents();
    this->connectPorts();
}

PayloadPowerSwitchTester ::~PayloadPowerSwitchTester() {
    this->component.deinit();
}

// ----------------------------------------------------------------------
// Tests
// ----------------------------------------------------------------------

// Each test uses a new tester, so the component starts in its initial state:
// switch OFF, NORMAL mode, nothing reported yet.
//
// The run*() helpers clear the event, telemetry and response histories before
// sending, so checks after a helper only see what that one command produced.

void PayloadPowerSwitchTester ::testPowerOnInNormalTurnsSwitchOn() {
    this->runPowerOn(Fw::CmdResponse::OK);

    ASSERT_EVENTS_SIZE(1);
    ASSERT_EVENTS_SwitchStateChanged_SIZE(1);
    ASSERT_EVENTS_SwitchStateChanged(0, PayloadPowerSwitch_SwitchState::ON);
    ASSERT_EVENTS_PowerOnRejected_SIZE(0);
    ASSERT_TLM_SIZE(1);
    ASSERT_TLM_PayloadSwitch_SIZE(1);
    ASSERT_TLM_PayloadSwitch(0, PayloadPowerSwitch_SwitchState::ON);
}

void PayloadPowerSwitchTester ::testPowerOnInSafeIsRejectedAndSwitchStaysOff() {
    this->runSetMode(PayloadPowerSwitch_Mode::SAFE, Fw::CmdResponse::OK);

    this->runPowerOn(Fw::CmdResponse::EXECUTION_ERROR);

    ASSERT_EVENTS_SIZE(1);
    ASSERT_EVENTS_PowerOnRejected_SIZE(1);
    ASSERT_EVENTS_PowerOnRejected(0, PayloadPowerSwitch_Mode::SAFE);
    ASSERT_EVENTS_SwitchStateChanged_SIZE(0);
    // The unchanged position is still reported, so the ground can confirm OFF.
    ASSERT_TLM_SIZE(1);
    ASSERT_TLM_PayloadSwitch_SIZE(1);
    ASSERT_TLM_PayloadSwitch(0, PayloadPowerSwitch_SwitchState::OFF);
}

void PayloadPowerSwitchTester ::testPowerOnInSafeLeavesOnSwitchUnchanged() {
    this->runPowerOn(Fw::CmdResponse::OK);
    this->runSetMode(PayloadPowerSwitch_Mode::SAFE, Fw::CmdResponse::OK);

    this->runPowerOn(Fw::CmdResponse::EXECUTION_ERROR);

    ASSERT_EVENTS_PowerOnRejected_SIZE(1);
    ASSERT_EVENTS_SwitchStateChanged_SIZE(0);
    ASSERT_TLM_PayloadSwitch_SIZE(1);
    ASSERT_TLM_PayloadSwitch(0, PayloadPowerSwitch_SwitchState::ON);
}

void PayloadPowerSwitchTester ::testPowerOffAllowedInSafe() {
    this->runPowerOn(Fw::CmdResponse::OK);
    this->runSetMode(PayloadPowerSwitch_Mode::SAFE, Fw::CmdResponse::OK);

    this->runPowerOff(Fw::CmdResponse::OK);

    ASSERT_EVENTS_SIZE(1);
    ASSERT_EVENTS_SwitchStateChanged_SIZE(1);
    ASSERT_EVENTS_SwitchStateChanged(0, PayloadPowerSwitch_SwitchState::OFF);
    ASSERT_TLM_PayloadSwitch_SIZE(1);
    ASSERT_TLM_PayloadSwitch(0, PayloadPowerSwitch_SwitchState::OFF);
}

void PayloadPowerSwitchTester ::testSetModeDoesNotMoveSwitch() {
    this->runPowerOn(Fw::CmdResponse::OK);

    this->runSetMode(PayloadPowerSwitch_Mode::SAFE, Fw::CmdResponse::OK);
    ASSERT_EVENTS_SIZE(1);
    ASSERT_EVENTS_ModeChanged_SIZE(1);
    ASSERT_EVENTS_ModeChanged(0, PayloadPowerSwitch_Mode::SAFE);
    ASSERT_EVENTS_SwitchStateChanged_SIZE(0);
    ASSERT_TLM_SIZE(1);
    ASSERT_TLM_PayloadMode_SIZE(1);
    ASSERT_TLM_PayloadMode(0, PayloadPowerSwitch_Mode::SAFE);
    ASSERT_TLM_PayloadSwitch_SIZE(0);

    this->runSetMode(PayloadPowerSwitch_Mode::NORMAL, Fw::CmdResponse::OK);
    ASSERT_EVENTS_ModeChanged(0, PayloadPowerSwitch_Mode::NORMAL);
    ASSERT_TLM_PayloadMode(0, PayloadPowerSwitch_Mode::NORMAL);
    ASSERT_TLM_PayloadSwitch_SIZE(0);
    ASSERT_EVENTS_SwitchStateChanged_SIZE(0);
}

void PayloadPowerSwitchTester ::testReturnToNormalAllowsPowerOn() {
    this->runSetMode(PayloadPowerSwitch_Mode::SAFE, Fw::CmdResponse::OK);
    this->runPowerOn(Fw::CmdResponse::EXECUTION_ERROR);
    this->runSetMode(PayloadPowerSwitch_Mode::NORMAL, Fw::CmdResponse::OK);

    this->runPowerOn(Fw::CmdResponse::OK);

    ASSERT_EVENTS_PowerOnRejected_SIZE(0);
    ASSERT_EVENTS_SwitchStateChanged(0, PayloadPowerSwitch_SwitchState::ON);
    ASSERT_TLM_PayloadSwitch(0, PayloadPowerSwitch_SwitchState::ON);
}

void PayloadPowerSwitchTester ::testPowerOffWhenAlreadyOff() {
    this->runPowerOff(Fw::CmdResponse::OK);

    ASSERT_EVENTS_SwitchStateChanged_SIZE(1);
    ASSERT_EVENTS_SwitchStateChanged(0, PayloadPowerSwitch_SwitchState::OFF);
    ASSERT_TLM_PayloadSwitch(0, PayloadPowerSwitch_SwitchState::OFF);
}

void PayloadPowerSwitchTester ::testInvalidModeIsRejected() {
    // Encode SET_MODE by hand with a value that is neither NORMAL nor SAFE.
    this->clearHistory();
    Fw::CmdArgBuffer args;
    const PayloadPowerSwitch_Mode::SerialType notAMode = 99;
    ASSERT_EQ(Fw::FW_SERIALIZE_OK, args.serializeFrom(notAMode));
    const U32 seq = ++this->m_cmdSeq;
    this->sendRawCmd(PayloadPowerSwitchComponentBase::OPCODE_SET_MODE, seq, args);
    this->dispatchOneMessage();

    ASSERT_CMD_RESPONSE_SIZE(1);
    ASSERT_CMD_RESPONSE(0, PayloadPowerSwitchComponentBase::OPCODE_SET_MODE, seq,
                        Fw::CmdResponse::FORMAT_ERROR);
    ASSERT_EVENTS_ModeChanged_SIZE(0);
    ASSERT_TLM_SIZE(0);

    // The mode is still NORMAL, so POWER_ON still works.
    this->runPowerOn(Fw::CmdResponse::OK);
    ASSERT_TLM_PayloadSwitch(0, PayloadPowerSwitch_SwitchState::ON);
}

// ----------------------------------------------------------------------
// Helper functions
// ----------------------------------------------------------------------

void PayloadPowerSwitchTester ::dispatchOneMessage() {
    ASSERT_EQ(PayloadPowerSwitchComponentBase::MSG_DISPATCH_OK, this->dispatchOne(this->component));
}

void PayloadPowerSwitchTester ::runPowerOn(Fw::CmdResponse expected) {
    this->clearHistory();
    const U32 seq = ++this->m_cmdSeq;
    this->sendCmd_POWER_ON(TEST_INSTANCE_ID, seq);
    this->dispatchOneMessage();
    ASSERT_CMD_RESPONSE_SIZE(1);
    ASSERT_CMD_RESPONSE(0, PayloadPowerSwitchComponentBase::OPCODE_POWER_ON, seq, expected);
}

void PayloadPowerSwitchTester ::runPowerOff(Fw::CmdResponse expected) {
    this->clearHistory();
    const U32 seq = ++this->m_cmdSeq;
    this->sendCmd_POWER_OFF(TEST_INSTANCE_ID, seq);
    this->dispatchOneMessage();
    ASSERT_CMD_RESPONSE_SIZE(1);
    ASSERT_CMD_RESPONSE(0, PayloadPowerSwitchComponentBase::OPCODE_POWER_OFF, seq, expected);
}

void PayloadPowerSwitchTester ::runSetMode(PayloadPowerSwitch_Mode mode, Fw::CmdResponse expected) {
    this->clearHistory();
    const U32 seq = ++this->m_cmdSeq;
    this->sendCmd_SET_MODE(TEST_INSTANCE_ID, seq, mode);
    this->dispatchOneMessage();
    ASSERT_CMD_RESPONSE_SIZE(1);
    ASSERT_CMD_RESPONSE(0, PayloadPowerSwitchComponentBase::OPCODE_SET_MODE, seq, expected);
}

}  // namespace CommandRehearsal
