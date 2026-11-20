# Test-only Klipper module for emu_kiln_afc.py ([emu_head_sensor]).
#
# The emulator's toolhead sensor is a switch on a pin nothing drives.
# TEST_HEAD_SENSOR PRESENT=0|1 feeds a reading to its RunoutHelper, as the
# real sensor does when a strand's end passes it.
import logging

class EmuHeadSensor:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.sensor_name = config.get('sensor', 'filament_switch_sensor toolhead')
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command("TEST_HEAD_SENSOR", self.cmd_TEST_HEAD_SENSOR)
    def cmd_TEST_HEAD_SENSOR(self, gcmd):
        present = bool(gcmd.get_int('PRESENT', minval=0, maxval=1))
        helper = self.printer.lookup_object(self.sensor_name).runout_helper
        now = self.printer.get_reactor().monotonic()
        # kiln_test_probe reads idle_timeout as Ready between prints and batch
        # mode doesn't always set it back: a printing file reads Printing, as
        # on a printer
        idle_timeout = self.printer.lookup_object("idle_timeout")
        ps = self.printer.lookup_object("print_stats").get_status(now)["state"]
        if ps == "printing":
            idle_timeout.state = "Printing"
        idle = idle_timeout.get_status(now)["state"]
        logging.info("emu_head_sensor: present %s -> %s, idle %s, event window %s,"
                     " enabled %s", helper.filament_present, present, idle,
                     helper.min_event_systime <= now, helper.sensor_enabled)
        helper.note_filament_present(now, present)

def load_config(config):
    return EmuHeadSensor(config)
