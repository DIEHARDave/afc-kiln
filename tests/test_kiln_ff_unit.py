"""Kiln OS (AD5X) with AFC driving a non-IFS unit through AFC_FF, in Klipper batch mode.

Kiln's own ifs.py (no IFS on its serial port), Kiln's adc_filament_sensor
on the head ADC (pause on runout), Kiln's purge box and brush macros from
ad5x.cfg / unit.cfg, AFC-dev's extras with AFC_FF and
templates/AFC_FF_kiln.cfg, and an emulated 4-lane BoxTurtle whose filament
path Zmod_Emulator/klipper/ff_unit_sim.py models. Kiln's T0-T3 (IFS_SWAP)
must become _T0-_T3 and AFC's T0-T3 run the changes.

    wsl -e bash -lc 'cd /mnt/d/Dev/3D-Printing/KILN_AFC && python3 tests/test_kiln_ff_unit.py'

Work files in ~/kiln_afc_test (shared with test_kiln_afc.py).
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.dont_write_bytecode = True
sys.path.insert(0, HERE)
import test_kiln_afc as tk  # noqa: E402
from test_kiln_afc import WORK, check, results, wait_for, Api, start_moonraker  # noqa: E402

DEV = os.path.dirname(os.path.dirname(HERE))
LANES = ["lane1", "lane2", "lane3", "lane4"]
SIM_LOG = os.path.join(WORK, "sim_moves.log")
NO_IFS = os.path.join(WORK, "no_ifs")

UNIT_CFG = """
[AFC_BoxTurtle Turtle_1]
hub: Turtle_1
extruder: extruder

[AFC_hub Turtle_1]
switch_pin: gpio170
afc_bowden_length: 1000
""" + "".join("""
[AFC_stepper lane%(n)d]
unit: Turtle_1:%(n)d
step_pin: gpio%(s)d
dir_pin: gpio%(d)d
enable_pin: !gpio%(e)d
microsteps: 16
rotation_distance: 4.65
dist_hub: 100
park_dist: 10
prep: gpio%(p)d
load: gpio%(l)d
""" % {'n': n, 's': 100 + 4 * n, 'd': 101 + 4 * n, 'e': 102 + 4 * n,
       'p': 150 + n, 'l': 160 + n} for n in range(1, 5))

CUT_SIM = """
[gcode_macro _AFC_FF_CUT]
gcode:
    {% set unit = printer['gcode_macro _AD5X_UNIT'] %}
    RESPOND MSG="AFC_FF: cut"
    G90
    G1 Y10 F18000
    G1 Y{unit.cut_y_offset - 7.5} F1800
    G1 X20 F12000
    G1 X{unit.cut_x_offset - 2.5} F600
    M400
    M83
    G1 E-5 F600
    M400
    FF_SIM_CUT
    G90
    G1 X20 F12000
    M400

"""

PROBE = ["AFC", "AFC_extruder extruder", "print_stats", "ifs", "ff_unit_sim",
         "filament_motion_sensor ff_head", "filament_switch_sensor toolhead",
         ] + ["AFC_stepper %s" % n for n in LANES]

def sections(path, names):
    """The named [sections] of a Klipper config file, verbatim."""
    out, keep = [], False
    for line in open(path, encoding="utf-8").read().splitlines():
        m = re.match(r"^\[([^\]]+)\]", line)
        if m:
            keep = m.group(1).strip() in names
        if keep:
            out.append(line)
    return "\n".join(out) + "\n"

def build_klippy(args):
    src = os.path.join(args.klipper, "klippy")
    dst = os.path.join(WORK, "klipper", "klippy")
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
        "__pycache__", "kiln_*", "ifs.py", "spoolman_slots.py", "AFC*.py",
        "adc_filament_sensor.py", "ff_unit_sim.py"))
    extras = os.path.join(dst, "extras")
    def link(name, target):
        os.symlink(target, os.path.join(extras, name))
    if args.source == "plugin":
        # KILN_AFC's bootstrap finds AFC (afc_path) and afc_ifs/ (AFC_IFS, AFC_FF)
        link("afc_kiln.py", os.path.join(tk.PLUGIN, "extras", "afc_kiln.py"))
    else:
        afc = os.path.join(args.afc_root, "extras")
        for f in sorted(os.listdir(afc)):
            if f.endswith(".py") and f != "__init__.py":
                link(f, os.path.join(afc, f))
    kx = os.path.join(args.kiln, "klipper", "extras")
    link("ifs.py", os.path.join(kx, "ifs.py"))
    link("adc_filament_sensor.py", os.path.join(kx, "adc_filament_sensor.py"))
    link("kiln_test_probe.py", os.path.join(args.emu, "klipper_test", "kiln_test_probe.py"))
    link("ff_unit_sim.py", os.path.join(args.sim, "ff_unit_sim.py"))
    return dst

def build_config(args):
    base = open(os.path.join(args.emu, "klipper_test", "printer.cfg")).read()
    base = base.replace("channels: auto\n", "")
    base = base.replace("/tmp/kiln_ifs_test", WORK).replace("serial: /tmp/ifs\n",
                                                             "serial: %s\n" % NO_IFS)
    kiln = os.path.join(args.kiln, "klipper")
    kiln_macros = (sections(os.path.join(kiln, "unit.cfg"), {"gcode_macro _AD5X_UNIT"})
                   + sections(os.path.join(kiln, "ad5x.cfg"),
                              {"gcode_macro _PURGE_BOX", "gcode_macro _PURGE_WIPE",
                               "gcode_macro _NOZZLE_BRUSH"}))
    # The cut also tells the model the strand got shorter
    cut = CUT_SIM
    if args.source == "plugin":
        # What KILN_AFC ships: plugin.cfg (IFS sections, [afc_kiln] bootstrap,
        # the AFC/afc_unit.cfg include) + afc_unit.example.cfg with the unit
        afc_cfg = tk.plugin_cfg(args)
        unit_file = (open(os.path.join(tk.PLUGIN, "afc_unit.example.cfg")).read()
                     + UNIT_CFG + cut)
        open(os.path.join(WORK, "AFC", "afc_unit.cfg"), "w").write(unit_file)
    else:
        tmpl = open(os.path.join(args.afc_root, "templates", "AFC_FF_kiln.cfg")).read()
        afc_cfg = (tmpl + UNIT_CFG + "\n[AFC]\nVarFile: %s/AFC/AFC.var\nmoonraker_port: %d\n"
                   "moonraker_host: http://127.0.0.1\n" % (WORK, tk.MOONRAKER_PORT) + cut)
    cfg = base + """
# -- test: the AFC unit's filament path, before the sensors that read the head ADC
[ff_unit_sim]
dir: %(work)s
lanes: %(lanes)s
loaded: lane1, lane2, lane3

[fan]
pin: gpio17

# AD5X axis limits (kiln-os ad5x.cfg): the cutter is at Y -7.5, the box at Y 229
[stepper_x]
position_min: -20
position_max: 225

[stepper_y]
position_min: -20
position_max: 232

[heater_bed]
heater_pin: gpio16
sensor_type: EPCOS 100K B57560G104F
sensor_pin: analog2
control: watermark
min_temp: -273
max_temp: 130

# -- Kiln's AD5X head sensor (ad5x.cfg)
[temperature_sensor filamentValue]
sensor_type: EPCOS 100K B57560G104F
sensor_pin: analog3
min_temp: -273
max_temp: 100000

[adc_filament_sensor toolhead]
adc: temperature_sensor filamentValue
present_below: 0.30
present_above: 0.72
pause_on_runout: True

# -- Kiln's macros (unit.cfg, ad5x.cfg)
%(kiln_macros)s
%(afc)s
[kiln_test_probe]
dir: %(work)s
objects: %(objects)s
""" % {'work': WORK, 'lanes': ", ".join(LANES), 'kiln_macros': kiln_macros,
       'afc': afc_cfg, 'objects': ", ".join(PROBE)}
    path = os.path.join(WORK, "printer.cfg")
    open(path, "w").write(cfg)
    return path

class Klippy:
    def __init__(self, args, klippy_dir, cfg):
        self.fifo = os.path.join(WORK, "input.fifo")
        os.mkfifo(self.fifo)
        self.log = os.path.join(WORK, "klippy.log")
        self.writer = os.open(self.fifo, os.O_RDWR)
        self.proc = subprocess.Popen(
            [args.python, os.path.join(klippy_dir, "klippy.py"), cfg,
             "-i", self.fifo, "-o", os.path.join(WORK, "out.serial"),
             "-d", os.path.join(args.klipper, "out", "klipper.dict"), "-l", self.log],
            stdout=open(os.path.join(WORK, "klippy.stdout"), "w"), stderr=subprocess.STDOUT)
        self.api = Api(self.writer)
        status = os.path.join(WORK, "status.json")
        for i in range(300):
            if os.path.exists(status) or self.proc.poll() is not None:
                break
            time.sleep(.1)
        self.ready = os.path.exists(status)
    def stop(self):
        try:
            os.close(self.writer)
        except OSError:
            pass
        try:
            self.proc.wait(15)
        except subprocess.TimeoutExpired:
            self.proc.kill()

class LogMark:
    def __init__(self, path):
        self.path = path
        self.pos = os.path.getsize(path) if os.path.exists(path) else 0
    def lines(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path, errors="replace") as f:
            f.seek(self.pos)
            return f.read().splitlines()

def obj(api, name):
    return api.query(name)[name] or {}

def lane(api, n):
    return obj(api, "AFC_stepper lane%d" % n)

def lanes(api, key):
    return [lane(api, n).get(key) for n in (1, 2, 3, 4)]

def afc(api):
    return obj(api, "AFC")

def ff(api):
    return obj(api, "filament_motion_sensor ff_head")

def sim(api):
    return obj(api, "ff_unit_sim")

def tip(api, name):
    return (sim(api).get("tip") or {}).get(name)

def head_at(api, name):
    return (sim(api).get("head_at") or {}).get(name, 0)

def parked(api, n):
    l = lane(api, n)
    return (l.get("status") in ("None", "Loaded") and l.get("loaded_to_hub") is True
            and l.get("tool_loaded") is False and l.get("prep") and l.get("load"))

def run(args, k):
    api = k.api
    if not check("klipper ready: Kiln ifs.py + adc sensor, AFC + AFC_FF, emulated BoxTurtle",
                 k.ready, "see %s" % k.log):
        return
    st = afc(api)
    check("AFC object exists", st and '_error' not in st, st.get('_error'))
    log = open(k.log, errors="replace").read()
    if args.source == "plugin":
        check("bootstrap: AFC/afc_unit.cfg's [AFC_FF] drops plugin.cfg's IFS unit sections",
              "dropped AFC_IFS IFS, AFC_lane lane1, AFC_lane lane2, AFC_lane lane3, "
              "AFC_lane lane4, AFC_hub IFS" in log)
    check("AFC_FF shortened the head ADC report time",
          "AFC_FF: temperature_sensor filamentValue report time 0.300 -> 0.050" in log)
    check("Kiln: no IFS (ifs not connected)", not wait_for(
        lambda: obj(api, "ifs").get("connected"), 5), obj(api, "ifs").get("connected"))
    check("Kiln's toolhead runout sensor disabled by AFC_FF",
          wait_for(lambda: obj(api, "filament_switch_sensor toolhead").get("enabled") is False, 10),
          obj(api, "filament_switch_sensor toolhead"))
    api.gcode("SET_KINEMATIC_POSITION X=100 Y=100 Z=10")

    prepped = wait_for(lambda: lanes(api, "status") == ["Loaded", "Loaded", "Loaded", "None"],
                       60, step=.5)
    check("PREP: lanes 1-3 Loaded, lane4 empty", prepped, lanes(api, "status"))
    check("lane maps T0-T3", lanes(api, "map") == [["T0"], ["T1"], ["T2"], ["T3"]],
          lanes(api, "map"))
    check("head sensor clear before any load, Kiln's sensor agrees",
          ff(api).get("filament_detected") is False
          and obj(api, "filament_switch_sensor toolhead").get("filament_detected") is False,
          (ff(api), obj(api, "filament_switch_sensor toolhead")))

    mark = LogMark(SIM_LOG)
    ok, err, out = api.gcode("T0")
    check("T0: AFC's T0 (Kiln's IFS_SWAP T0 renamed by force_assign_map) loads lane1",
          ok and afc(api).get("current_load") == "lane1"
          and lane(api, 1).get("tool_loaded") is True
          and not any("IFS" in l for l in out),
          err or (afc(api).get("current_load"), lane(api, 1).get("status"), out[-3:]))
    check("T0: homed on the head sensor (AFC_FF)",
          any("lane1 home tool" in l and "homed" in l for l in mark.lines()),
          [l for l in mark.lines() if "home" in l][-2:])
    check("T0: Kiln's sensor sees filament too (same ADC)",
          obj(api, "filament_switch_sensor toolhead").get("filament_detected") is True,
          obj(api, "filament_switch_sensor toolhead"))

    cuts = sim(api).get("cuts", 0)
    ok, err, out = api.gcode("T1")
    text = "\n".join(out)
    check("T1: lane2 loaded, lane1 back in the unit",
          ok and afc(api).get("current_load") == "lane2" and parked(api, 1),
          err or (afc(api).get("current_load"), lanes(api, "status")))
    check("T1: Kiln's cutter moves ran (_AFC_FF_CUT)",
          sim(api).get("cuts", 0) == cuts + 1 and "AFC_FF: cut" in text,
          (sim(api).get("cuts"), [l for l in out if "AFC_FF" in l]))
    check("T1: parked, purged and brushed at Kiln's box (_PURGE_BOX, _PURGE_WIPE, _NOZZLE_BRUSH)",
          all(s in text for s in ("AFC_FF: park", "AFC_FF: purge", "AFC_FF: brush")),
          [l for l in out if "AFC_FF" in l])
    check("T1: lane1's strand clear of the head",
          (tip(api, "lane1") or 0) < head_at(api, "lane1"), tip(api, "lane1"))

    ok, err, out = api.gcode("TOOL_UNLOAD")
    check("TOOL_UNLOAD: head empty, nothing loaded, lane2 back in the unit",
          ok and ff(api).get("filament_detected") is False
          and afc(api).get("current_load") is None and parked(api, 2),
          err or (ff(api), afc(api).get("current_load"), lanes(api, "status")))

    api.gcode("FF_SIM_INSERT LANE=lane4")
    check("spool into lane4: AFC preps it (Loaded)",
          wait_for(lambda: lane(api, 4).get("status") == "Loaded", 30, step=.5),
          (lane(api, 4).get("status"), tip(api, "lane4")))

    open(os.path.join(WORK, "gcodes", "ff.gcode"), "w").write(
        "; two changes\nG90\nT0\nTEST_SLEEP S=6\nG1 X20 Y20 F6000\nG1 E2 F300\n"
        "T3\nG1 X30 Y30\nG1 E2 F300\nTEST_SLEEP S=2\nT1\nG1 X40 Y40\nG1 E2 F300\n"
        "TEST_SLEEP S=2\n")
    cuts = sim(api).get("cuts", 0)
    ok, err, out = api.gcode("SDCARD_PRINT_FILE FILENAME=ff.gcode")
    wait_for(lambda: obj(api, "print_stats").get("state") in ("complete", "error", "cancelled",
                                                              "paused"), 400, step=.5)
    wait_for(lambda: obj(api, "print_stats").get("state") in ("complete", "error", "cancelled"),
             30, step=.5)
    state = obj(api, "print_stats")
    check("print: ff.gcode (T0 -> T3 -> T1) completes, no runout pause",
          ok and state.get("state") == "complete", err or state)
    check("print: ends on lane2 (T1), two cuts",
          afc(api).get("current_load") == "lane2" and sim(api).get("cuts", 0) == cuts + 2,
          (afc(api).get("current_load"), sim(api).get("cuts")))

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--klipper", default="~/kiln-klipper")
    ap.add_argument("--source", choices=("plugin", "afc-dev"), default="plugin",
                    help="plugin: KILN_AFC's plugin.cfg, bootstrap, afc_ifs/AFC_FF.py and "
                         "afc_unit.example.cfg; afc-dev: --afc-root's extras and "
                         "templates/AFC_FF_kiln.cfg")
    ap.add_argument("--afc-root", default=os.path.join(DEV, "AFC-dev"),
                    help="AFC checkout (plugin mode: the afc_path, at AFC_COMMIT)")
    ap.add_argument("--kiln", default=os.path.join(DEV, "kiln-os"), help="kiln-os checkout")
    ap.add_argument("--emu", default=os.path.join(DEV, "KILN_IFSJacker", "emulator"))
    ap.add_argument("--sim", default=os.path.join(DEV, "Zmod_Emulator", "klipper"),
                    help="dir with ff_unit_sim.py")
    ap.add_argument("--python", default="~/klippy-test-env/bin/python")
    args = ap.parse_args()
    for a in ("klipper", "afc_root", "kiln", "emu", "sim", "python"):
        setattr(args, a, os.path.expanduser(getattr(args, a)))
    args.afc = args.afc_root        # test_kiln_afc.plugin_cfg's afc_path

    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(os.path.join(WORK, "gcodes"))
    os.makedirs(os.path.join(WORK, "AFC"))
    open(os.path.join(WORK, "variables.cfg"), "w").close()
    klippy_dir = build_klippy(args)
    cfg = build_config(args)
    start_moonraker()
    k = Klippy(args, klippy_dir, cfg)
    try:
        run(args, k)
    except Exception as e:
        import traceback
        traceback.print_exc()
        check("harness ran to the end", False, "%s: %s" % (type(e).__name__, e))
    finally:
        k.stop()
        print("logs: %s, %s, %s" % (k.log, os.path.join(WORK, "AFC.log"), SIM_LOG))
    failed = [r for r in results if not r[1]]
    print("\n%d checks, %d failed" % (len(results), len(failed)))
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
