"""Regression coverage for the Kiln mode-2 AFC restoration wrapper."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

spec = importlib.util.spec_from_file_location(
    "afc_kiln", Path(__file__).resolve().parents[1] / "extras" / "afc_kiln.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class Mode2Restore(unittest.TestCase):
    def make_afc(self, mode=2, state="printing", backend="kiln",
                 paused=False, error=False):
        class AFC:
            def cmd_CHANGE_TOOL(self, gcmd):
                self.restore_pos()

            def restore_pos(self, move_z_first=True):
                self.original_calls += 1

        mod = NS(afc=AFC, State=NS(RESTORING_POS="restoring", IDLE="idle"),
                 set_gcode_absolute_extrude=lambda gm, value:
                 setattr(gm, "absolute_extrude", value))
        bootstrap.wrap_mode2_restore(mod)
        bootstrap.wrap_mode2_restore(mod)
        afc = AFC()
        objects = {
            "save_variables": NS(allVariables={"use_trash_on_print": mode}),
            "print_stats": NS(get_status=lambda t: {"state": state}),
        }
        afc.printer = NS(lookup_object=lambda name, default=None:
                         objects.get(name, default))
        afc.reactor = NS(monotonic=lambda: 0)
        afc.function = NS(is_paused=lambda: paused)
        afc.error_state = error
        afc.tool_cmds = {"T0": "lane1"}
        afc.lanes = {"lane1": NS(unit_obj=NS(backend=NS(name=backend)))}
        afc.in_toolchange = True
        afc.position_saved = True
        afc.original_calls = 0
        afc.base_position = [0, 0, 0, 100]
        afc.homing_position = [0, 0, 0, 0]
        afc.last_gcode_position = [100, 100, 2, 120]
        afc.absolute_coord = True
        afc.absolute_extrude = False
        afc.extrude_factor = 1.0
        afc.speed = 300
        afc.speed_factor = 1 / 60
        afc.gcode_move = NS(last_position=[52.5, 220, 7, 108])
        afc.logger = NS(info=lambda msg: None)
        return afc

    def run_tool(self, afc, command="T0"):
        afc.cmd_CHANGE_TOOL(NS(get_commandline=lambda: command))

    def test_mode2_keeps_xyz_and_restores_extrusion_state(self):
        afc = self.make_afc()
        self.run_tool(afc)
        gm = afc.gcode_move
        self.assertEqual(gm.last_position, [52.5, 220, 7, 108])
        self.assertEqual(gm.base_position, [0, 0, 0, 88])
        self.assertFalse(gm.absolute_extrude)
        self.assertTrue(gm.absolute_coord)
        self.assertEqual(gm.speed, 300)
        self.assertEqual(gm.extrude_factor, 1)
        self.assertEqual(gm.homing_position, afc.homing_position)
        self.assertFalse(afc.position_saved)
        self.assertEqual(afc.current_state, "idle")
        self.assertEqual(afc.original_calls, 0)
        afc.restore_pos()
        self.assertEqual(afc.original_calls, 1)

    def test_other_paths_keep_original_restore(self):
        cases = [
            {"mode": 0}, {"mode": 1}, {"mode": None},
            {"state": "standby"}, {"state": "paused"},
            {"backend": "zmod"}, {"backend": "other"},
            {"paused": True}, {"error": True},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                afc = self.make_afc(**kwargs)
                self.run_tool(afc)
                self.assertEqual(afc.original_calls, 1)
        afc = self.make_afc()
        self.run_tool(afc, "CHANGE_TOOL LANE=lane1")
        self.assertEqual(afc.original_calls, 1)

    def test_runout_direct_restore_is_unchanged(self):
        afc = self.make_afc()
        afc.restore_pos(False)
        self.assertEqual(afc.original_calls, 1)


if __name__ == "__main__":
    unittest.main()
