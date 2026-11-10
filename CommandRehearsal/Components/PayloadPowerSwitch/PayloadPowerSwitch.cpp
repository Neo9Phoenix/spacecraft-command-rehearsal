// ======================================================================
// \title  PayloadPowerSwitch.cpp
// \author neophoenix
// \brief  cpp file for PayloadPowerSwitch component implementation class
// ======================================================================

#include "CommandRehearsal/Components/PayloadPowerSwitch/PayloadPowerSwitch.hpp"

namespace CommandRehearsal {

// ----------------------------------------------------------------------
// Component construction and destruction
// ----------------------------------------------------------------------

PayloadPowerSwitch ::PayloadPowerSwitch(const char* const compName) : PayloadPowerSwitchComponentBase(compName) {}

PayloadPowerSwitch ::~PayloadPowerSwitch() {}

// ----------------------------------------------------------------------
// Handler implementations for commands
// ----------------------------------------------------------------------

void PayloadPowerSwitch ::POWER_ON_cmdHandler(FwOpcodeType opCode, U32 cmdSeq) {
    if (this->m_mode == PayloadPowerSwitch_Mode::SAFE) {
        // SAFE mode: refuse to power the payload. The switch position is unchanged,
        // and is reported so the ground can confirm it is still OFF.
        this->log_WARNING_HI_PowerOnRejected(this->m_mode);
        this->tlmWrite_PayloadSwitch(this->m_switch);
        this->cmdResponse_out(opCode, cmdSeq, Fw::CmdResponse::EXECUTION_ERROR);
        return;
    }
    this->m_switch = PayloadPowerSwitch_SwitchState::ON;
    this->log_ACTIVITY_HI_SwitchStateChanged(this->m_switch);
    this->tlmWrite_PayloadSwitch(this->m_switch);
    this->cmdResponse_out(opCode, cmdSeq, Fw::CmdResponse::OK);
}

void PayloadPowerSwitch ::POWER_OFF_cmdHandler(FwOpcodeType opCode, U32 cmdSeq) {
    // Turning the payload off is always allowed, in NORMAL and SAFE mode.
    this->m_switch = PayloadPowerSwitch_SwitchState::OFF;
    this->log_ACTIVITY_HI_SwitchStateChanged(this->m_switch);
    this->tlmWrite_PayloadSwitch(this->m_switch);
    this->cmdResponse_out(opCode, cmdSeq, Fw::CmdResponse::OK);
}

void PayloadPowerSwitch ::SET_MODE_cmdHandler(FwOpcodeType opCode,
                                              U32 cmdSeq,
                                              const CommandRehearsal::PayloadPowerSwitch_Mode& newMode) {
    // Changing mode never moves the switch.
    this->m_mode = newMode;
    this->log_ACTIVITY_HI_ModeChanged(this->m_mode);
    this->tlmWrite_PayloadMode(this->m_mode);
    this->cmdResponse_out(opCode, cmdSeq, Fw::CmdResponse::OK);
}

}  // namespace CommandRehearsal
