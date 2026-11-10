// ======================================================================
// \title  PayloadPowerSwitchTestMain.cpp
// \author neophoenix
// \brief  cpp file for PayloadPowerSwitch component test main function
// ======================================================================

#include "PayloadPowerSwitchTester.hpp"

TEST(Nominal, PowerOnInNormalTurnsSwitchOn) {
    CommandRehearsal::PayloadPowerSwitchTester tester;
    tester.testPowerOnInNormalTurnsSwitchOn();
}

TEST(Safe, PowerOnInSafeIsRejectedAndSwitchStaysOff) {
    CommandRehearsal::PayloadPowerSwitchTester tester;
    tester.testPowerOnInSafeIsRejectedAndSwitchStaysOff();
}

TEST(Safe, PowerOnInSafeLeavesOnSwitchUnchanged) {
    CommandRehearsal::PayloadPowerSwitchTester tester;
    tester.testPowerOnInSafeLeavesOnSwitchUnchanged();
}

TEST(Safe, PowerOffAllowedInSafe) {
    CommandRehearsal::PayloadPowerSwitchTester tester;
    tester.testPowerOffAllowedInSafe();
}

TEST(Mode, SetModeDoesNotMoveSwitch) {
    CommandRehearsal::PayloadPowerSwitchTester tester;
    tester.testSetModeDoesNotMoveSwitch();
}

TEST(Mode, ReturnToNormalAllowsPowerOn) {
    CommandRehearsal::PayloadPowerSwitchTester tester;
    tester.testReturnToNormalAllowsPowerOn();
}

TEST(Nominal, PowerOffWhenAlreadyOff) {
    CommandRehearsal::PayloadPowerSwitchTester tester;
    tester.testPowerOffWhenAlreadyOff();
}

TEST(OffNominal, InvalidModeIsRejected) {
    CommandRehearsal::PayloadPowerSwitchTester tester;
    tester.testInvalidModeIsRejected();
}

int main(int argc, char** argv) {
    ::testing::InitGoogleTest(&argc, argv);
    return RUN_ALL_TESTS();
}
