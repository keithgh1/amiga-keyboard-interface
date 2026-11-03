"""Runs amikbd commands, then logs LED voltages and LED events for a while.

Usage: python tools/ledwatch.py [--secs N] "<cmd>" ["<cmd>" ...]
Pass commands as separate arguments; the backslash escapes reach the firmware unchanged.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from amikbd import Amikbd  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("commands", nargs="*")
ap.add_argument("--secs", type=float, default=15)
args = ap.parse_args()

kb = Amikbd(verbose=False)
t0 = time.time()
last = None


def sample(tag=""):
    global last
    ok, reply, events = kb.command("L", timeout=2)
    for e in events:
        print(f"{time.time() - t0:6.2f}  {e}")
    if reply != last or tag:
        print(f"{time.time() - t0:6.2f}  {tag:5} {reply}")
        last = reply


sample("base")
for c in args.commands:
    ok, reply, events = kb.command(c)
    print(f"{time.time() - t0:6.2f}  sent  {c!r} -> {reply} {events or ''}")
while time.time() - t0 < args.secs:
    sample()
    time.sleep(0.02)
sample("end")
