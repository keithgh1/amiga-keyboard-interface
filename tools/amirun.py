# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Keith Monahan
"""Build with vbcc (+a500), send to the A500 over LinkAgent, run it in the Shell, grab the screen.

python tools/amirun.py devchain/examples/hello/hello.c
python tools/amirun.py src/main.c src/gfx.c --name game --args "-v" --wait 8
python tools/amirun.py devchain/build/LinkAgent --name la     # already-linked executable
python tools/amirun.py hello.c --build-only

Needs LinkAgent running with its strip visible and the A500-Shell active (the
LinkAgent boot disk sets both up). Output: devchain/out/<name>, captures/run-<name>.jpg.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
VBCC = ROOT / "devchain/vendor/vbcc"
OUT = ROOT / "devchain/out"
sys.path.insert(0, str(ROOT / "devchain"))
sys.path.insert(0, str(ROOT / "tools"))
from build import check_ks13_hunks  # noqa: E402
from amikbd import snapshot  # noqa: E402
from amipush import Link, close_keyboard, open_keyboard, put  # noqa: E402

SOURCES = {".c", ".s", ".asm"}


def compile_program(sources, name, cflags):
    OUT.mkdir(exist_ok=True)
    exe = OUT / name
    env = dict(os.environ, VBCC=str(VBCC))
    env["PATH"] = str(VBCC / "bin") + os.pathsep + env["PATH"]
    cmd = [str(VBCC / "bin/vc.exe"), "+a500", "-O2", *cflags, "-o", str(exe), *map(str, sources)]
    subprocess.run(cmd, env=env, check=True)
    return exe


def typed(text):
    """Escape text for the firmware's T command."""
    if any(ord(c) < 32 or ord(c) > 126 for c in text):
        raise ValueError("only printable ASCII can be typed")
    return text.replace("\\", "\\\\")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", type=Path, help=".c/.s sources, or one linked executable")
    ap.add_argument("--name", help="program name on the Amiga (default: first input's stem)")
    ap.add_argument("--args", default="", help="command-line arguments for the program")
    ap.add_argument("--cflags", default="", help="extra vc options, e.g. \"-DDEBUG -lmieee\"")
    ap.add_argument("--wait", type=float, default=3, help="seconds to let it run before the snapshot")
    ap.add_argument("--build-only", action="store_true")
    ap.add_argument("--no-run", action="store_true", help="send but do not start it")
    args = ap.parse_args()

    name = args.name or args.inputs[0].stem
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,24}", name):
        ap.error("--name must be 1..25 characters of A-Z a-z 0-9 _ . -")
    if all(p.suffix.lower() in SOURCES for p in args.inputs):
        exe = compile_program(args.inputs, name, args.cflags.split())
    elif len(args.inputs) == 1:
        exe = args.inputs[0]
    else:
        ap.error("give .c/.s sources, or exactly one executable")
    check_ks13_hunks(exe)
    print(f"Built {exe.relative_to(ROOT) if exe.is_relative_to(ROOT) else exe}: {exe.stat().st_size} bytes, KS 1.3 hunks OK")
    if args.build_only:
        return

    try:
        kb = open_keyboard()
    except (OSError, RuntimeError) as e:
        sys.exit(f"amirun: {e}")
    try:
        # LinkAgent never overwrites; clear the previous copy from the Shell first.
        # Delete loads from floppy, so wait for the drive rather than a fixed delay.
        kb.command(f"T delete ram:{name}\\n")
        kb.command("WAITDISK")
        put(Link(kb), exe, f"RAM:{name}")
        if not args.no_run:
            line = f"ram:{name} {typed(args.args)}".rstrip()
            ok, reply, _ = kb.command(f"T {line}\\n")
            if not ok:
                raise RuntimeError(f"typing the command failed: {reply}")
            time.sleep(args.wait)
            shot = ROOT / f"captures/run-{name}.jpg"
            snapshot(shot)
            print(f"Ran RAM:{name}; screen: {shot.relative_to(ROOT)}")
    except (OSError, RuntimeError, ValueError) as e:
        sys.exit(f"amirun: {e}")
    finally:
        close_keyboard(kb)


if __name__ == "__main__":
    main()
