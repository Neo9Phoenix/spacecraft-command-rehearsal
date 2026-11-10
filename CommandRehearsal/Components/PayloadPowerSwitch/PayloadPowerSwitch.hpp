// ======================================================================
// \title  PayloadPowerSwitch.hpp
// \author neophoenix
// \brief  hpp file for PayloadPowerSwitch component implementation class
// ======================================================================

#ifndef CommandRehearsal_PayloadPowerSwitch_HPP
#define CommandRehearsal_PayloadPowerSwitch_HPP

#include "CommandRehearsal/Components/PayloadPowerSwitch/PayloadPowerSwitchComponentAc.hpp"

namespace CommandRehearsal {

class PayloadPowerSwitch final : public PayloadPowerSwitchComponentBase {
  public:
    // ----------------------------------------------------------------------
    // Component construction and destruction
    // ----------------------------------------------------------------------

    //! Construct PayloadPowerSwitch object
    PayloadPowerSwitch(const char* const compName  //!< The component name
    );

    //! Destroy PayloadPowerSwitch object
    ~PayloadPowerSwitch();

  private:
    // ----------------------------------------------------------------------
    // Handler implementations for commands
    // ----------------------------------------------------------------------

    //! Handler implementation for command POWER_ON
    //!
    //! Turn the payload power switch on. Rejected with EXECUTION_ERROR in SAFE mode.
    void POWER_ON_cmdHandler(FwOpcodeType opCode,  //!< The opcode
                             U32 cmdSeq            //!< The command sequence number
                             ) override;

    //! Handler implementation for command POWER_OFF
    //!
    //! Turn the payload power switch off. Allowed in every mode.
    void POWER_OFF_cmdHandler(FwOpcodeType opCode,  //!< The opcode
                              U32 cmdSeq            //!< The command sequence number
                              ) override;

    //! Handler implementation for command SET_MODE
    //!
    //! Set the operating mode of the payload power switch
    void SET_MODE_cmdHandler(FwOpcodeType opCode,                                      //!< The opcode
                             U32 cmdSeq,                                               //!< The command sequence number
                             const CommandRehearsal::PayloadPowerSwitch_Mode& newMode  //!< The new operating mode
                             ) override;

  private:
    // ----------------------------------------------------------------------
    // Simulated hardware state (no hardware is driven)
    // ----------------------------------------------------------------------

    //! Simulated switch position; the payload starts unpowered
    PayloadPowerSwitch_SwitchState m_switch = PayloadPowerSwitch_SwitchState::OFF;

    //! Operating mode; the switch starts in NORMAL mode
    PayloadPowerSwitch_Mode m_mode = PayloadPowerSwitch_Mode::NORMAL;
};

}  // namespace CommandRehearsal

#endif
