"""KILN_IFSJacker's fake IFS with a control file, for emu_kiln_afc.py.

Every tick the fake reads (and deletes) the control file; each line is
`remove <slot>` or `insert <slot>`, so the test can run a spool out in a
print.

    python3 tests/emu_fake_ifs.py --emu <KILN_IFSJacker/emulator> \\
        --link /tmp/ifs_afc --ctl ~/kiln_afc_test/ifs_ctl [--speed 40] [--log f]
"""
import argparse
import os
import sys


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--emu", required=True, help="KILN_IFSJacker emulator dir")
    ap.add_argument("--link", required=True)
    ap.add_argument("--ctl", required=True)
    ap.add_argument("--speed", type=float, default=10.)
    ap.add_argument("--log")
    args = ap.parse_args()
    sys.path.insert(0, args.emu)
    from fake_ifs import FakeIfs, serve_pty

    class CtlIfs(FakeIfs):
        def tick(self):
            super().tick()
            try:
                with open(args.ctl) as f:
                    lines = f.read().splitlines()
                os.unlink(args.ctl)
            except FileNotFoundError:
                return
            for line in lines:
                parts = line.split()
                if len(parts) == 2 and parts[0] in ("remove", "insert"):
                    getattr(self, parts[0] + "_spool")(int(parts[1]))
                    print("ctl: %s" % (line,), flush=True)

    serve_pty(CtlIfs(speed=args.speed), args.link,
              open(args.log, "w") if args.log else None)


if __name__ == "__main__":
    main()
