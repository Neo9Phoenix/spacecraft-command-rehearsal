module CommandRehearsal {
    @ Simulated payload power switch with NORMAL and SAFE modes.
    @ Simulation only: the switch is a state variable, no hardware is driven.
    active component PayloadPowerSwitch {

        # ----------------------------------------------------------------------
        # Types
        # ----------------------------------------------------------------------

        @ Operating mode of the payload power switch
        enum Mode {
            NORMAL @< Commands are executed
            SAFE   @< POWER_ON is rejected; POWER_OFF is still allowed
        }

        @ Position of the payload power switch
        enum SwitchState {
            OFF @< Payload unpowered
            ON  @< Payload powered
        }

        # ----------------------------------------------------------------------
        # Commands
        # ----------------------------------------------------------------------

        @ Turn the payload power switch on. Rejected with EXECUTION_ERROR in SAFE mode.
        async command POWER_ON

        @ Turn the payload power switch off. Allowed in every mode.
        async command POWER_OFF

        @ Set the operating mode of the payload power switch
        async command SET_MODE(
            newMode: Mode @< The new operating mode
        )

        # ----------------------------------------------------------------------
        # Events
        # ----------------------------------------------------------------------

        @ The switch position after a POWER_ON or POWER_OFF command was executed
        event SwitchStateChanged(
            position: SwitchState @< The new switch position
        ) severity activity high format "Payload power switch is now {}"

        @ A POWER_ON command was rejected because the switch is in SAFE mode
        event PowerOnRejected(
            currentMode: Mode @< The mode that caused the rejection
        ) severity warning high format "POWER_ON rejected: payload power switch is in {} mode"

        @ The operating mode was changed
        event ModeChanged(
            newMode: Mode @< The new operating mode
        ) severity activity high format "Payload power switch mode set to {}"

        # ----------------------------------------------------------------------
        # Telemetry
        # ----------------------------------------------------------------------

        @ Current switch position, reported after every POWER_ON/POWER_OFF command,
        @ including a rejected one
        telemetry PayloadSwitch: SwitchState

        @ Current operating mode, reported after every SET_MODE command
        telemetry PayloadMode: Mode

        ###############################################################################
        # Standard AC Ports: Required for Channels, Events, Commands, and Parameters  #
        ###############################################################################
        @ Port for requesting the current time
        time get port timeCaller

        @ Enables command handling
        import Fw.Command

        @ Enables event handling
        import Fw.Event

        @ Enables telemetry channels handling
        import Fw.Channel

    }
}
