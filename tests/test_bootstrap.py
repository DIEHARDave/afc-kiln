"""afc_kiln: replacing KILN_AFCLite plugins.cfg, the plugin folder, the
Moonraker include and the spool hand-over, against fake Kiln folders;
and the AFC checkout following AFC_COMMIT, against a local git repo.

    python3 tests/test_retire_afclite.py

No Klipper needed (afc_kiln imports nothing of Klipper's at load).
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
spec = importlib.util.spec_from_file_location(
    "afc_kiln", os.path.join(os.path.dirname(HERE), "extras", "afc_kiln.py"))
afc_kiln = importlib.util.module_from_spec(spec)
spec.loader.exec_module(afc_kiln)


class ConfigError(Exception):
    pass


class Section:
    def __init__(self, opts):
        self.opts = opts
    def get(self, key, default=None, note_valid=True):
        return self.opts.get(key, default)


class Reactor:
    def __init__(self):
        self.callbacks = []
    def monotonic(self):
        return 0.
    def register_callback(self, cb, waketime=0.):
        self.callbacks.append((cb, waketime))


class Printer:
    def __init__(self):
        self.reactor = Reactor()
        self.exits = []
    def get_reactor(self):
        return self.reactor
    def request_exit(self, result):
        self.exits.append(result)


class Config:
    error = ConfigError
    def __init__(self, sections):
        self.sections = sections
        self.printer = Printer()
    def has_section(self, name):
        return name in self.sections
    def getsection(self, name):
        return Section(self.sections[name])
    def get_printer(self):
        return self.printer
    def get(self, key, default=None):
        return self.sections.get('afc_kiln', {}).get(key, default)


def plugin(base, name):
    d = os.path.join(base, name)
    os.makedirs(os.path.join(d, "extras"))
    open(os.path.join(d, "plugin.cfg"), "w").close()
    with open(os.path.join(d, "plugin.json"), "w") as f:
        json.dump({"name": name}, f)
    open(os.path.join(d, "moonraker.conf"), "w").write("[update_manager %s]\n" % name)
    return d


class Retire(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.user = os.path.join(t, "plugins")
        self.cfgdir = os.path.join(t, "config")
        os.makedirs(self.user)
        os.makedirs(os.path.join(self.cfgdir, "AFC"))
        self.lite = plugin(self.user, "afclite")
        self.other = plugin(self.user, "afclite2")
        self.afc = plugin(self.user, "afc")
        self.plugins = os.path.join(self.cfgdir, "plugins.cfg")
        self.mr = os.path.join(self.cfgdir, "moonraker-plugins.conf")
        self.restart = os.path.join(t, "restart-moonraker")
        self.var = os.path.join(self.cfgdir, "AFC", "AFC.var")
        self.vars = os.path.join(self.cfgdir, "variables.cfg")
        with open(self.vars, "w") as f:
            f.write("[Variables]\nkiln_afclite_spool_e0 = 12\n"
                    "kiln_afclite_spool_e2 = 0\nkiln_afclite_weight_e0 = 800.0\n"
                    "kiln_afclite_spool_e3 = 7\nother = 'x'\n")
        self.sections = {
            'ad5x_plugins': {'plugins_file': self.plugins, 'moonraker_file': self.mr,
                             'user_dir': self.user,
                             'moonraker_restart_file': self.restart},
            'AFC': {'VarFile': self.var},
            'save_variables': {'filename': self.vars},
            'afc_kiln': {'afc_path': os.path.join(t, 'no-afc')},
        }
    def tearDown(self):
        self.tmp.cleanup()

    def write_enabled(self, *names):
        lines = ["# Plugins enabled on this printer"] + [
            "[include %s/plugi[n].cfg]" % os.path.join(self.user, n) for n in names]
        open(self.plugins, "w").write("\n".join(lines) + "\n")
        mr = ["# Moonraker parts"] + [
            "[include %s]" % os.path.join(self.user, n, "moonraker.conf") for n in names]
        open(self.mr, "w").write("\n".join(mr + ["[update_manager]",
                                                 "enable_system_updates: False"]) + "\n")

    def test_enabled(self):
        self.write_enabled("camera", "afclite", "afclite2", "afc")
        self.assertTrue(afc_kiln.retire_afclite(Config(self.sections)))
        text = open(self.plugins).read()
        self.assertNotIn("/afclite/", text)
        for n in ("camera", "afclite2", "afc"):
            self.assertIn("/%s/plugi[n].cfg" % n, text)
        self.assertTrue(text.startswith("# Plugins enabled"))
        mr = open(self.mr).read()
        self.assertNotIn("/afclite/moonraker.conf", mr)
        self.assertIn("/afclite2/moonraker.conf", mr)
        self.assertIn("enable_system_updates", mr)
        self.assertFalse(os.path.exists(self.lite))
        self.assertTrue(os.path.isdir(self.other))
        self.assertTrue(os.path.exists(self.restart))
        seed = json.load(open(self.var + afc_kiln.SEED_SUFFIX))
        self.assertEqual(seed, {"slots": {"1": 12, "4": 7}})

    def test_installed_but_off(self):
        self.write_enabled("afc")
        before = open(self.plugins).read()
        self.assertFalse(afc_kiln.retire_afclite(Config(self.sections)))
        self.assertFalse(os.path.exists(self.lite))
        self.assertEqual(open(self.plugins).read(), before)
        self.assertFalse(os.path.exists(self.var + afc_kiln.SEED_SUFFIX))
        self.assertFalse(os.path.exists(self.restart))

    def test_not_there(self):
        import shutil
        shutil.rmtree(self.lite)
        self.write_enabled("afc")
        before = open(self.plugins).read()
        self.assertFalse(afc_kiln.retire_afclite(Config(self.sections)))
        self.assertEqual(open(self.plugins).read(), before)
        self.assertTrue(os.path.isdir(self.other))

    def test_foreign_folder_kept(self):
        # A folder called afclite whose plugin.json names something else
        with open(os.path.join(self.lite, "plugin.json"), "w") as f:
            json.dump({"name": "something"}, f)
        self.write_enabled("afc")
        self.assertFalse(afc_kiln.retire_afclite(Config(self.sections)))
        self.assertTrue(os.path.isdir(self.lite))

    def test_no_kiln(self):
        # The emulator test and plain Klipper: no ad5x_plugins, default
        # paths that don't exist
        self.assertFalse(afc_kiln.retire_afclite(Config({})))

    def test_bootstrap_restarts(self):
        self.write_enabled("afclite", "afc")
        cfg = Config(self.sections)
        with self.assertRaises(ConfigError) as e:
            afc_kiln.KilnAFC(cfg)
        self.assertIn("afclite", str(e.exception))
        (cb, when), = cfg.printer.reactor.callbacks
        self.assertGreater(when, 0)
        cb(when)
        self.assertEqual(cfg.printer.exits, ["restart"])
        # The restarted Klipper has no afclite: the bootstrap goes on (and
        # here stops at the missing AFC checkout)
        cfg2 = Config(self.sections)
        with self.assertRaises(ConfigError) as e:
            afc_kiln.KilnAFC(cfg2)
        self.assertIn("no AFC checkout", str(e.exception))
        self.assertEqual(cfg2.printer.reactor.callbacks, [])


def git(*args, cwd=None):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t"] + list(args),
                   cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL).stdout.decode().strip()


class AfcCheckout(unittest.TestCase):
    """The plugin's AFC checkout follows AFC_COMMIT (sync_afc_checkout)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.repo = os.path.join(t, "AFC-upstream")
        os.makedirs(self.repo)
        git("init", "-q", cwd=self.repo)
        open(os.path.join(self.repo, "AFC.py"), "w").write("v1\n")
        git("add", ".", cwd=self.repo)
        self.c1 = git("commit", "-qm", "one", cwd=self.repo)
        self.dest = os.path.join(t, "plugin", "AFC-Klipper-Add-On")
        self.commit_file = os.path.join(t, "AFC_COMMIT")
    def tearDown(self):
        self.tmp.cleanup()
    def sync(self, commit):
        open(self.commit_file, "w").write(commit + "\n")
        return afc_kiln.sync_afc_checkout(self.dest, self.commit_file, "file://" + self.repo)
    def head(self):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.dest,
                              stdout=subprocess.PIPE).stdout.decode().strip()

    def test_clone_update_and_noop(self):
        self.assertIn("new", self.sync(self.c1))
        self.assertEqual(self.head(), self.c1)
        self.assertIsNone(self.sync(self.c1))
        self.assertIsNone(self.sync(self.c1[:7]))
        # A newer upstream commit: fetched, then checked out
        open(os.path.join(self.repo, "AFC.py"), "w").write("v2\n")
        c2 = git("commit", "-qam", "two", cwd=self.repo)
        note = self.sync(c2)
        self.assertEqual(note, "AFC %s -> %s" % (self.c1[:7], c2[:7]))
        self.assertEqual(open(os.path.join(self.dest, "AFC.py")).read(), "v2\n")
        # Back to the older pin (no network needed)
        self.sync(self.c1)
        self.assertEqual(self.head(), self.c1)

    def test_origin_moved(self):
        self.sync(self.c1)
        subprocess.run(["git", "remote", "set-url", "origin", "https://example.invalid/old.git"],
                       cwd=self.dest, check=True)
        self.assertIsNone(self.sync(self.c1))
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=self.dest,
                             stdout=subprocess.PIPE).stdout.decode().strip()
        self.assertEqual(url, "file://" + self.repo)

    def test_unknown_commit(self):
        self.sync(self.c1)
        with self.assertRaises(RuntimeError):
            self.sync("0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(self.head(), self.c1)

    def test_bad_commit_file(self):
        with self.assertRaises(RuntimeError):
            self.sync("not a commit")


class ForgetModules(unittest.TestCase):
    """RESTART reloads AFC: its cached modules are dropped (_forget_modules)."""
    def test_drops_only_afc_modules(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("AFC_fake_one", "AFC_fake_two"):
                open(os.path.join(d, n + ".py"), "w").close()
            open(os.path.join(d, "notes.txt"), "w").close()
            sys.modules["extras.AFC_fake_one"] = object()
            sys.modules["extras.unrelated_fake"] = object()
            try:
                got = afc_kiln._forget_modules([d, os.path.join(d, "missing")])
                self.assertEqual(got, ["AFC_fake_one"])
                self.assertNotIn("extras.AFC_fake_one", sys.modules)
                self.assertIn("extras.unrelated_fake", sys.modules)
            finally:
                sys.modules.pop("extras.unrelated_fake", None)


class DropIfsUnit(unittest.TestCase):
    """Another AFC unit in place of the IFS: plugin.cfg's IFS unit sections go."""
    CFG = """
[AFC_extruder extruder]
[AFC_hub IFS]
switch_pin: virtual
[AFC_IFS IFS]
hub: IFS
extruder: extruder
[AFC_lane lane1]
unit: IFS:1
[AFC_lane lane5]
unit: IFS:5
[AFC_BoxTurtle Turtle_1]
hub: Turtle_1
[AFC_hub Turtle_1]
switch_pin: PC4
[AFC_stepper lane2]
unit: Turtle_1:2
[AFC_FF]
"""
    def parse(self, text):
        import configparser
        fc = configparser.RawConfigParser(strict=False)
        fc.read_string(text)
        return fc

    def test_drops_ifs_sections_only(self):
        fc = self.parse(self.CFG)
        got = afc_kiln.drop_ifs_unit(fc)
        self.assertEqual(sorted(got), ["AFC_IFS IFS", "AFC_hub IFS",
                                       "AFC_lane lane1", "AFC_lane lane5"])
        self.assertEqual(fc.sections(), ["AFC_extruder extruder", "AFC_BoxTurtle Turtle_1",
                                         "AFC_hub Turtle_1", "AFC_stepper lane2", "AFC_FF"])

    def test_no_ifs_unit_nothing_dropped(self):
        fc = self.parse("[AFC_BoxTurtle Turtle_1]\nhub: Turtle_1\n[AFC_FF]\n")
        self.assertEqual(afc_kiln.drop_ifs_unit(fc), [])
        self.assertEqual(len(fc.sections()), 2)

    def test_shared_hub_kept(self):
        fc = self.parse("[AFC_hub H]\n[AFC_IFS IFS]\nhub: H\n[AFC_lane lane1]\nunit: IFS:1\n"
                        "[AFC_BoxTurtle T]\nhub: H\n")
        self.assertEqual(sorted(afc_kiln.drop_ifs_unit(fc)), ["AFC_IFS IFS", "AFC_lane lane1"])
        self.assertTrue(fc.has_section("AFC_hub H"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
