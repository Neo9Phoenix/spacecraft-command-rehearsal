// ======================================================================
// \title  PayloadPowerSwitchTester.hpp
// \author neophoenix
// \brief  hpp file for PayloadPowerSwitch component test harness implementation class
// ======================================================================

#ifndef CommandRehearsal_PayloadPowerSwitchTester_HPP
#define CommandRehearsal_PayloadPowerSwitchTester_HPP

#include "CommandRehearsal/Components/PayloadPowerSwitch/PayloadPowerSwitch.hpp"
#include "CommandRehearsal/Components/PayloadPowerSwitch/PayloadPowerSwitchGTestBase.hpp"

namespace CommandRehearsal {

class PayloadPowerSwitchTester final : public PayloadPowerSwitchGTestBase {
  public:
    // ----------------------------------------------------------------------
    // Constants
    // ----------------------------------------------------------------------

    // Maximum size of histories storing events, telemetry, and port outputs
    static const FwSizeType MAX_HISTORY_SIZE = 10;

    // Instance ID supplied to the component instance under test
    static const FwEnumStoreType TEST_INSTANCE_ID = 0;

    // Queue depth supplied to the component instance under test
    static const FwSizeType TEST_INSTANCE_QUEUE_DEPTH = 10;

  public:
    // ----------------------------------------------------------------------
    // Construction and destruction
    // ----------------------------------------------------------------------

    //! Construct object PayloadPowerSwitchTester
    PayloadPowerSwitchTester();

    //! Destroy object PayloadPowerSwitchTester
    ~PayloadPowerSwitchTester();

  public:
    // ----------------------------------------------------------------------
    // Tests
    // ----------------------------------------------------------------------

    //! POWER_ON in NORMAL mode turns the switch ON
    void testPowerOnInNormalTurnsSwitchOn();

    //! POWER_ON in SAFE mode is rejected and the switch stays OFF
    void testPowerOnInSafeIsRejectedAndSwitchStaysOff();

    //! A rejected POWER_ON in SAFE mode leaves an already-ON switch ON
    void testPowerOnInSafeLeavesOnSwitchUnchanged();

    //! POWER_OFF is allowed in SAFE mode
    void testPowerOffAllowedInSafe();

    //! SET_MODE changes the mode without moving the switch
    void testSetModeDoesNotMoveSwitch();

    //! Returning from SAFE to NORMAL allows POWER_ON again
    void testReturnToNormalAllowsPowerOn();

    //! POWER_OFF when already OFF succeeds and reports OFF
    void testPowerOffWhenAlreadyOff();

    //! SET_MODE with an out-of-range mode value is rejected and changes nothing
    void testInvalidModeIsRejected();

  private:
    // ----------------------------------------------------------------------
    // Helper functions
    // ----------------------------------------------------------------------

    //! Connect ports
    void connectPorts();

    //! Initialize components
    void initComponents();

    //! Process the one message the last command queued on the active component
    void dispatchOneMessage();

    //! Clear all histories, send one command, process it, and check its response
    void runPowerOn(Fw::CmdResponse expected);
    void runPowerOff(Fw::CmdResponse expected);
    void runSetMode(PayloadPowerSwitch_Mode mode, Fw::CmdResponse expected);

    //! Command sequence number for the next command
    U32 m_cmdSeq = 0;

  private:
    // ----------------------------------------------------------------------
    // Member variables
    // ----------------------------------------------------------------------

    //! The component under test
    PayloadPowerSwitch component;
};

}  // namespace CommandRehearsal

#endif
