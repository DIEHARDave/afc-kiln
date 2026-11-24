# KILN_AFC bootstrap: runs AFC-Klipper-Add-On (the AFC Project) on Kiln OS.
#
# Kiln's Klipper lives on a read-only squashfs, so AFC's modules cannot be
# linked into klippy/extras the way install-afc.sh does it. Kiln's plugin
# loader (ad5x_plugins) only takes lowercase module names, and AFC asks
# Klipper for its own helper objects with printer.load_object(), which only
# looks in klippy/extras. This module bridges both:
#
# - extras.__path__ gets the plugin's afc_ifs/ folder and the AFC checkout's
#   extras/ folder, so "from extras.AFC_utils import ..." resolves;
# - printer.load_object is wrapped so a section or object named after a
#   module in those folders (AFC, AFC_lane lane1, AFC_spool, ...) is created
#   from there, the way Klipper's own load_object would;
# - AFC's hardcoded unit-type lists learn the "IFS" type of afc_ifs/AFC_IFS.py;
# - another AFC unit in place of the IFS (AFC/afc_unit.cfg, which
#   afc_unit.example.cfg starts): with an [AFC_FF] section in the config,
#   plugin.cfg's IFS unit sections are dropped and [AFC_FF]
#   (afc_ifs/AFC_FF.py, the AD5X head sensor as AFC's toolhead sensor) is
#   created before [AFC_extruder] needs it.
#
# Since plugin API 1.7 Kiln loads both folders itself (plugin.json
# module_dirs; AFC is a git submodule at the tested commit). The bridge
# stays for an afc_path of your own and for a first start where the
# submodule isn't fetched yet (this module fetches it, then Kiln's module
# list is made already). Both import the same extras.<name> modules.
#
# ad5x_plugins creates [afc_kiln] while it loads plugins, which is before
# Klipper reaches the AFC sections, so the wrapper is in place in time.
#
# Copyright (C) 2026 DIEHARDave. GPLv3, like Klipper and AFC.

import ast
import configparser
import importlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
DEFAULT_AFC_DIR = os.path.join(PLUGIN_DIR, 'AFC-Klipper-Add-On')
# The AFC commit this plugin is tested with; the plugin's own checkout
# (DEFAULT_AFC_DIR) is brought to it at each Klipper start, so a plugin
# update from Moonraker's update manager also updates AFC
AFC_COMMIT_FILE = os.path.join(PLUGIN_DIR, 'AFC_COMMIT')
AFC_REPO = 'https://github.com/AFCProject/AFC-Klipper-Add-On.git'
GIT_TIMEOUT = 300
UNIT_DIR = os.path.join(PLUGIN_DIR, 'afc_ifs')
IFS_TYPE = 'IFS'
# Section of afc_ifs/AFC_FF.py: its presence switches to another AFC unit
FF_SECTION = 'AFC_FF'

# KILN_AFCLite (plugin "afclite") is replaced by this plugin. Both make the
# AFC object and CHANGE_TOOL, so they can't start together; Kiln's
# ad5x_plugins defaults (its [ad5x_plugins] options override them).
AFCLITE = 'afclite'
AFCLITE_SPOOL_PREFIX = 'kiln_afclite_spool_e'
KILN_PATHS = {
    'plugins_file': '/home/klipper/printer_data/config/plugins.cfg',
    'moonraker_file': '/home/klipper/printer_data/config/moonraker-plugins.conf',
    'user_dir': '/data/ad5x/plugins',
    'moonraker_restart_file': '/data/ad5x/kiln-update/restart-moonraker',
}
# ad5x_plugins' include line: [include <dir>/<name>/plugi[n].cfg]
INCLUDE_RE = re.compile(r"^\s*\[include\s+\S*/(?P<name>[A-Za-z0-9_.-]+)/"
                        r"plugi(?:n|\[n\])\.cfg\s*\]\s*$")
# AFC_IFS reads this next to AFC's VarFile: lane spools to keep at its
# first spool sync (afclite's, or Kiln's spoolman slots)
SEED_SUFFIX = '.afclite-spools.json'


def _git(args, cwd=None, timeout=GIT_TIMEOUT):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    env.setdefault('HOME', '/home/klipper')
    try:
        r = subprocess.run(['git'] + args, cwd=cwd, env=env,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return r.returncode, r.stdout.decode('utf-8', 'replace').strip()


def sync_afc_checkout(afc_dir, commit_file=AFC_COMMIT_FILE, repo=AFC_REPO):
    """Clone AFC into afc_dir if it isn't there and check out the commit in
    commit_file (fetching it when it's new). Returns what changed, or None.
    Raises RuntimeError when git fails."""
    with open(commit_file) as f:
        want = f.read().strip().lower()
    if not re.match(r'^[0-9a-f]{7,40}$', want):
        raise RuntimeError("%s holds no commit" % commit_file)
    if not os.path.exists(os.path.join(afc_dir, '.git')):
        # AFC is a git submodule of this plugin: fetch it the git way when
        # the plugin was cloned without --recurse-submodules (a .git file
        # then points into the plugin's .git/modules), else clone it here
        parent = os.path.dirname(os.path.abspath(afc_dir))
        rc = 1
        if os.path.isfile(os.path.join(parent, '.gitmodules')):
            rc, out = _git(['submodule', 'update', '--init', '--quiet', '--',
                            os.path.basename(afc_dir)], cwd=parent)
        if rc:
            rc, out = _git(['clone', '--quiet', repo, afc_dir])
            if rc:
                raise RuntimeError("git clone %s failed: %s"
                                   % (repo, out[-300:]))
        before = None
    else:
        # Older installs cloned AFC from ArmoredTurtle/; it lives at
        # AFCProject/ now (GitHub redirects, but fetch from the real place)
        rc, url = _git(['remote', 'get-url', 'origin'], cwd=afc_dir, timeout=30)
        if rc == 0 and url != repo:
            _git(['remote', 'set-url', 'origin', repo], cwd=afc_dir, timeout=30)
        rc, before = _git(['rev-parse', 'HEAD'], cwd=afc_dir, timeout=30)
        if rc == 0 and before.lower().startswith(want):
            return None
    rc, out = _git(['checkout', '--quiet', want], cwd=afc_dir, timeout=60)
    if rc:
        # A commit newer than the checkout
        rc, out = _git(['fetch', '--quiet', 'origin'], cwd=afc_dir)
        if rc:
            raise RuntimeError("git fetch failed: %s" % (out[-300:],))
        rc, out = _git(['checkout', '--quiet', want], cwd=afc_dir, timeout=60)
        if rc:
            raise RuntimeError("git checkout %s failed: %s"
                               % (want[:7], out[-300:]))
    return "AFC %s -> %s" % ((before or 'new')[:7], want[:7])


def _write_atomic(path, text):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(text)
    os.replace(tmp, path)


def _afclite_spools(config):
    """slot -> spool ID afclite kept in save_variables (it uses Kiln's
    spoolman slots instead when that plugin is on; AFC_IFS reads those)."""
    if not config.has_section('save_variables'):
        return {}
    path = config.getsection('save_variables').get('filename', None,
                                                     note_valid=False)
    if not path:
        return {}
    vars_ = configparser.ConfigParser()
    try:
        vars_.read(os.path.expanduser(path))
        items = vars_.items('Variables') if vars_.has_section('Variables') else []
    except configparser.Error:
        return {}
    spools = {}
    for key, val in items:
        if not key.startswith(AFCLITE_SPOOL_PREFIX):
            continue
        try:
            slot = int(key[len(AFCLITE_SPOOL_PREFIX):]) + 1
            sid = int(ast.literal_eval(val) or 0)
        except (ValueError, SyntaxError, TypeError):
            continue
        if 1 <= slot <= 16 and sid > 0:
            spools[str(slot)] = sid
    return spools


def retire_afclite(config):
    """Disable and delete KILN_AFCLite. Returns True when it was enabled in
    this Klipper start (Klipper then has to restart without it)."""
    opts = dict(KILN_PATHS)
    if config.has_section('ad5x_plugins'):
        sect = config.getsection('ad5x_plugins')
        for key in opts:
            opts[key] = sect.get(key, opts[key], note_valid=False)
    pdir = os.path.join(opts['user_dir'], AFCLITE)
    try:
        with open(opts['plugins_file']) as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    def is_afclite(line):
        m = INCLUDE_RE.match(line)
        return m is not None and m.group('name') == AFCLITE
    keep = [l for l in lines if not is_afclite(l)]
    enabled = len(keep) != len(lines)
    try:
        with open(os.path.join(pdir, 'plugin.json')) as f:
            installed = json.load(f).get('name') == AFCLITE
    except (OSError, ValueError, AttributeError):
        installed = False
    if not enabled and not installed:
        return False
    # Switching over from afclite: the lane spools it had, for AFC_IFS's
    # first spool sync (an afclite that was already off left AFC in charge)
    if enabled and config.has_section('AFC'):
        var = config.getsection('AFC').get('VarFile', None, note_valid=False)
        if var:
            try:
                os.makedirs(os.path.dirname(var), exist_ok=True)
                _write_atomic(var + SEED_SUFFIX,
                              json.dumps({'slots': _afclite_spools(config)}))
            except OSError as e:
                logging.warning("afc_kiln: can't write %s: %s",
                                var + SEED_SUFFIX, e)
    if enabled:
        _write_atomic(opts['plugins_file'], "\n".join(keep) + "\n")
    # Its Moonraker part (update_manager entry) goes too; Moonraker would
    # stop on an include of a file that is gone
    mr_changed = False
    try:
        with open(opts['moonraker_file']) as f:
            mr = f.read().splitlines()
        mine = '/%s/moonraker.conf' % AFCLITE
        mr_keep = [l for l in mr if not (l.strip().startswith('[include')
                                         and mine in l)]
        if mr_keep != mr:
            _write_atomic(opts['moonraker_file'], "\n".join(mr_keep) + "\n")
            mr_changed = True
    except OSError:
        pass
    if installed:
        if os.path.islink(pdir):
            os.unlink(pdir)
        else:
            shutil.rmtree(pdir)
    if mr_changed:
        try:
            with open(opts['moonraker_restart_file'], 'w') as f:
                f.write("plugins\n")
        except OSError as e:
            logging.info("afc_kiln: %s: %s", opts['moonraker_restart_file'], e)
    logging.info("afc_kiln: retired afclite (was %s%s)",
                 "enabled" if enabled else "disabled",
                 ", deleted %s" % pdir if installed else "")
    return enabled


def _add_paths(afc_dir):
    import extras
    dirs = [UNIT_DIR, os.path.join(afc_dir, 'extras')]
    for d in dirs:
        if d not in extras.__path__:
            extras.__path__.append(d)
    return dirs


def _forget_modules(dirs):
    """Drop AFC's modules (and AFC_IFS) that an earlier Klipper start in
    this process imported, so RESTART loads the files on disk: Klipper keeps
    imported modules across RESTART, and Kiln's Moonraker can't restart the
    Klipper service after an update (provider: none). Returns the names."""
    names = set()
    for d in dirs:
        try:
            names.update(fn[:-3] for fn in os.listdir(d) if fn.endswith('.py'))
        except OSError:
            pass
    return sorted(n for n in names
                  if sys.modules.pop('extras.' + n, None) is not None)


def _teach_unit_type():
    # AFC keeps per-type behavior in module-level lists; IFS lanes behave
    # like OpenAMS lanes (one sensor per lane, no lane stepper).
    lane_mod = importlib.import_module('extras.AFC_lane')
    unit_mod = importlib.import_module('extras.AFC_unit')
    for lst in (lane_mod.ONLY_LOAD_TYPES, lane_mod.EXCLUDE_TYPES,
                unit_mod.SENSORLESS_UNITS):
        if IFS_TYPE not in lst:
            lst.append(IFS_TYPE)


def drop_ifs_unit(fileconfig):
    """Remove the IFS unit's sections (plugin.cfg's [AFC_IFS IFS], its hub,
    and every lane whose unit is an AFC_IFS unit) from Klipper's parsed
    config, for another AFC unit in place of the IFS. Returns their names."""
    sections = fileconfig.sections()
    units = [s.split(None, 1)[1] for s in sections
             if s.startswith('AFC_IFS ') and len(s.split()) == 2]
    if not units:
        return []
    drop = ['AFC_IFS %s' % (u,) for u in units]
    hubs = set()
    for u in units:
        sec = 'AFC_IFS %s' % (u,)
        hubs.add(fileconfig.get(sec, 'hub') if fileconfig.has_option(sec, 'hub') else u)
    used_hubs = set()
    for s in sections:
        parts = s.split()
        if len(parts) != 2 or s in drop:
            continue
        unit = (fileconfig.get(s, 'unit') if fileconfig.has_option(s, 'unit') else '')
        if parts[0] in ('AFC_lane', 'AFC_stepper') and unit.split(':')[0] in units:
            drop.append(s)
        elif fileconfig.has_option(s, 'hub'):
            used_hubs.add(fileconfig.get(s, 'hub'))
    drop += ['AFC_hub %s' % (h,) for h in sorted(hubs - used_hubs)
             if fileconfig.has_section('AFC_hub %s' % (h,))]
    for s in drop:
        fileconfig.remove_section(s)
    return drop


def wrap_mode2_restore(mod):
    """Keep the AFC state restore, but leave a Kiln slicer purge at the box."""
    cls = mod.afc
    if getattr(cls, '_kiln_mode2_wrapped', False):
        return
    original_command = cls.cmd_CHANGE_TOOL
    original_restore = cls.restore_pos

    def command(self, gcmd):
        name = gcmd.get_commandline().split(';', 1)[0].strip().split()
        tool = name[0].upper() if name else ''
        lane = self.lanes.get(self.tool_cmds.get(tool, ''))
        backend = getattr(getattr(lane, 'unit_obj', None), 'backend', None)
        sv = self.printer.lookup_object('save_variables', None)
        ps = self.printer.lookup_object('print_stats', None)
        mode = sv.allVariables.get('use_trash_on_print') if sv else None
        previous = getattr(self, '_kiln_mode2_command', False)
        self._kiln_mode2_command = (
            tool in ('T0', 'T1', 'T2', 'T3')
            and getattr(backend, 'name', None) == 'kiln'
            and str(mode) == '2'
            and ps is not None
            and ps.get_status(self.reactor.monotonic()).get('state') == 'printing'
            and not self.function.is_paused()
            and not self.error_state
        )
        try:
            return original_command(self, gcmd)
        finally:
            self._kiln_mode2_command = previous

    def restore(self, move_z_first=True):
        if not (getattr(self, '_kiln_mode2_command', False)
                and self.in_toolchange and self.position_saved
                and not self.error_state and not self.function.is_paused()):
            return original_restore(self, move_z_first)

        # Equivalent to AFC's pinned restore_pos(), minus the XYZ moves
        # and the assignment of stale XYZ coordinates.
        gm = self.gcode_move
        self.current_state = mod.State.RESTORING_POS
        gm.base_position = list(self.base_position)
        gm.homing_position = list(self.homing_position)
        gm.absolute_coord = self.absolute_coord
        mod.set_gcode_absolute_extrude(gm, self.absolute_extrude)
        gm.extrude_factor = self.extrude_factor
        gm.speed = self.speed
        gm.speed_factor = self.speed_factor
        gm.base_position[3] += gm.last_position[3] - self.last_gcode_position[3]
        self.current_state = mod.State.IDLE
        self.position_saved = False
        self.logger.info("KilnOS mode 2: preserving purge-box position")

    cls.cmd_CHANGE_TOOL = command
    cls.restore_pos = restore
    cls._kiln_mode2_wrapped = True


class KilnAFC:
    def __init__(self, config):
        self.printer = printer = config.get_printer()
        self._retire_afclite(config)
        afc_path = config.get('afc_path', None)
        self.afc_dir = afc_path or DEFAULT_AFC_DIR
        if afc_path is None:
            # The plugin's own checkout follows AFC_COMMIT (an afc_path of
            # your own is left as it is)
            try:
                note = sync_afc_checkout(self.afc_dir)
            except (OSError, RuntimeError) as e:
                raise config.error(
                    "afc_kiln: can't bring AFC to the commit in AFC_COMMIT: "
                    "%s. Check the network, then restart Klipper "
                    "(/etc/init.d/S61klipper restart)." % (e,))
            if note:
                logging.info("afc_kiln: %s", note)
        if not os.path.isfile(os.path.join(self.afc_dir, 'extras', 'AFC.py')):
            raise config.error(
                "afc_kiln: no AFC checkout at %s. Run install.sh in the "
                "plugin folder (it clones AFC-Klipper-Add-On)." % self.afc_dir)
        self.dirs = _add_paths(self.afc_dir)
        reloaded = _forget_modules(self.dirs)
        if reloaded:
            logging.info("afc_kiln: reloading %d AFC modules", len(reloaded))
        _teach_unit_type()
        self._prepare_varfile(config)
        self.dropped = drop_ifs_unit(config.fileconfig)             if config.has_section(FF_SECTION) else []
        self._orig_load_object = printer.load_object
        printer.load_object = self._load_object
        logging.info("afc_kiln: AFC modules from %s", ", ".join(self.dirs))
        if config.has_section(FF_SECTION):
            # [AFC_extruder extruder] (plugin.cfg) comes before the unit file's
            # [AFC_FF] and looks AFC_FF's head sensor up when it loads
            self._load_object(config, FF_SECTION)
            logging.info("afc_kiln: another AFC unit in place of the IFS; "
                         "dropped %s", ", ".join(self.dropped) or "nothing")

    def _retire_afclite(self, config):
        # ENABLE_PLUGIN NAME=afc with afclite still on: this start has
        # both, which can't work. Drop afclite, then restart without it.
        try:
            was_on = retire_afclite(config)
        except OSError as e:
            raise config.error(
                "afc_kiln: can't remove afclite (KILN_AFCLite): %s. Run "
                "DISABLE_PLUGIN NAME=afclite, then REMOVE_PLUGIN NAME=afclite"
                % (e,))
        if not was_on:
            return
        reactor = self.printer.get_reactor()
        reactor.register_callback(
            lambda e: self.printer.request_exit('restart'),
            reactor.monotonic() + 2.)
        raise config.error(
            "KILN_AFC replaces afclite (KILN_AFCLite): afclite is now "
            "disabled and deleted. Klipper restarts without it in a moment.")

    def _prepare_varfile(self, config):
        # AFC's PREP stops with "AFC.var.unit file not found" on a first
        # start; install-afc.sh would have created its folder and file.
        if not config.has_section('AFC'):
            return
        var = config.getsection('AFC').get('VarFile', None, note_valid=False)
        if not var:
            return
        try:
            os.makedirs(os.path.dirname(var), exist_ok=True)
            unit = var + '.unit'
            if not os.path.exists(unit):
                with open(unit, 'w') as f:
                    f.write('{}')
        except OSError as e:
            logging.warning("afc_kiln: can't prepare %s: %s", var, e)

    def _module_file(self, name):
        for d in self.dirs:
            if os.path.isfile(os.path.join(d, name + '.py')):
                return True
        return False

    def _load_object(self, config, section, *default):
        objects = self.printer.objects
        if section in objects:
            return objects[section]
        if section in self.dropped:
            # Klipper's section list was made before drop_ifs_unit
            return None
        parts = section.split()
        if not parts or not self._module_file(parts[0]):
            return self._orig_load_object(config, section, *default)
        mod = importlib.import_module('extras.' + parts[0])
        if section == 'AFC':
            wrap_mode2_restore(mod)
        init = getattr(mod, 'load_config_prefix' if len(parts) > 1
                       else 'load_config', None)
        if init is None:
            if default:
                return default[0]
            raise config.error("Unable to load module '%s'" % (section,))
        objects[section] = init(config.getsection(section))
        return objects[section]

    def get_status(self, eventtime):
        return {'afc_path': self.afc_dir}


# Kiln OS 1.0 keeps kiln_ names for itself (v0.1.0-rc1 on), so this plugin's
# sections were renamed. A user config still naming the old ones is fixed
# here at the first start that sees them: the old options are used for this
# start, the section headers in the config folder's .cfg files are renamed
# (a .before-rename copy of each changed file is kept), and a red console
# line plus a warning in Fluidd/Mainsail ask for a reboot.
LEGACY_PLUGIN = 'KILN_AFC'
LEGACY_SECTIONS = {'kiln_afc': 'afc_kiln'}


def _renamed(section):
    """The new name for an old section name ("kiln_x" or "kiln_x name"),
    None for any other section."""
    head, _, rest = section.strip().partition(' ')
    new = LEGACY_SECTIONS.get(head.lower())
    if new is None:
        return None
    rest = rest.strip()
    return new + (' ' + rest if rest else '')


def _rename_in_files(cfg_file):
    """Renames the old section headers in the .cfg files of cfg_file's
    folder (and below; links to folders are not followed). Returns the
    changed files (relative paths) and the errors."""
    import os, re, shutil
    olds = sorted(LEGACY_SECTIONS, key=len, reverse=True)
    pat = re.compile(r'^(\s*(?:#\*#\s*)?\[\s*)(%s)((?:\s+[^\]]*?)?)(\s*\])'
                     % '|'.join(map(re.escape, olds)), re.I | re.M)
    def sub(m):
        return m.group(1) + LEGACY_SECTIONS[m.group(2).lower()] + m.group(3) + m.group(4)
    root = os.path.dirname(os.path.abspath(cfg_file))
    changed, errors = [], []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for name in files:
            if not name.endswith('.cfg'):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, encoding='utf-8', errors='surrogateescape',
                          newline='') as f:
                    text = f.read()
                new_text = pat.sub(sub, text)
                if new_text == text:
                    continue
                backup = path + '.before-rename'
                if not os.path.exists(backup):
                    shutil.copy2(path, backup)
                tmp = path + '.renaming'
                with open(tmp, 'w', encoding='utf-8', errors='surrogateescape',
                          newline='') as f:
                    f.write(new_text)
                shutil.copymode(path, tmp)
                os.replace(tmp, path)
                changed.append(os.path.relpath(path, root))
            except OSError as e:
                errors.append("%s: %s" % (path, e))
    return changed, errors


def migrate_legacy_sections(config):
    import logging
    fc = config.fileconfig
    found = [(s, _renamed(s)) for s in fc.sections() if _renamed(s)]
    if not found:
        return
    printer = config.get_printer()
    me = config.get_name().lower()
    for old, new in found:
        legacy = config.getsection(old)
        options = fc.options(old)
        for opt in options:
            if new.lower() == me:
                fc.set(config.get_name(), opt, fc.get(old, opt))
            legacy.get(opt)  # marks it used: no "not a valid section" stop
        if not options:
            config.access_tracking[(old.lower(), '__renamed__')] = 1
    pairs = ", ".join("[%s] \u2192 [%s]" % p for p in found)
    try:
        cfg_file = printer.get_start_args().get('config_file')
    except Exception:
        cfg_file = None
    changed, errors = _rename_in_files(cfg_file) if cfg_file else ([], [])
    if changed and not errors:
        msg = ("\u26a0\ufe0f\U0001f6a8 %s: Kiln OS 1.0 keeps kiln_ names for "
               "itself, so these sections were RENAMED in %s: %s. REBOOT the "
               "printer now (or RESTART Klipper) to use the new names. "
               "\U0001f6a8\u26a0\ufe0f" % (LEGACY_PLUGIN, ", ".join(changed), pairs))
    else:
        msg = ("\u26a0\ufe0f\U0001f6a8 %s: Kiln OS 1.0 keeps kiln_ names for "
               "itself. Rename these sections in your config BY HAND: %s, then "
               "REBOOT the printer (or RESTART Klipper).%s \U0001f6a8\u26a0\ufe0f"
               % (LEGACY_PLUGIN, pairs,
                  (" Could not change: " + "; ".join(errors)) if errors else ""))
    logging.warning(msg)
    try:
        printer.lookup_object('configfile').runtime_warning(msg)
    except Exception:
        pass
    def say(eventtime):
        try:
            printer.lookup_object('gcode').respond_raw('!! ' + msg)
        except Exception:
            logging.exception("%s: rename notice", LEGACY_PLUGIN)
        return printer.get_reactor().NEVER
    def ready():
        # Moonraker subscribes to the console a little after klippy:ready, so
        # a line sent at once only reaches klippy.log: send it twice, later
        reactor = printer.get_reactor()
        now = reactor.monotonic()
        for delay in (15., 60.):
            reactor.register_timer(say, now + delay)
    try:
        printer.register_event_handler('klippy:ready', ready)
    except Exception:
        pass


def load_config(config):
    migrate_legacy_sections(config)
    return KilnAFC(config)
